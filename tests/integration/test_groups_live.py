"""Live-Postgres product-groups test (SPEC-04 US3 T035, SC-008) — DEFERRED.

Exercises the full `/v1/product-groups` surface against a real database
through FastAPI's `TestClient` (no running server/container required —
only the database needs to be live), mirroring
`tests/integration/test_products_crud_live.py`'s fixture pattern:

1. Create a group + add a product item and a variant item -> both
   listed as members (FR-013).
2. Re-adding the same product/variant item to the same group is
   rejected (409, duplicate membership — the partial-unique index on
   `(workspace_id, product_group_id, product_id)` /
   `(workspace_id, product_group_id, product_variant_id)`).
3. Removing an item drops it from the group's member list.
4. A cross-workspace item reference (a product that exists, but in a
   *different* workspace) is rejected (422) — workspace-local
   resolution via `app_shared.catalog.consistency`
   (`contracts/workspace-consistency.md`).

Needs a reachable Postgres instance with `DATABASE_URL` (app role, RLS
enforced) usable AND the SPEC-04 migration already applied (`alembic
upgrade head`). Not runnable in the no-Docker-daemon build environment
used to author this feature — SKIPS cleanly whenever
`Settings`/`DATABASE_URL` isn't usable, a real connection attempt fails,
or the catalog tables don't exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-04 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest


def _live_catalog_reachable() -> bool:
    """Best-effort probe: True only if Postgres is reachable AND the
    SPEC-04 catalog tables (including groups) already exist (migration applied)."""
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
        if not {"products", "product_variants", "product_groups", "product_group_items"} <= table_names:
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
            name=f"Groups Live Test {unique}",
            slug=f"groups-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id

        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=workspace_id,
            name="groups-live-test-key",
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
            text("DELETE FROM product_group_items WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(
            text("DELETE FROM product_groups WHERE workspace_id = :ws"), {"ws": workspace_id}
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
def other_workspace_product() -> Iterator[dict[str, str]]:
    """A product living in a *second*, unrelated workspace — used to prove
    cross-workspace group-item references are rejected."""
    from app_shared.database import get_session
    from app_shared.enums import ProductStatus, WorkspaceStatus
    from app_shared.models import Workspace
    from app_shared.models.catalog import Product

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        other_workspace = Workspace(
            name=f"Groups Live Other {unique}",
            slug=f"groups-live-other-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(other_workspace)
        session.flush()

        other_product = Product(
            workspace_id=other_workspace.id, title="Other-WS Product", status=ProductStatus.ACTIVE
        )
        session.add(other_product)
        session.commit()

        ids = {"workspace_id": other_workspace.id, "product_id": other_product.id}

    yield {"product_id": str(ids["product_id"])}

    with get_session() as session:
        from sqlalchemy import text

        session.execute(
            text("DELETE FROM products WHERE workspace_id = :ws"), {"ws": ids["workspace_id"]}
        )
        session.execute(
            text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ids["workspace_id"]}
        )
        session.commit()


def _create_product_with_variant(client, auth_headers) -> dict[str, str]:
    response = client.post(
        "/v1/products",
        headers=auth_headers,
        json={"title": "Grouped Widget", "price": "3.0000", "currency": "USD"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {"product_id": body["id"], "variant_id": body["variants"][0]["id"]}


# --- create group + add product/variant items -------------------------------


def test_create_group_and_add_product_and_variant_items(client, auth_headers) -> None:
    product = _create_product_with_variant(client, auth_headers)

    group_resp = client.post(
        "/v1/product-groups", headers=auth_headers, json={"name": "Featured"}
    )
    assert group_resp.status_code == 201, group_resp.text
    group_id = group_resp.json()["id"]

    add_product = client.post(
        f"/v1/product-groups/{group_id}/items",
        headers=auth_headers,
        json={"product_id": product["product_id"]},
    )
    assert add_product.status_code == 201, add_product.text

    add_variant = client.post(
        f"/v1/product-groups/{group_id}/items",
        headers=auth_headers,
        json={"product_variant_id": product["variant_id"]},
    )
    assert add_variant.status_code == 201, add_variant.text

    read = client.get(f"/v1/product-groups/{group_id}", headers=auth_headers)
    assert read.status_code == 200
    items = read.json()["items"]
    assert len(items) == 2
    member_product_ids = {i["product_id"] for i in items if i["product_id"]}
    member_variant_ids = {i["product_variant_id"] for i in items if i["product_variant_id"]}
    assert product["product_id"] in member_product_ids
    assert product["variant_id"] in member_variant_ids


# --- re-adding the same item -> duplicate rejected (409) --------------------


def test_readding_same_product_item_is_rejected_as_duplicate(client, auth_headers) -> None:
    product = _create_product_with_variant(client, auth_headers)
    group_resp = client.post(
        "/v1/product-groups", headers=auth_headers, json={"name": "Dup Test"}
    )
    group_id = group_resp.json()["id"]

    first = client.post(
        f"/v1/product-groups/{group_id}/items",
        headers=auth_headers,
        json={"product_id": product["product_id"]},
    )
    assert first.status_code == 201

    second = client.post(
        f"/v1/product-groups/{group_id}/items",
        headers=auth_headers,
        json={"product_id": product["product_id"]},
    )
    assert second.status_code == 409


# --- remove item --------------------------------------------------------------


def test_remove_item_drops_it_from_group(client, auth_headers) -> None:
    product = _create_product_with_variant(client, auth_headers)
    group_resp = client.post(
        "/v1/product-groups", headers=auth_headers, json={"name": "Removable"}
    )
    group_id = group_resp.json()["id"]

    add = client.post(
        f"/v1/product-groups/{group_id}/items",
        headers=auth_headers,
        json={"product_id": product["product_id"]},
    )
    assert add.status_code == 201
    item_id = add.json()["id"]

    remove = client.delete(
        f"/v1/product-groups/{group_id}/items/{item_id}", headers=auth_headers
    )
    assert remove.status_code == 204

    read = client.get(f"/v1/product-groups/{group_id}", headers=auth_headers)
    assert read.json()["items"] == []


# --- references are workspace-local ------------------------------------------


def test_cross_workspace_item_reference_is_rejected(
    client, auth_headers, other_workspace_product: dict[str, str]
) -> None:
    group_resp = client.post(
        "/v1/product-groups", headers=auth_headers, json={"name": "Cross WS"}
    )
    group_id = group_resp.json()["id"]

    resp = client.post(
        f"/v1/product-groups/{group_id}/items",
        headers=auth_headers,
        json={"product_id": other_workspace_product["product_id"]},
    )
    assert resp.status_code == 422


def test_nonexistent_item_reference_is_rejected(client, auth_headers) -> None:
    group_resp = client.post(
        "/v1/product-groups", headers=auth_headers, json={"name": "Nonexistent Ref"}
    )
    group_id = group_resp.json()["id"]

    resp = client.post(
        f"/v1/product-groups/{group_id}/items",
        headers=auth_headers,
        json={"product_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404
