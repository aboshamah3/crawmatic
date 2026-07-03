"""Recompute triggers (b)/(c): client price/currency change + match
archive/pause (SPEC-09 US3 T030/T031/T033, contracts/recompute-triggers.md,
FR-015/FR-016, SC-003).

Exercised via `TestClient` with `app.dependency_overrides[get_current_principal]`
swapped for a fake, DB-less principal bound to a shared in-memory fake
session (`tests/unit/_jobs_fake_session.py::FakeOrmSession` for the
equality-only lookups these routes issue; `tests/unit/_alerts_fake_session.py::FakeAlertsSession`
for the one Insert-with-`on_conflict_do_update` statement
`bulk_upsert_variants` issues). `app.routers.variants.enqueue`/
`app.routers.matches.enqueue` are monkeypatched with a recording fake —
never a real Celery producer, never `apps/workers`.

- Trigger (b): `PATCH /v1/variants/{id}` changing `price`/`currency`
  enqueues one recompute (`scrape_job_id=None`, correct kwargs); a PATCH
  changing only `title` enqueues none; `POST /v1/variants/bulk-upsert`
  enqueues once per upserted variant.
- Trigger (c): `PATCH /v1/matches/{id}` transitioning `status` into an
  archived/paused (non-active) value enqueues one recompute for its
  variant; a PATCH that doesn't change the effective status enqueues
  none; `DELETE /v1/matches/{id}` (this router's archive path) enqueues
  one recompute for its variant.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app_shared.enums import HealthStatus, MatchPriority, MatchStatus
from app_shared.models.catalog import Product, ProductVariant
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.task_names import PRICE_ANALYSIS_RECOMPUTE

from app.deps import Principal, get_current_principal
from app.main import app
import app.routers.matches as matches_router
import app.routers.variants as variants_router

from unit._alerts_fake_session import FakeAlertsSession
from unit._jobs_fake_session import FakeOrmSession

WORKSPACE_ID = uuid.uuid4()


class _RecordingEnqueue:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, name: str, *, queue: str, kwargs: dict | None = None) -> None:
        self.calls.append({"name": name, "queue": queue, "kwargs": kwargs})


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def enqueue_variants(monkeypatch: pytest.MonkeyPatch) -> _RecordingEnqueue:
    fake = _RecordingEnqueue()
    monkeypatch.setattr(variants_router, "enqueue", fake)
    return fake


@pytest.fixture()
def enqueue_matches(monkeypatch: pytest.MonkeyPatch) -> _RecordingEnqueue:
    fake = _RecordingEnqueue()
    monkeypatch.setattr(matches_router, "enqueue", fake)
    return fake


def _override_principal(session, *, scopes: list[str], workspace_id: uuid.UUID = WORKSPACE_ID):
    def _dependency() -> Iterator[tuple]:
        yield session, Principal(
            kind="api_key",
            id=uuid.uuid4(),
            role=None,
            scopes=scopes,
            workspace_id=workspace_id,
        )

    return _dependency


def _make_variant(
    *, workspace_id: uuid.UUID = WORKSPACE_ID, product_id: uuid.UUID | None = None
) -> ProductVariant:
    now = datetime.now(timezone.utc)
    variant = ProductVariant(
        workspace_id=workspace_id,
        product_id=product_id or uuid.uuid4(),
        title="Widget",
        current_price=Decimal("2999.0000"),
        currency="SAR",
        status="active",
        created_at=now,
        updated_at=now,
    )
    variant.id = uuid.uuid4()
    return variant


def _recompute_calls(enqueue: _RecordingEnqueue) -> list[dict]:
    return [c for c in enqueue.calls if c["name"] == PRICE_ANALYSIS_RECOMPUTE]


# --- Trigger (b): PATCH /v1/variants/{id} -----------------------------------


def test_patch_variant_price_change_enqueues_one_recompute(
    client: TestClient, enqueue_variants: _RecordingEnqueue
) -> None:
    session = FakeOrmSession()
    variant = _make_variant()
    session.seed(variant)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["variants:write"]
    )

    response = client.patch(f"/v1/variants/{variant.id}", json={"price": "3199.00"})

    assert response.status_code == 200
    calls = _recompute_calls(enqueue_variants)
    assert len(calls) == 1
    assert calls[0]["queue"] == "price_analysis"
    assert calls[0]["kwargs"] == {
        "workspace_id": str(WORKSPACE_ID),
        "product_variant_id": str(variant.id),
        "product_id": str(variant.product_id),
        "scrape_job_id": None,
    }


def test_patch_variant_currency_change_enqueues_one_recompute(
    client: TestClient, enqueue_variants: _RecordingEnqueue
) -> None:
    session = FakeOrmSession()
    variant = _make_variant()
    session.seed(variant)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["variants:write"]
    )

    response = client.patch(f"/v1/variants/{variant.id}", json={"currency": "USD"})

    assert response.status_code == 200
    assert len(_recompute_calls(enqueue_variants)) == 1


def test_patch_variant_title_only_enqueues_nothing(
    client: TestClient, enqueue_variants: _RecordingEnqueue
) -> None:
    session = FakeOrmSession()
    variant = _make_variant()
    session.seed(variant)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["variants:write"]
    )

    response = client.patch(f"/v1/variants/{variant.id}", json={"title": "New Title"})

    assert response.status_code == 200
    assert _recompute_calls(enqueue_variants) == []


def test_bulk_upsert_variants_enqueues_once_per_upserted_variant(
    client: TestClient, enqueue_variants: _RecordingEnqueue
) -> None:
    session = FakeAlertsSession()
    product = Product(workspace_id=WORKSPACE_ID, title="Parent Product")
    product.id = uuid.uuid4()
    session.seed(product)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["variants:write"]
    )

    payload = {
        "variants": [
            {
                "product_id": str(product.id),
                "title": "Variant A",
                "price": "10.00",
                "currency": "USD",
            },
            {
                "product_id": str(product.id),
                "title": "Variant B",
                "price": "20.00",
                "currency": "USD",
            },
        ]
    }
    response = client.post("/v1/variants/bulk-upsert", json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["upserted"] == 2
    calls = _recompute_calls(enqueue_variants)
    assert len(calls) == 2
    enqueued_variant_ids = {c["kwargs"]["product_variant_id"] for c in calls}
    response_variant_ids = {v["id"] for v in body["variants"]}
    assert enqueued_variant_ids == response_variant_ids
    for call in calls:
        assert call["kwargs"]["scrape_job_id"] is None
        assert call["kwargs"]["product_id"] == str(product.id)


# --- Trigger (c): match archive/pause ---------------------------------------


def _make_match(
    *,
    workspace_id: uuid.UUID = WORKSPACE_ID,
    product_id: uuid.UUID | None = None,
    product_variant_id: uuid.UUID | None = None,
    status: MatchStatus = MatchStatus.ACTIVE,
) -> CompetitorProductMatch:
    now = datetime.now(timezone.utc)
    match = CompetitorProductMatch(
        workspace_id=workspace_id,
        product_id=product_id or uuid.uuid4(),
        product_variant_id=product_variant_id or uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        competitor_url="https://competitor.example.com/p/1",
        normalized_competitor_url="https://competitor.example.com/p/1",
        url_pattern="https://competitor.example.com/p/*",
        url_pattern_version=1,
        priority=MatchPriority.NORMAL,
        status=status,
        health_status=HealthStatus.UNKNOWN,
        consecutive_failures=0,
        created_at=now,
        updated_at=now,
    )
    match.id = uuid.uuid4()
    return match


def test_patch_match_status_to_archived_enqueues_one_recompute(
    client: TestClient, enqueue_matches: _RecordingEnqueue
) -> None:
    session = FakeOrmSession()
    match = _make_match(status=MatchStatus.ACTIVE)
    session.seed(match)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["matches:write"]
    )

    response = client.patch(f"/v1/matches/{match.id}", json={"status": "ARCHIVED"})

    assert response.status_code == 200, response.text
    calls = _recompute_calls(enqueue_matches)
    assert len(calls) == 1
    assert calls[0]["queue"] == "price_analysis"
    assert calls[0]["kwargs"] == {
        "workspace_id": str(WORKSPACE_ID),
        "product_variant_id": str(match.product_variant_id),
        "product_id": str(match.product_id),
        "scrape_job_id": None,
    }


def test_patch_match_status_to_paused_enqueues_one_recompute(
    client: TestClient, enqueue_matches: _RecordingEnqueue
) -> None:
    session = FakeOrmSession()
    match = _make_match(status=MatchStatus.ACTIVE)
    session.seed(match)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["matches:write"]
    )

    response = client.patch(f"/v1/matches/{match.id}", json={"status": "PAUSED"})

    assert response.status_code == 200, response.text
    assert len(_recompute_calls(enqueue_matches)) == 1


def test_patch_match_other_field_only_enqueues_nothing(
    client: TestClient, enqueue_matches: _RecordingEnqueue
) -> None:
    session = FakeOrmSession()
    match = _make_match(status=MatchStatus.ACTIVE)
    session.seed(match)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["matches:write"]
    )

    response = client.patch(f"/v1/matches/{match.id}", json={"priority": "HIGH"})

    assert response.status_code == 200, response.text
    assert _recompute_calls(enqueue_matches) == []


def test_patch_match_re_archiving_an_already_archived_match_enqueues_nothing(
    client: TestClient, enqueue_matches: _RecordingEnqueue
) -> None:
    """No effective status transition (already ARCHIVED) -- no duplicate enqueue."""
    session = FakeOrmSession()
    match = _make_match(status=MatchStatus.ARCHIVED)
    session.seed(match)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["matches:write"]
    )

    response = client.patch(f"/v1/matches/{match.id}", json={"status": "ARCHIVED"})

    assert response.status_code == 200, response.text
    assert _recompute_calls(enqueue_matches) == []


def test_patch_match_status_to_active_enqueues_nothing(
    client: TestClient, enqueue_matches: _RecordingEnqueue
) -> None:
    """A transition INTO active isn't an archive/pause -- no recompute
    trigger from this path (the comparable set widening back isn't the
    scope of trigger (c))."""
    session = FakeOrmSession()
    match = _make_match(status=MatchStatus.PAUSED)
    session.seed(match)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["matches:write"]
    )

    response = client.patch(f"/v1/matches/{match.id}", json={"status": "ACTIVE"})

    assert response.status_code == 200, response.text
    assert _recompute_calls(enqueue_matches) == []


def test_delete_match_enqueues_one_recompute_for_its_variant(
    client: TestClient, enqueue_matches: _RecordingEnqueue
) -> None:
    session = FakeOrmSession()
    match = _make_match(status=MatchStatus.ACTIVE)
    session.seed(match)
    app.dependency_overrides[get_current_principal] = _override_principal(
        session, scopes=["matches:write"]
    )

    response = client.delete(f"/v1/matches/{match.id}")

    assert response.status_code == 200, response.text
    calls = _recompute_calls(enqueue_matches)
    assert len(calls) == 1
    assert calls[0]["kwargs"] == {
        "workspace_id": str(WORKSPACE_ID),
        "product_variant_id": str(match.product_variant_id),
        "product_id": str(match.product_id),
        "scrape_job_id": None,
    }


# --- neither trigger imports apps/workers ------------------------------------


def test_variants_and_matches_routers_do_not_import_apps_workers() -> None:
    import sys

    assert "apps.workers" not in sys.modules
    assert not any(
        name == "apps.workers" or name.startswith("apps.workers.")
        for name in sys.modules
    )
