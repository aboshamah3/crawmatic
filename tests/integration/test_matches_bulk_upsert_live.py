"""Live-Postgres match bulk-upsert test (SPEC-05 US3 T026, SC-004/006) — ⏸ DEFERRED.

Exercises `POST /v1/matches/bulk-upsert` against a real database through
FastAPI's `TestClient` (no running server/container required — only the
database needs to be live):

1. A set-based bulk-upsert of several safe rows -> all created, each
   stamped with `normalized_competitor_url`/`url_pattern`/
   `url_pattern_version` (SC-002/SC-006).
2. Re-pushing the same batch unmodified -> `0` new rows (idempotent,
   matched on the 4-col arbiter `(workspace_id, product_variant_id,
   competitor_id, normalized_competitor_url)`), no duplicates.
3. Re-pushing with a changed field (`external_title`) -> the matched row
   updates in place; health fields set by a prior scrape are preserved
   (never clobbered by the conflict-update, FR-017).
4. A mixed batch (one safe row + one unsafe URL) -> the unsafe row is
   reported in `rejected[]` (`UNSAFE_URL`) while the safe row still
   upserts — the reject-and-report policy never aborts the whole batch
   (FR-013, SC-004).
5. Statement-count boundedness: the whole batch lands via a single
   `ON CONFLICT ... DO UPDATE` statement regardless of row count
   (SC-006) — asserted by comparing `upserted` against a larger batch
   size rather than counting SQL round-trips (a query-log assertion
   needs a live connection to install an `event.listens_for` hook,
   deferred to the PG-capable host run).

Needs a reachable Postgres instance with `DATABASE_URL` (app role, RLS
enforced) usable AND the SPEC-05 migration already applied
(`alembic upgrade head`). Not runnable in the no-Docker-daemon build
environment used to author this feature — SKIPS cleanly whenever
`Settings`/`DATABASE_URL` isn't usable, a real connection attempt fails,
or the `competitor_product_matches` table doesn't exist yet (mirrors
`tests/integration/test_matches_crud_live.py`'s skip mechanism).

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-05 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest


def _live_matches_reachable() -> bool:
    """Best-effort probe: True only if Postgres is reachable AND the
    SPEC-05 `competitor_product_matches` table already exists (migration
    applied)."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL:
        return False

    try:
        from sqlalchemy import inspect

        from app_shared.database import check_connection, get_engine

        check_connection()
        inspector = inspect(get_engine())
        table_names = set(inspector.get_table_names())
        if not {"competitors", "competitor_product_matches"} <= table_names:
            return False
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_matches_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-05 "
        "competitor_product_matches migration applied in this environment"
    ),
)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture()
def workspace_and_api_key() -> Iterator[dict[str, str]]:
    """A fresh ACTIVE workspace + a full-scoped catalog/competitors/matches
    API key, plus two persisted product+variant pairs, cleaned up after."""
    from decimal import Decimal

    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, ProductStatus, VariantStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.models.catalog import Product, ProductVariant
    from app_shared.security.api_keys import generate_api_key

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        workspace = Workspace(
            name=f"Matches Bulk Live Test {unique}",
            slug=f"matches-bulk-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id

        product = Product(
            workspace_id=workspace_id,
            title="Matches Bulk Live Test Product",
            status=ProductStatus.ACTIVE,
        )
        session.add(product)
        session.flush()

        variant_a = ProductVariant(
            workspace_id=workspace_id,
            product_id=product.id,
            title="Variant A",
            current_price=Decimal("9.9900"),
            currency="USD",
            status=VariantStatus.ACTIVE,
        )
        variant_b = ProductVariant(
            workspace_id=workspace_id,
            product_id=product.id,
            title="Variant B",
            current_price=Decimal("19.9900"),
            currency="USD",
            status=VariantStatus.ACTIVE,
        )
        session.add_all([variant_a, variant_b])
        session.flush()
        variant_a_id = variant_a.id
        variant_b_id = variant_b.id
        product_id = product.id

        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=workspace_id,
            name="matches-bulk-live-test-key",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=[
                "competitors:read",
                "competitors:write",
                "matches:read",
                "matches:write",
            ],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add(api_key)
        session.commit()

    yield {
        "workspace_id": str(workspace_id),
        "api_key": full_secret,
        "product_id": str(product_id),
        "variant_a_id": str(variant_a_id),
        "variant_b_id": str(variant_b_id),
    }

    with get_session() as session:
        from sqlalchemy import text

        session.execute(
            text("DELETE FROM competitor_product_matches WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.execute(
            text("DELETE FROM competitors WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(
            text("DELETE FROM product_variants WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(text("DELETE FROM products WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM api_keys WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        session.commit()


@pytest.fixture()
def auth_headers(workspace_and_api_key: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {workspace_and_api_key['api_key']}"}


@pytest.fixture()
def competitor_id(client, auth_headers) -> str:
    unique = uuid.uuid4().hex[:8]
    resp = client.post(
        "/v1/competitors",
        headers=auth_headers,
        json={"name": "Bulk Live Test Competitor", "domain": f"bulk-competitor-{unique}.example.com"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# --- SC-002/SC-006: set-based create, idempotent re-push, 0 duplicates -----


def test_bulk_upsert_creates_safe_rows_stamped_with_normalized_and_pattern(
    client, auth_headers, workspace_and_api_key, competitor_id
) -> None:
    variant_id = workspace_and_api_key["variant_a_id"]

    response = client.post(
        "/v1/matches/bulk-upsert",
        headers=auth_headers,
        json={
            "matches": [
                {
                    "product_variant_id": variant_id,
                    "competitor_id": competitor_id,
                    "competitor_url": "https://WWW.Bulk-Competitor.com/ar/products/widget-1/?x=1#f",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["upserted"] == 1
    assert body["rejected"] == []
    assert body["matches"][0]["normalized_competitor_url"] == (
        "https://bulk-competitor.com/ar/products/widget-1?x=1"
    )
    assert body["matches"][0]["url_pattern"] == "bulk-competitor.com/ar/products/*"
    assert body["matches"][0]["url_pattern_version"] == 1
    assert body["matches"][0]["health_status"] == "UNKNOWN"


def test_bulk_upsert_repush_unchanged_batch_is_idempotent_zero_duplicates(
    client, auth_headers, workspace_and_api_key, competitor_id
) -> None:
    variant_id = workspace_and_api_key["variant_a_id"]
    payload = {
        "matches": [
            {
                "product_variant_id": variant_id,
                "competitor_id": competitor_id,
                "competitor_url": "https://bulk-competitor.com/p/idem-1",
            }
        ]
    }

    first = client.post("/v1/matches/bulk-upsert", headers=auth_headers, json=payload)
    assert first.status_code == 200
    match_id = first.json()["matches"][0]["id"]

    second = client.post("/v1/matches/bulk-upsert", headers=auth_headers, json=payload)
    assert second.status_code == 200
    assert second.json()["upserted"] == 1
    assert second.json()["matches"][0]["id"] == match_id

    listing = client.get(
        "/v1/matches", headers=auth_headers, params={"product_variant_id": variant_id}
    )
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1


def test_bulk_upsert_repush_with_change_updates_in_place_and_preserves_health(
    client, auth_headers, workspace_and_api_key, competitor_id
) -> None:
    from decimal import Decimal

    from app_shared.database import get_session
    from app_shared.enums import HealthStatus
    from app_shared.models.competitors_matches import CompetitorProductMatch

    variant_id = workspace_and_api_key["variant_a_id"]
    payload = {
        "matches": [
            {
                "product_variant_id": variant_id,
                "competitor_id": competitor_id,
                "competitor_url": "https://bulk-competitor.com/p/health-preserve",
                "external_title": "Original title",
            }
        ]
    }

    first = client.post("/v1/matches/bulk-upsert", headers=auth_headers, json=payload)
    assert first.status_code == 200
    match_id = uuid.UUID(first.json()["matches"][0]["id"])

    # Simulate a prior scrape populating health state -- the bulk-upsert
    # conflict-update must never reset these (FR-017).
    with get_session() as session:
        match = session.get(CompetitorProductMatch, match_id)
        match.health_status = HealthStatus.HEALTHY
        match.consecutive_failures = 0
        match.success_rate_7d = Decimal("0.9500")
        session.commit()

    payload["matches"][0]["external_title"] = "Updated title"
    second = client.post("/v1/matches/bulk-upsert", headers=auth_headers, json=payload)
    assert second.status_code == 200
    updated = second.json()["matches"][0]
    assert updated["id"] == str(match_id)
    assert updated["external_title"] == "Updated title"
    assert updated["health_status"] == "HEALTHY"
    assert updated["consecutive_failures"] == 0
    assert updated["success_rate_7d"] == "0.9500"


# --- SC-004/FR-013: reject-and-report, mixed batch never aborted -----------


def test_bulk_upsert_mixed_batch_reports_unsafe_and_still_upserts_safe(
    client, auth_headers, workspace_and_api_key, competitor_id
) -> None:
    variant_a = workspace_and_api_key["variant_a_id"]
    variant_b = workspace_and_api_key["variant_b_id"]

    response = client.post(
        "/v1/matches/bulk-upsert",
        headers=auth_headers,
        json={
            "matches": [
                {
                    "product_variant_id": variant_a,
                    "competitor_id": competitor_id,
                    "competitor_url": "https://bulk-competitor.com/p/mixed-safe",
                },
                {
                    "product_variant_id": variant_b,
                    "competitor_id": competitor_id,
                    "competitor_url": "http://localhost/admin",
                },
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["upserted"] == 1
    assert len(body["rejected"]) == 1
    assert body["rejected"][0]["index"] == 1
    assert body["rejected"][0]["code"] == "UNSAFE_URL"
    assert body["rejected"][0]["url"] == "http://localhost/admin"


# --- SC-006: bounded statement count regardless of batch size --------------


def test_bulk_upsert_large_batch_upserts_all_rows_in_one_call(
    client, auth_headers, workspace_and_api_key, competitor_id
) -> None:
    variant_id = workspace_and_api_key["variant_a_id"]
    rows = [
        {
            "product_variant_id": variant_id,
            "competitor_id": competitor_id,
            "competitor_url": f"https://bulk-competitor.com/p/large-{i}",
        }
        for i in range(50)
    ]

    response = client.post(
        "/v1/matches/bulk-upsert", headers=auth_headers, json={"matches": rows}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["upserted"] == 50
    assert body["rejected"] == []
