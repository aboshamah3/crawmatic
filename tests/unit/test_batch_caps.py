"""Batch caps on every bulk endpoint (PLAN §7.4: <=500 items/request).

Real payload field names + minimal valid items per endpoint (verified
against `apps/api/app/schemas/catalog.py`, `matches.py`,
`scrape_profiles.py` and their routers) -- NOT the `{"items": [...]}`
placeholder from the task brief:

- `POST /v1/products/bulk-upsert`        -> `{"products": [...]}`,       item: `{"title": ...}`
- `POST /v1/variants/bulk-upsert`        -> `{"variants": [...]}`,       item: `{"title", "price", "currency"}`
- `POST /v1/matches/bulk-upsert`         -> `{"matches": [...]}`,        item: `{"competitor_id", "competitor_url", "variant_sku"}`
- `POST /v1/scrape-profiles/bulk-upsert` -> `{"profiles": [...]}`,       item: `{"name": ...}`

Each item is only as complete as Pydantic's `model_config =
ConfigDict(extra="forbid")` schema requires -- request-body parsing
happens before the handler runs, so an under-schema item would 422 on
parsing rather than exercising the batch-size guard. The guard itself
must run before any per-item business-logic validation (e.g. products'
MISSING_PRICE check, matches' variant/competitor resolution), so an
over-cap request is refused cheaply.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.deps import Principal, get_current_principal
from app.limits import MAX_BULK_ITEMS
from app.main import app

from unit._jobs_fake_session import FakeOrmSession

WORKSPACE_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _authorize(scopes: list[str]) -> None:
    session = FakeOrmSession()

    def _dep() -> Iterator[tuple[FakeOrmSession, Principal]]:
        yield session, Principal(
            kind="api_key",
            id=uuid.uuid4(),
            role=None,
            scopes=scopes,
            workspace_id=WORKSPACE_ID,
        )

    app.dependency_overrides[get_current_principal] = _dep


def test_cap_is_500() -> None:
    assert MAX_BULK_ITEMS == 500


@pytest.mark.parametrize(
    ("path", "field", "scopes", "item"),
    [
        ("/v1/products/bulk-upsert", "products", ["products:write"], {"title": "x"}),
        (
            "/v1/variants/bulk-upsert",
            "variants",
            ["variants:write"],
            {"title": "x", "price": 10, "currency": "USD"},
        ),
        (
            "/v1/matches/bulk-upsert",
            "matches",
            ["matches:write"],
            {
                "competitor_id": str(uuid.uuid4()),
                "competitor_url": "https://e.com/p",
                "variant_sku": "sku-x",
            },
        ),
        (
            "/v1/scrape-profiles/bulk-upsert",
            "profiles",
            ["scrape_profiles:write"],
            {"name": "x"},
        ),
    ],
)
def test_over_cap_is_rejected(client, path, field, scopes, item):
    _authorize(scopes)
    body = {field: [item] * (MAX_BULK_ITEMS + 1)}
    resp = client.post(path, json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "BATCH_TOO_LARGE"


@pytest.mark.parametrize(
    ("path", "field", "scopes", "item", "expected_code"),
    [
        # Each item is deliberately chosen to fail *past* the cap guard on
        # a cheap, DB-free path (no parent/variant reference supplied, so
        # the router's own pre-upsert business-logic check fires first) --
        # `FakeOrmSession` (tests/unit/_jobs_fake_session.py) only
        # evaluates `Select` statements, so a request that reached this
        # router's real `INSERT ... ON CONFLICT` would blow up on the fake
        # for unrelated reasons. What matters here is exclusively that the
        # response is NOT `BATCH_TOO_LARGE` at exactly the cap.
        ("/v1/products/bulk-upsert", "products", ["products:write"], {"title": "x"}, "MISSING_PRICE"),
        (
            "/v1/variants/bulk-upsert",
            "variants",
            ["variants:write"],
            {"title": "x", "price": 10, "currency": "USD"},
            "UNRESOLVED_PARENT",
        ),
        (
            "/v1/matches/bulk-upsert",
            "matches",
            ["matches:write"],
            {
                "competitor_id": str(uuid.uuid4()),
                "competitor_url": "https://competitor.com/p",
                "variant_sku": "sku-x",
            },
            "UNRESOLVED_VARIANT",
        ),
    ],
)
def test_exactly_at_cap_is_not_rejected_for_being_too_large(
    client, path, field, scopes, item, expected_code
):
    """500 items must clear the batch-size guard -- the response is driven
    entirely by downstream business logic (never `BATCH_TOO_LARGE`)."""
    _authorize(scopes)
    body = {field: [item] * MAX_BULK_ITEMS}
    resp = client.post(path, json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == expected_code


def test_scrape_profiles_exactly_at_cap_is_not_rejected_for_being_too_large(client):
    """Same guarantee for scrape-profiles, via an all-rows-rejected (not
    cap-rejected) path: every row fails `validate_profile` on an
    uncompilable `price_regex`, so `prepare_profiles` returns an empty
    `valid` list and the handler returns before any upsert statement."""
    _authorize(["scrape_profiles:write"])
    item = {"name": "x", "price_regex": "("}
    body = {"profiles": [item] * MAX_BULK_ITEMS}
    resp = client.post("/v1/scrape-profiles/bulk-upsert", json=body)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["upserted"] == 0
    assert len(payload["rejected"]) == MAX_BULK_ITEMS
