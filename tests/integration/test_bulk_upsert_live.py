"""Live-Postgres bulk-upsert idempotency test (SPEC-04 US2 T028, SC-002) -- DEFERRED.

Exercises `POST /v1/products/bulk-upsert` (and, for the standalone path,
`POST /v1/variants/bulk-upsert`) against a real database through
FastAPI's `TestClient` (no running server/container required -- only
the database needs to be live):

1. Bulk-upsert a batch of products (with nested variants) -> all
   created, each ending with >=1 variant (FR-012 tail).
2. Re-push the *same* payload unchanged -> 0 duplicate rows (matched by
   `external_id` -> `sku` -> `(product_id, title)`, FR-011); row counts
   stay identical.
3. Re-push with a changed field (e.g. `title`/price) -> the matched row
   is updated in place, not duplicated.
4. Two records in the *same* batch sharing an identity -> last-wins
   (FR-012), no error, no duplicate row.
5. A product upserted with zero explicit `variants` gets exactly one
   default variant.

Needs a reachable Postgres instance with `DATABASE_URL` (app role, RLS
enforced) usable AND the SPEC-04 migration already applied
(`alembic upgrade head`). Not runnable in the no-Docker-daemon build
environment used to author this feature -- SKIPS cleanly whenever
`Settings`/`DATABASE_URL` isn't usable, a real connection attempt
fails, or the `products`/`product_variants` tables don't exist yet.

Author now; leave unchecked (DEFERRED -- needs a Postgres-capable host
with the SPEC-04 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest


def _live_catalog_reachable() -> bool:
    """Best-effort probe: True only if Postgres is reachable AND the
    SPEC-04 catalog tables already exist (migration applied)."""
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
        if not {"products", "product_variants"} <= table_names:
            return False
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_catalog_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-04 catalog "
        "migration applied in this environment"
    ),
)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture()
def workspace_and_api_key() -> Iterator[dict[str, str]]:
    """A fresh ACTIVE workspace + a full-scoped catalog API key, cleaned up after."""
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.security.api_keys import generate_api_key

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        workspace = Workspace(
            name=f"Bulk Upsert Live Test {unique}",
            slug=f"bulk-upsert-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id

        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=workspace_id,
            name="bulk-upsert-live-test-key",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=["products:read", "products:write", "variants:read", "variants:write"],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add(api_key)
        session.commit()

    yield {"workspace_id": str(workspace_id), "api_key": full_secret}

    with get_session() as session:
        from sqlalchemy import text

        session.execute(
            text("DELETE FROM product_variants WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.execute(
            text("DELETE FROM products WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(text("DELETE FROM api_keys WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        session.commit()


@pytest.fixture()
def auth_headers(workspace_and_api_key: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {workspace_and_api_key['api_key']}"}


# --- SC-002: insert batch -> all created, each product ends with >=1 variant --


def test_bulk_upsert_creates_all_products_with_default_variant(client, auth_headers) -> None:
    payload = {
        "products": [
            {"external_id": "WOO-1", "title": "Widget One", "price": "9.9900", "currency": "USD"},
            {
                "external_id": "WOO-2",
                "title": "Widget Two",
                "variants": [
                    {"title": "Small", "price": "5.0000", "currency": "USD"},
                    {"title": "Large", "price": "7.0000", "currency": "USD"},
                ],
            },
        ]
    }
    response = client.post("/v1/products/bulk-upsert", headers=auth_headers, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["upserted"] == 2

    by_external_id = {p["external_id"]: p for p in body["products"]}
    assert len(by_external_id["WOO-1"]["variants"]) == 1
    assert len(by_external_id["WOO-2"]["variants"]) == 2


# --- SC-002: re-push unchanged -> 0 duplicates ---------------------------------


def test_bulk_upsert_repush_unchanged_creates_no_duplicates(client, auth_headers) -> None:
    payload = {
        "products": [
            {
                "external_id": "IDEMPOTENT-1",
                "title": "Stable Widget",
                "price": "12.5000",
                "currency": "USD",
            }
        ]
    }
    first = client.post("/v1/products/bulk-upsert", headers=auth_headers, json=payload)
    assert first.status_code == 200
    first_id = first.json()["products"][0]["id"]

    second = client.post("/v1/products/bulk-upsert", headers=auth_headers, json=payload)
    assert second.status_code == 200
    second_id = second.json()["products"][0]["id"]

    assert first_id == second_id

    listing = client.get("/v1/products", headers=auth_headers)
    matching = [p for p in listing.json()["items"] if p["external_id"] == "IDEMPOTENT-1"]
    assert len(matching) == 1


# --- SC-002: re-push changed -> in-place update --------------------------------


def test_bulk_upsert_repush_changed_updates_in_place(client, auth_headers) -> None:
    original = {
        "products": [
            {
                "external_id": "UPDATE-ME",
                "title": "Original Title",
                "price": "1.0000",
                "currency": "USD",
            }
        ]
    }
    first = client.post("/v1/products/bulk-upsert", headers=auth_headers, json=original)
    assert first.status_code == 200
    product_id = first.json()["products"][0]["id"]

    updated = {
        "products": [
            {
                "external_id": "UPDATE-ME",
                "title": "New Title",
                "price": "2.0000",
                "currency": "USD",
            }
        ]
    }
    second = client.post("/v1/products/bulk-upsert", headers=auth_headers, json=updated)
    assert second.status_code == 200
    second_product = second.json()["products"][0]
    assert second_product["id"] == product_id
    assert second_product["title"] == "New Title"


# --- SC-002 / FR-012: in-batch same-identity -> last-wins ----------------------


def test_bulk_upsert_in_batch_same_identity_resolves_last_wins(client, auth_headers) -> None:
    payload = {
        "products": [
            {
                "external_id": "DUP-1",
                "title": "First Version",
                "price": "1.0000",
                "currency": "USD",
            },
            {
                "external_id": "DUP-1",
                "title": "Second Version",
                "price": "2.0000",
                "currency": "USD",
            },
        ]
    }
    response = client.post("/v1/products/bulk-upsert", headers=auth_headers, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["upserted"] == 1
    assert body["products"][0]["title"] == "Second Version"

    listing = client.get("/v1/products", headers=auth_headers)
    matching = [p for p in listing.json()["items"] if p["external_id"] == "DUP-1"]
    assert len(matching) == 1
