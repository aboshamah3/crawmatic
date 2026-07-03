"""Live cross-workspace catalog isolation + scope-gating test (SPEC-04 US4
T031, FR-003/FR-016, SC-004/SC-005) — DEFERRED.

Combines the two patterns already used in this suite:
`tests/integration/test_products_crud_live.py` (TestClient + API-key auth
against `DATABASE_URL`) and `tests/integration/test_rls_cross_workspace.py`
(raw-engine `app.workspace_id` `set_config` probes proving RLS holds even
when the app-layer `WHERE workspace_id = ...` predicate is missing).

Proves, on the catalog tables:

1. Workspace-A's caller (a `products:read`/`write`-scoped API key)
   sees **0** of workspace-B's products — by id (`GET /v1/products/{id}`
   -> 404) and in a list (`GET /v1/products` never includes it).
2. A deliberately app-**unscoped** raw query (no `WHERE workspace_id =
   ...` at all) with `app.workspace_id` set to workspace A still returns
   0 of workspace B's rows — RLS alone enforces isolation (FR-003).
3. With **no** `app.workspace_id` context set at all, the same query
   returns 0 rows for either workspace's seeded product (fail closed).
4. A `products:read`-only API key cannot `POST /v1/products` (403); a
   `products:write`-scoped key can (201) (FR-016, SC-005).
5. A composite-FK cross-workspace reference (a `product_variants` row
   naming a `product_id` that belongs to a *different* workspace) is
   rejected at the DB — impossible, not merely app-checked (FR-003,
   `contracts/workspace-consistency.md` Layer 1).

Needs a reachable Postgres with `DATABASE_URL` (app role, RLS enforced),
`AUTH_DATABASE_URL` (bypassRLS auth role, unused directly here but
probed for parity with the SPEC-03 live test), and
`MIGRATION_DATABASE_URL` (privileged seeding), all with the SPEC-04
catalog migration already applied. Not runnable in the no-Docker-daemon
build environment used to author this feature — SKIPS cleanly whenever
any of those URLs is unset/unreachable or the catalog tables don't
exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-04 migration applied).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


def _auth_database_url() -> str | None:
    return os.environ.get("AUTH_DATABASE_URL")


def _migration_database_url() -> str | None:
    return os.environ.get("MIGRATION_DATABASE_URL")


def _all_reachable_with_catalog() -> bool:
    """Best-effort probe: True only if all three URLs are set, connectable,
    AND the SPEC-04 catalog tables already exist (migration applied)."""
    urls = (_database_url(), _auth_database_url(), _migration_database_url())
    if not all(urls):
        return False
    try:
        for url in urls:
            engine = create_engine(url)
            with engine.connect():
                pass
            engine.dispose()

        from sqlalchemy import inspect

        engine = create_engine(_database_url())
        table_names = set(inspect(engine).get_table_names())
        engine.dispose()
        if not {"products", "product_variants"} <= table_names:
            return False
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _all_reachable_with_catalog(),
    reason=(
        "Needs reachable DATABASE_URL (app role, no BYPASSRLS) + "
        "AUTH_DATABASE_URL + MIGRATION_DATABASE_URL, with the SPEC-04 "
        "catalog migration already applied -- not available in this "
        "environment."
    ),
)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture()
def app_engine() -> Iterator[Engine]:
    engine = create_engine(_database_url())
    yield engine
    engine.dispose()


@pytest.fixture()
def two_workspaces_with_products() -> Iterator[dict[str, object]]:
    """Seed two workspaces, one product+variant each, and per-workspace-A
    read-only / write-scoped API keys, via the privileged migration engine."""
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, ProductStatus, VariantStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.models.catalog import Product, ProductVariant
    from app_shared.security.api_keys import generate_api_key

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        ws_a = Workspace(
            name=f"Isolation A {unique}", slug=f"isolation-a-{unique}", status=WorkspaceStatus.ACTIVE
        )
        ws_b = Workspace(
            name=f"Isolation B {unique}", slug=f"isolation-b-{unique}", status=WorkspaceStatus.ACTIVE
        )
        session.add_all([ws_a, ws_b])
        session.flush()

        product_a = Product(workspace_id=ws_a.id, title="Product A", status=ProductStatus.ACTIVE)
        product_b = Product(workspace_id=ws_b.id, title="Product B", status=ProductStatus.ACTIVE)
        session.add_all([product_a, product_b])
        session.flush()

        variant_a = ProductVariant(
            workspace_id=ws_a.id,
            product_id=product_a.id,
            title="Product A",
            current_price=Decimal("9.9900"),
            currency="USD",
            status=VariantStatus.ACTIVE,
        )
        variant_b = ProductVariant(
            workspace_id=ws_b.id,
            product_id=product_b.id,
            title="Product B",
            current_price=Decimal("9.9900"),
            currency="USD",
            status=VariantStatus.ACTIVE,
        )
        session.add_all([variant_a, variant_b])
        session.flush()

        write_secret_a, write_prefix_a, write_hash_a = generate_api_key()
        write_key_a = ApiKey(
            workspace_id=ws_a.id,
            name="isolation-write-a",
            key_prefix=write_prefix_a,
            key_hash=write_hash_a,
            scopes=["products:read", "products:write", "variants:read", "variants:write"],
            status=ApiKeyStatus.ACTIVE,
        )
        read_secret_a, read_prefix_a, read_hash_a = generate_api_key()
        read_key_a = ApiKey(
            workspace_id=ws_a.id,
            name="isolation-read-a",
            key_prefix=read_prefix_a,
            key_hash=read_hash_a,
            scopes=["products:read", "variants:read"],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add_all([write_key_a, read_key_a])
        session.commit()

        ids = {
            "workspace_a_id": ws_a.id,
            "workspace_b_id": ws_b.id,
            "product_a_id": product_a.id,
            "product_b_id": product_b.id,
            "write_api_key_a": write_secret_a,
            "read_api_key_a": read_secret_a,
        }

    try:
        yield ids
    finally:
        with get_session() as session:
            for ws in (ids["workspace_a_id"], ids["workspace_b_id"]):
                session.execute(
                    text("DELETE FROM product_variants WHERE workspace_id = :ws"), {"ws": ws}
                )
                session.execute(text("DELETE FROM products WHERE workspace_id = :ws"), {"ws": ws})
                session.execute(text("DELETE FROM api_keys WHERE workspace_id = :ws"), {"ws": ws})
                session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
            session.commit()


# --- SC-004: workspace-A caller sees 0 of workspace-B's rows ----------------


def test_workspace_a_caller_gets_404_for_workspace_b_product_by_id(
    client, two_workspaces_with_products: dict[str, object]
) -> None:
    headers = {"Authorization": f"Bearer {two_workspaces_with_products['write_api_key_a']}"}
    resp = client.get(
        f"/v1/products/{two_workspaces_with_products['product_b_id']}", headers=headers
    )
    assert resp.status_code == 404


def test_workspace_a_caller_list_never_includes_workspace_b_product(
    client, two_workspaces_with_products: dict[str, object]
) -> None:
    headers = {"Authorization": f"Bearer {two_workspaces_with_products['write_api_key_a']}"}
    resp = client.get("/v1/products", headers=headers)
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(two_workspaces_with_products["product_a_id"]) in ids
    assert str(two_workspaces_with_products["product_b_id"]) not in ids


# --- FR-003: app-filter-omitted query still returns 0 other-ws rows (RLS) ---


def test_app_filter_omitted_query_returns_zero_other_workspace_rows_via_rls(
    two_workspaces_with_products: dict[str, object], app_engine: Engine
) -> None:
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": str(two_workspaces_with_products["workspace_a_id"])},
        )
        # Deliberately app-unscoped -- no WHERE workspace_id = ... at all;
        # RLS is the only thing standing between this query and workspace B's row.
        rows = conn.execute(text("SELECT id FROM products")).fetchall()

    ids = {row[0] for row in rows}
    assert two_workspaces_with_products["product_a_id"] in ids
    assert two_workspaces_with_products["product_b_id"] not in ids


# --- FR-003: no context at all -> 0 rows, fail closed ------------------------


def test_no_workspace_context_returns_zero_rows_fail_closed(
    two_workspaces_with_products: dict[str, object], app_engine: Engine
) -> None:
    with app_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id FROM products WHERE id IN (:a, :b)"),
            {
                "a": str(two_workspaces_with_products["product_a_id"]),
                "b": str(two_workspaces_with_products["product_b_id"]),
            },
        ).fetchall()

    assert rows == []


# --- SC-005: read-only credential can't write; write credential can --------


def test_read_only_scoped_credential_cannot_create_product(
    client, two_workspaces_with_products: dict[str, object]
) -> None:
    headers = {"Authorization": f"Bearer {two_workspaces_with_products['read_api_key_a']}"}
    resp = client.post(
        "/v1/products",
        headers=headers,
        json={"title": "Should Be Refused", "price": "1.0000", "currency": "USD"},
    )
    assert resp.status_code == 403


def test_write_scoped_credential_can_create_product(
    client, two_workspaces_with_products: dict[str, object]
) -> None:
    headers = {"Authorization": f"Bearer {two_workspaces_with_products['write_api_key_a']}"}
    resp = client.post(
        "/v1/products",
        headers=headers,
        json={"title": "Should Succeed", "price": "1.0000", "currency": "USD"},
    )
    assert resp.status_code == 201


# --- FR-003: composite-FK cross-workspace reference rejected ----------------


def test_composite_fk_rejects_cross_workspace_variant_parent_reference(
    two_workspaces_with_products: dict[str, object], app_engine: Engine
) -> None:
    """A `product_variants` row naming workspace B's id but a `product_id`
    that belongs to workspace A is structurally impossible — the
    composite FK `(workspace_id, product_id) -> products(workspace_id,
    id)` rejects it at the DB (never reaches app-layer validation)."""
    with pytest.raises((IntegrityError, DBAPIError)):
        with app_engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.workspace_id', :w, true)"),
                {"w": str(two_workspaces_with_products["workspace_b_id"])},
            )
            conn.execute(
                text(
                    "INSERT INTO product_variants "
                    "(id, workspace_id, product_id, title, current_price, "
                    "currency, status, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :ws, :product_id, 'Cross-WS Variant', "
                    "9.99, 'USD', 'active', now(), now())"
                ),
                {
                    "ws": str(two_workspaces_with_products["workspace_b_id"]),
                    "product_id": str(two_workspaces_with_products["product_a_id"]),
                },
            )
