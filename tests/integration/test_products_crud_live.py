"""Live-Postgres product/variant CRUD test (SPEC-04 US1 T020, SC-001) — ⏸ DEFERRED.

Exercises the full `/v1/products` + `/v1/variants` surface against a real
database through FastAPI's `TestClient` (no running server/container
required — only the database needs to be live):

1. Create a simple product (no explicit `variants`) -> exactly one
   default variant persisted, inheriting price/currency/sku/url from the
   product-create payload (FR-005, SC-001).
2. Create a product with N explicit variants -> N variants persisted, 0
   spurious default (FR-005).
3. Read / update / list persistence, workspace-scoped.
4. Delete returns `{"outcome": "hard_deleted"}` (FR-017) and a
   subsequent read 404s.
5. The last-variant invariant (FR-006): a product's variant count never
   drops to zero through the surface exercised here (no variant-DELETE
   endpoint exists in this feature -- see `contracts/api-variants.md`
   [analyze F2] -- so this is exercised only via the create paths above,
   never via a runtime delete-to-zero check).

Needs a reachable Postgres instance with `DATABASE_URL` (app role, RLS
enforced) usable AND the SPEC-04 migration already applied
(`alembic upgrade head`). Not runnable in the no-Docker-daemon build
environment used to author this feature -- SKIPS cleanly whenever
`Settings`/`DATABASE_URL` isn't usable, a real connection attempt fails,
or the `products`/`product_variants` tables don't exist yet.

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
            name=f"Catalog Live Test {unique}",
            slug=f"catalog-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id

        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=workspace_id,
            name="catalog-live-test-key",
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


# --- SC-001: simple product -> exactly one default variant -----------------


def test_create_simple_product_yields_one_default_variant(client, auth_headers) -> None:
    response = client.post(
        "/v1/products",
        headers=auth_headers,
        json={
            "title": "Acme Widget",
            "sku": "ACME-1",
            "url": "https://example.com/acme-1",
            "price": "19.9900",
            "currency": "USD",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert len(body["variants"]) == 1
    variant = body["variants"][0]
    assert variant["title"] == "Acme Widget"
    assert variant["sku"] == "ACME-1"
    assert variant["url"] == "https://example.com/acme-1"
    assert Decimal(variant["current_price"]) == Decimal("19.9900")
    assert variant["currency"] == "USD"


def test_create_simple_product_without_price_is_422(client, auth_headers) -> None:
    response = client.post(
        "/v1/products", headers=auth_headers, json={"title": "No Price Widget"}
    )
    assert response.status_code == 422


# --- explicit variants -> N variants, 0 spurious default -------------------


def test_create_product_with_explicit_variants_yields_exactly_those(client, auth_headers) -> None:
    response = client.post(
        "/v1/products",
        headers=auth_headers,
        json={
            "title": "Multi Widget",
            "variants": [
                {"title": "Red", "price": "10.0000", "currency": "USD"},
                {"title": "Blue", "price": "11.0000", "currency": "USD"},
                {"title": "Green", "price": "12.0000", "currency": "USD"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert len(body["variants"]) == 3
    titles = {v["title"] for v in body["variants"]}
    assert titles == {"Red", "Blue", "Green"}


# --- read / update / list / delete ------------------------------------------


def test_read_update_list_delete_round_trip(client, auth_headers) -> None:
    create = client.post(
        "/v1/products",
        headers=auth_headers,
        json={"title": "Roundtrip Widget", "price": "5.0000", "currency": "USD"},
    )
    assert create.status_code == 201
    product_id = create.json()["id"]

    # Read.
    read = client.get(f"/v1/products/{product_id}", headers=auth_headers)
    assert read.status_code == 200
    assert read.json()["title"] == "Roundtrip Widget"

    # Update.
    update = client.patch(
        f"/v1/products/{product_id}", headers=auth_headers, json={"title": "Updated Widget"}
    )
    assert update.status_code == 200
    assert update.json()["title"] == "Updated Widget"

    # List includes it.
    listing = client.get("/v1/products", headers=auth_headers)
    assert listing.status_code == 200
    assert any(item["id"] == product_id for item in listing.json()["items"])

    # Delete.
    delete = client.delete(f"/v1/products/{product_id}", headers=auth_headers)
    assert delete.status_code == 200
    assert delete.json() == {"id": product_id, "outcome": "hard_deleted"}

    # Subsequent read 404s.
    after = client.get(f"/v1/products/{product_id}", headers=auth_headers)
    assert after.status_code == 404


def test_variant_read_and_patch_persist(client, auth_headers) -> None:
    create = client.post(
        "/v1/products",
        headers=auth_headers,
        json={"title": "Variant Widget", "price": "7.5000", "currency": "USD"},
    )
    assert create.status_code == 201
    variant_id = create.json()["variants"][0]["id"]

    read = client.get(f"/v1/variants/{variant_id}", headers=auth_headers)
    assert read.status_code == 200

    patch = client.patch(
        f"/v1/variants/{variant_id}",
        headers=auth_headers,
        json={"price": "8.2500"},
    )
    assert patch.status_code == 200
    assert Decimal(patch.json()["current_price"]) == Decimal("8.2500")
