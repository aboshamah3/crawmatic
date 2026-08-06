"""On-demand variant rescrape router unit tests
(`POST /v1/variants/{variant_id}/rescrape` — the WooCommerce plugin's
"refresh prices now" action).

Exercised the same DB-less way as `tests/unit/test_jobs_router.py` (the
sibling job-creating router): `TestClient` with
`app.dependency_overrides[get_current_principal]` swapped for a fake
principal bound to `FakeOrmSession` (`tests/unit/_jobs_fake_session.py`
— the `WHERE`-evaluating double that also implements `add`/`flush`,
which the price-route fake does not), plus a patched
`app_shared.jobs.service.enqueue` so no Redis/Celery broker is touched.

Covered per `routers/variants.py::rescrape_variant`:

* `202` — one `scrape_jobs` row (`scope=VARIANT`, `priority=HIGH`,
  `total_targets=N`) + one `scrape_job_targets` row per **ACTIVE** match
  (inactive excluded), one `scrape.dispatch_job` enqueue, and a
  `{"job_id", "match_count"}` body.
* `404 NOT_FOUND` — unknown / cross-workspace variant (no job, no enqueue).
* `409 NO_ACTIVE_MATCHES` — variant whose matches are all inactive: no
  job row at all (unlike `POST /v1/jobs/run/variant/{id}`, which creates
  an immediately-COMPLETED one).
* `429 RESCRAPE_COOLDOWN` — an unfinished `scope=VARIANT` job for this
  variant younger than `RESCRAPE_COOLDOWN`, including the boundary cases
  that must *not* trip it (finished job, expired window, another
  variant, another workspace).
* Scope gating — declared `require_scopes("jobs:write")`, behavioral 403.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app_shared.jobs.service as service_module
from app_shared.enums import (
    MatchPriority,
    MatchStatus,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
)
from app_shared.models.catalog import ProductVariant
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.task_names import SCRAPE_DISPATCH_JOB

from app.deps import Principal, get_current_principal
from app.main import app
from app.routers.variants import RESCRAPE_COOLDOWN

from unit._jobs_fake_session import FakeOrmSession

# Route-introspection helpers — imported, never re-copied, so this module
# can never disagree with the others about how a route's declared
# `require_scopes(...)` is read.
from unit.test_catalog_scope_gating import _required_scopes, _route

WORKSPACE_ID = uuid.uuid4()
OTHER_WORKSPACE_ID = uuid.uuid4()


# --- shared plumbing ---------------------------------------------------------


class _FakeEnqueue:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, name: str, *, queue: str, kwargs: dict[str, Any] | None = None) -> None:
        self.calls.append({"name": name, "queue": queue, "kwargs": kwargs})


@pytest.fixture()
def fake_enqueue(monkeypatch: pytest.MonkeyPatch) -> _FakeEnqueue:
    fake = _FakeEnqueue()
    monkeypatch.setattr(service_module, "enqueue", fake)
    return fake


@pytest.fixture()
def fake_session() -> FakeOrmSession:
    return FakeOrmSession()


def _override_principal(
    session: FakeOrmSession, *, scopes: list[str], workspace_id: uuid.UUID = WORKSPACE_ID
):
    def _dependency() -> Iterator[tuple[FakeOrmSession, Principal]]:
        yield session, Principal(
            kind="api_key",
            id=uuid.uuid4(),
            role=None,
            scopes=scopes,
            workspace_id=workspace_id,
        )

    return _dependency


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# --- row builders ------------------------------------------------------------


def _make_variant(*, workspace_id: uuid.UUID = WORKSPACE_ID) -> ProductVariant:
    variant = ProductVariant(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        title="Variant A",
    )
    variant.id = uuid.uuid4()
    return variant


def _make_match_for_variant(
    *,
    variant_id: uuid.UUID,
    workspace_id: uuid.UUID = WORKSPACE_ID,
    status: MatchStatus = MatchStatus.ACTIVE,
) -> CompetitorProductMatch:
    match = CompetitorProductMatch(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=variant_id,
        competitor_id=uuid.uuid4(),
        competitor_url="https://shop.example.com/p/1",
        normalized_competitor_url="https://shop.example.com/p/1",
        url_pattern="https://shop.example.com/p/1",
        url_pattern_version=1,
        priority=MatchPriority.NORMAL,
        status=status,
    )
    match.id = uuid.uuid4()
    return match


def _make_variant_job(
    *,
    variant_id: uuid.UUID,
    workspace_id: uuid.UUID = WORKSPACE_ID,
    status: ScrapeJobStatus = ScrapeJobStatus.PENDING,
    age: timedelta = timedelta(minutes=1),
) -> ScrapeJob:
    job = ScrapeJob(
        workspace_id=workspace_id,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.VARIANT,
        product_variant_id=variant_id,
        status=status,
        priority=MatchPriority.HIGH,
        total_targets=2,
        success_count=0,
        failure_count=0,
        skipped_count=0,
        requested_by=uuid.uuid4(),
        source=ScrapeJobSource.API,
        created_at=datetime.now(timezone.utc) - age,
    )
    job.id = uuid.uuid4()
    return job


def _seed_rescrapable_variant(
    session: FakeOrmSession, *, active: int = 2
) -> tuple[ProductVariant, list[CompetitorProductMatch]]:
    variant = _make_variant()
    matches = [_make_match_for_variant(variant_id=variant.id) for _ in range(active)]
    session.seed(variant, *matches)
    return variant, matches


# --- 202 success -------------------------------------------------------------


def test_rescrape_returns_202_with_job_id_and_match_count(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    variant, active_matches = _seed_rescrapable_variant(fake_session, active=3)
    inactive = _make_match_for_variant(variant_id=variant.id, status=MatchStatus.PAUSED)
    fake_session.seed(inactive)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"]
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 202, resp.json()
    body = resp.json()
    assert body["match_count"] == 3
    job_id = uuid.UUID(body["job_id"])

    jobs = fake_session._rows.get(ScrapeJob, [])
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == job_id
    assert job.scope == ScrapeScope.VARIANT
    assert job.product_variant_id == variant.id
    assert job.workspace_id == WORKSPACE_ID
    assert job.status == ScrapeJobStatus.PENDING
    assert job.total_targets == 3
    assert job.requested_by is not None
    # The model's existing priority notion, raised for a user-triggered run.
    assert job.priority == MatchPriority.HIGH

    targets = fake_session._rows.get(ScrapeJobTarget, [])
    assert {target.match_id for target in targets} == {m.id for m in active_matches}
    assert inactive.id not in {target.match_id for target in targets}
    for target in targets:
        assert target.scrape_job_id == job_id


def test_rescrape_enqueues_dispatch_once_through_the_existing_seam(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    """Same task/queue/kwargs the scheduler and `POST /v1/jobs/run/variant`
    use — this endpoint adds no queue of its own."""
    variant, _matches = _seed_rescrapable_variant(fake_session)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"]
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 202
    assert len(fake_enqueue.calls) == 1
    call = fake_enqueue.calls[0]
    assert call["name"] == SCRAPE_DISPATCH_JOB
    assert call["queue"] == "scrape_dispatch"
    assert call["kwargs"] == {
        "scrape_job_id": resp.json()["job_id"],
        "workspace_id": str(WORKSPACE_ID),
    }


def test_rescrape_ignores_another_variants_matches(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    variant, mine = _seed_rescrapable_variant(fake_session, active=1)
    other_variant = _make_variant()
    fake_session.seed(
        other_variant, _make_match_for_variant(variant_id=other_variant.id)
    )
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"]
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 202
    assert resp.json()["match_count"] == 1
    targets = fake_session._rows.get(ScrapeJobTarget, [])
    assert [target.match_id for target in targets] == [mine[0].id]


# --- 404 ---------------------------------------------------------------------


def test_rescrape_unknown_variant_is_404_no_job_no_enqueue(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"]
    )

    resp = client.post(f"/v1/variants/{uuid.uuid4()}/rescrape")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"
    assert fake_session._rows.get(ScrapeJob, []) == []
    assert fake_enqueue.calls == []


def test_rescrape_cross_workspace_variant_is_404_no_job_no_enqueue(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    variant = _make_variant(workspace_id=OTHER_WORKSPACE_ID)
    fake_session.seed(
        variant,
        _make_match_for_variant(variant_id=variant.id, workspace_id=OTHER_WORKSPACE_ID),
    )
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"], workspace_id=WORKSPACE_ID
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"
    assert fake_session._rows.get(ScrapeJob, []) == []
    assert fake_enqueue.calls == []


# --- 409 NO_ACTIVE_MATCHES ---------------------------------------------------


def test_rescrape_variant_with_no_matches_is_409(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    variant = _make_variant()
    fake_session.seed(variant)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"]
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"]["code"] == "NO_ACTIVE_MATCHES"
    # No job row at all — unlike `POST /v1/jobs/run/variant/{id}`, which
    # returns an immediately-COMPLETED empty job.
    assert fake_session._rows.get(ScrapeJob, []) == []
    assert fake_session._rows.get(ScrapeJobTarget, []) == []
    assert fake_enqueue.calls == []


@pytest.mark.parametrize("status", [MatchStatus.PAUSED, MatchStatus.ARCHIVED])
def test_rescrape_variant_with_only_inactive_matches_is_409(
    client: TestClient,
    fake_session: FakeOrmSession,
    fake_enqueue: _FakeEnqueue,
    status: MatchStatus,
) -> None:
    variant = _make_variant()
    fake_session.seed(variant, _make_match_for_variant(variant_id=variant.id, status=status))
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"]
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"]["code"] == "NO_ACTIVE_MATCHES"
    assert fake_session._rows.get(ScrapeJob, []) == []
    assert fake_enqueue.calls == []


def test_rescrape_cross_workspace_match_does_not_count_as_active(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    """Same variant id, an ACTIVE match row owned by another workspace ->
    still nothing to scrape here."""
    variant = _make_variant()
    fake_session.seed(
        variant,
        _make_match_for_variant(variant_id=variant.id, workspace_id=OTHER_WORKSPACE_ID),
    )
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"], workspace_id=WORKSPACE_ID
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"]["code"] == "NO_ACTIVE_MATCHES"


# --- 429 RESCRAPE_COOLDOWN ---------------------------------------------------


@pytest.mark.parametrize("status", [ScrapeJobStatus.PENDING, ScrapeJobStatus.RUNNING])
def test_rescrape_within_cooldown_of_unfinished_job_is_429(
    client: TestClient,
    fake_session: FakeOrmSession,
    fake_enqueue: _FakeEnqueue,
    status: ScrapeJobStatus,
) -> None:
    variant, _matches = _seed_rescrapable_variant(fake_session)
    in_flight = _make_variant_job(variant_id=variant.id, status=status)
    fake_session.seed(in_flight)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"]
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 429
    error = resp.json()["detail"]["error"]
    assert error["code"] == "RESCRAPE_COOLDOWN"
    assert error["job_id"] == str(in_flight.id)
    assert int(resp.headers["retry-after"]) > 0
    # No second job, no duplicate dispatch of the same competitor pages.
    assert fake_session._rows.get(ScrapeJob, []) == [in_flight]
    assert fake_enqueue.calls == []


def test_cooldown_reports_the_most_recent_unfinished_job(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    variant, _matches = _seed_rescrapable_variant(fake_session)
    older = _make_variant_job(variant_id=variant.id, age=timedelta(minutes=5))
    newer = _make_variant_job(variant_id=variant.id, age=timedelta(minutes=1))
    fake_session.seed(older, newer)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"]
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 429
    assert resp.json()["detail"]["error"]["job_id"] == str(newer.id)


def test_finished_job_inside_the_window_does_not_trip_the_cooldown(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    """The user already has those prices; asking again must be allowed."""
    variant, _matches = _seed_rescrapable_variant(fake_session)
    fake_session.seed(
        _make_variant_job(
            variant_id=variant.id, status=ScrapeJobStatus.COMPLETED, age=timedelta(minutes=1)
        )
    )
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"]
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 202
    assert len(fake_enqueue.calls) == 1


def test_unfinished_job_older_than_the_window_does_not_trip_the_cooldown(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    """A job stuck PENDING for longer than the cooldown must not wedge the
    variant out of ever being rescraped again."""
    variant, _matches = _seed_rescrapable_variant(fake_session)
    fake_session.seed(
        _make_variant_job(variant_id=variant.id, age=RESCRAPE_COOLDOWN + timedelta(minutes=1))
    )
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"]
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 202
    assert len(fake_enqueue.calls) == 1


def test_cooldown_is_per_variant_and_per_workspace(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    variant, _matches = _seed_rescrapable_variant(fake_session)
    fake_session.seed(
        # Another variant's in-flight job.
        _make_variant_job(variant_id=uuid.uuid4()),
        # Another workspace's in-flight job for this same variant id.
        _make_variant_job(variant_id=variant.id, workspace_id=OTHER_WORKSPACE_ID),
    )
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"], workspace_id=WORKSPACE_ID
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 202
    assert len(fake_enqueue.calls) == 1


def test_second_rescrape_immediately_after_a_successful_one_is_429(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    """End-to-end de-duplication: the job the first call creates is exactly
    what the second call's cooldown lookup finds."""
    variant, _matches = _seed_rescrapable_variant(fake_session)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["jobs:write"]
    )

    first = client.post(f"/v1/variants/{variant.id}/rescrape")
    assert first.status_code == 202

    second = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert second.status_code == 429
    assert second.json()["detail"]["error"]["job_id"] == first.json()["job_id"]
    assert len(fake_session._rows.get(ScrapeJob, [])) == 1
    assert len(fake_enqueue.calls) == 1


# --- scope gating ------------------------------------------------------------


def test_rescrape_without_jobs_write_scope_is_403(
    client: TestClient, fake_session: FakeOrmSession, fake_enqueue: _FakeEnqueue
) -> None:
    variant, _matches = _seed_rescrapable_variant(fake_session)
    app.dependency_overrides[get_current_principal] = _override_principal(
        fake_session, scopes=["variants:write", "jobs:read"]
    )

    resp = client.post(f"/v1/variants/{variant.id}/rescrape")

    assert resp.status_code == 403
    assert fake_session._rows.get(ScrapeJob, []) == []
    assert fake_enqueue.calls == []


def test_rescrape_route_declares_jobs_write_scope() -> None:
    """Gated like the sibling `POST /v1/jobs/run/variant/{id}` (it *is* a job
    run), not like the catalog routes it shares a router with."""
    route = _route("/v1/variants/{variant_id}/rescrape", "POST")
    assert _required_scopes(route) == ("jobs:write",)
