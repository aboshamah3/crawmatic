"""Live cross-workspace competitors/matches isolation + scope-gating test
(SPEC-05 US4 T030, FR-002/FR-006, SC-004/SC-007/SC-008) — ⏸ DEFERRED.

Mirrors `tests/integration/test_workspace_isolation_live.py` (SPEC-04
US4 T031) exactly, substituting the competitors/matches surface for the
catalog one, combining the two patterns already used in this suite:
`tests/integration/test_matches_crud_live.py` /
`tests/integration/test_competitors_crud_live.py` (TestClient + API-key
auth against `DATABASE_URL`) and `tests/integration/test_rls_cross_workspace.py`
(raw-engine `app.workspace_id` `set_config` probes proving RLS holds even
when the app-layer `WHERE workspace_id = ...` predicate is missing).

Proves, on the competitors/matches tables:

1. Workspace-A's caller (a `competitors:*`/`matches:*`-scoped API key)
   sees **0** of workspace-B's competitors/matches — by id
   (`GET /v1/competitors/{id}` / `GET /v1/matches/{id}` -> 404) and in a
   list (`GET /v1/competitors` / `GET /v1/matches` never includes them).
2. A deliberately app-**unscoped** raw query (no `WHERE workspace_id =
   ...` at all) with `app.workspace_id` set to workspace A still returns
   0 of workspace B's rows for both tables — RLS alone enforces
   isolation (FR-002).
3. With **no** `app.workspace_id` context set at all, the same queries
   return 0 rows for either workspace's seeded competitor/match (fail
   closed, FR-002/SC-007).
4. A `competitors:read`/`matches:read`-only API key cannot
   `POST /v1/competitors` or `POST /v1/matches` (403); a
   `competitors:write`/`matches:write`-scoped key can (201) (FR-015,
   SC-008).
5. A composite-FK cross-workspace reference (a
   `competitor_product_matches` row naming workspace B but a
   `competitor_id`/`product_variant_id`/`product_id` that belongs to
   workspace A) is rejected at the DB — impossible, not merely
   app-checked (FR-006, `contracts/workspace-consistency.md` Layer 1).

Needs a reachable Postgres with `DATABASE_URL` (app role, RLS enforced),
`AUTH_DATABASE_URL` (bypassRLS auth role, unused directly here but
probed for parity with the SPEC-03/04 live tests), and
`MIGRATION_DATABASE_URL` (privileged seeding), all with the SPEC-05
competitors/matches migration already applied. Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever any of those URLs is unset/unreachable or the
competitors/matches tables don't exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-05 migration applied).
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


def _all_reachable_with_competitors_matches() -> bool:
    """Best-effort probe: True only if all three URLs are set, connectable,
    AND the SPEC-05 competitors/matches tables already exist (migration
    applied)."""
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
        if not {"competitors", "competitor_product_matches", "products", "product_variants"} <= table_names:
            return False
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _all_reachable_with_competitors_matches(),
    reason=(
        "Needs reachable DATABASE_URL (app role, no BYPASSRLS) + "
        "AUTH_DATABASE_URL + MIGRATION_DATABASE_URL, with the SPEC-05 "
        "competitors/matches migration already applied -- not available "
        "in this environment."
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
def two_workspaces_with_competitors_and_matches() -> Iterator[dict[str, object]]:
    """Seed two workspaces, one product+variant+competitor+match each, and
    per-workspace-A read-only / write-scoped API keys, via the privileged
    migration engine."""
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, ProductStatus, VariantStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.models.catalog import Product, ProductVariant
    from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
    from app_shared.security.api_keys import generate_api_key
    from app_shared.url_pattern import derive_match_url_fields

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        ws_a = Workspace(
            name=f"CM Isolation A {unique}", slug=f"cm-isolation-a-{unique}", status=WorkspaceStatus.ACTIVE
        )
        ws_b = Workspace(
            name=f"CM Isolation B {unique}", slug=f"cm-isolation-b-{unique}", status=WorkspaceStatus.ACTIVE
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
            title="Variant A",
            current_price=Decimal("9.9900"),
            currency="USD",
            status=VariantStatus.ACTIVE,
        )
        variant_b = ProductVariant(
            workspace_id=ws_b.id,
            product_id=product_b.id,
            title="Variant B",
            current_price=Decimal("9.9900"),
            currency="USD",
            status=VariantStatus.ACTIVE,
        )
        session.add_all([variant_a, variant_b])
        session.flush()

        competitor_a = Competitor(
            workspace_id=ws_a.id, name="Competitor A", domain=f"competitor-a-{unique}.example.com"
        )
        competitor_b = Competitor(
            workspace_id=ws_b.id, name="Competitor B", domain=f"competitor-b-{unique}.example.com"
        )
        session.add_all([competitor_a, competitor_b])
        session.flush()

        url_a = f"https://competitor-a-{unique}.example.com/products/widget-a"
        url_b = f"https://competitor-b-{unique}.example.com/products/widget-b"
        norm_a, pattern_a, version_a = derive_match_url_fields(url_a)
        norm_b, pattern_b, version_b = derive_match_url_fields(url_b)

        match_a = CompetitorProductMatch(
            workspace_id=ws_a.id,
            product_id=product_a.id,
            product_variant_id=variant_a.id,
            competitor_id=competitor_a.id,
            competitor_url=url_a,
            normalized_competitor_url=norm_a,
            url_pattern=pattern_a,
            url_pattern_version=version_a,
        )
        match_b = CompetitorProductMatch(
            workspace_id=ws_b.id,
            product_id=product_b.id,
            product_variant_id=variant_b.id,
            competitor_id=competitor_b.id,
            competitor_url=url_b,
            normalized_competitor_url=norm_b,
            url_pattern=pattern_b,
            url_pattern_version=version_b,
        )
        session.add_all([match_a, match_b])
        session.flush()

        scopes = ["competitors:read", "competitors:write", "matches:read", "matches:write"]
        write_secret_a, write_prefix_a, write_hash_a = generate_api_key()
        write_key_a = ApiKey(
            workspace_id=ws_a.id,
            name="cm-isolation-write-a",
            key_prefix=write_prefix_a,
            key_hash=write_hash_a,
            scopes=scopes,
            status=ApiKeyStatus.ACTIVE,
        )
        read_secret_a, read_prefix_a, read_hash_a = generate_api_key()
        read_key_a = ApiKey(
            workspace_id=ws_a.id,
            name="cm-isolation-read-a",
            key_prefix=read_prefix_a,
            key_hash=read_hash_a,
            scopes=["competitors:read", "matches:read"],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add_all([write_key_a, read_key_a])
        session.commit()

        ids = {
            "workspace_a_id": ws_a.id,
            "workspace_b_id": ws_b.id,
            "product_a_id": product_a.id,
            "variant_a_id": variant_a.id,
            "competitor_a_id": competitor_a.id,
            "competitor_b_id": competitor_b.id,
            "match_a_id": match_a.id,
            "match_b_id": match_b.id,
            "write_api_key_a": write_secret_a,
            "read_api_key_a": read_secret_a,
        }

    try:
        yield ids
    finally:
        with get_session() as session:
            for ws in (ids["workspace_a_id"], ids["workspace_b_id"]):
                session.execute(
                    text("DELETE FROM competitor_product_matches WHERE workspace_id = :ws"),
                    {"ws": ws},
                )
                session.execute(
                    text("DELETE FROM competitors WHERE workspace_id = :ws"), {"ws": ws}
                )
                session.execute(
                    text("DELETE FROM product_variants WHERE workspace_id = :ws"), {"ws": ws}
                )
                session.execute(text("DELETE FROM products WHERE workspace_id = :ws"), {"ws": ws})
                session.execute(text("DELETE FROM api_keys WHERE workspace_id = :ws"), {"ws": ws})
                session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
            session.commit()


# --- SC-007: workspace-A caller sees 0 of workspace-B's rows ----------------


def test_workspace_a_caller_gets_404_for_workspace_b_competitor_by_id(
    client, two_workspaces_with_competitors_and_matches: dict[str, object]
) -> None:
    headers = {
        "Authorization": f"Bearer {two_workspaces_with_competitors_and_matches['write_api_key_a']}"
    }
    resp = client.get(
        f"/v1/competitors/{two_workspaces_with_competitors_and_matches['competitor_b_id']}",
        headers=headers,
    )
    assert resp.status_code == 404


def test_workspace_a_caller_gets_404_for_workspace_b_match_by_id(
    client, two_workspaces_with_competitors_and_matches: dict[str, object]
) -> None:
    headers = {
        "Authorization": f"Bearer {two_workspaces_with_competitors_and_matches['write_api_key_a']}"
    }
    resp = client.get(
        f"/v1/matches/{two_workspaces_with_competitors_and_matches['match_b_id']}",
        headers=headers,
    )
    assert resp.status_code == 404


def test_workspace_a_caller_list_never_includes_workspace_b_competitor(
    client, two_workspaces_with_competitors_and_matches: dict[str, object]
) -> None:
    headers = {
        "Authorization": f"Bearer {two_workspaces_with_competitors_and_matches['write_api_key_a']}"
    }
    resp = client.get("/v1/competitors", headers=headers)
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(two_workspaces_with_competitors_and_matches["competitor_a_id"]) in ids
    assert str(two_workspaces_with_competitors_and_matches["competitor_b_id"]) not in ids


def test_workspace_a_caller_list_never_includes_workspace_b_match(
    client, two_workspaces_with_competitors_and_matches: dict[str, object]
) -> None:
    headers = {
        "Authorization": f"Bearer {two_workspaces_with_competitors_and_matches['write_api_key_a']}"
    }
    resp = client.get("/v1/matches", headers=headers)
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(two_workspaces_with_competitors_and_matches["match_a_id"]) in ids
    assert str(two_workspaces_with_competitors_and_matches["match_b_id"]) not in ids


# --- FR-002: app-filter-omitted query still returns 0 other-ws rows (RLS) ---


def test_app_filter_omitted_query_returns_zero_other_workspace_competitors_via_rls(
    two_workspaces_with_competitors_and_matches: dict[str, object], app_engine: Engine
) -> None:
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": str(two_workspaces_with_competitors_and_matches["workspace_a_id"])},
        )
        # Deliberately app-unscoped -- no WHERE workspace_id = ... at all;
        # RLS is the only thing standing between this query and workspace B's row.
        rows = conn.execute(text("SELECT id FROM competitors")).fetchall()

    ids = {row[0] for row in rows}
    assert two_workspaces_with_competitors_and_matches["competitor_a_id"] in ids
    assert two_workspaces_with_competitors_and_matches["competitor_b_id"] not in ids


def test_app_filter_omitted_query_returns_zero_other_workspace_matches_via_rls(
    two_workspaces_with_competitors_and_matches: dict[str, object], app_engine: Engine
) -> None:
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": str(two_workspaces_with_competitors_and_matches["workspace_a_id"])},
        )
        rows = conn.execute(text("SELECT id FROM competitor_product_matches")).fetchall()

    ids = {row[0] for row in rows}
    assert two_workspaces_with_competitors_and_matches["match_a_id"] in ids
    assert two_workspaces_with_competitors_and_matches["match_b_id"] not in ids


# --- FR-002/SC-007: no context at all -> 0 rows, fail closed ----------------


def test_no_workspace_context_returns_zero_competitor_rows_fail_closed(
    two_workspaces_with_competitors_and_matches: dict[str, object], app_engine: Engine
) -> None:
    with app_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id FROM competitors WHERE id IN (:a, :b)"),
            {
                "a": str(two_workspaces_with_competitors_and_matches["competitor_a_id"]),
                "b": str(two_workspaces_with_competitors_and_matches["competitor_b_id"]),
            },
        ).fetchall()

    assert rows == []


def test_no_workspace_context_returns_zero_match_rows_fail_closed(
    two_workspaces_with_competitors_and_matches: dict[str, object], app_engine: Engine
) -> None:
    with app_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id FROM competitor_product_matches WHERE id IN (:a, :b)"),
            {
                "a": str(two_workspaces_with_competitors_and_matches["match_a_id"]),
                "b": str(two_workspaces_with_competitors_and_matches["match_b_id"]),
            },
        ).fetchall()

    assert rows == []


# --- SC-008: read-only credential can't write; write credential can --------


def test_read_only_scoped_credential_cannot_create_competitor(
    client, two_workspaces_with_competitors_and_matches: dict[str, object]
) -> None:
    headers = {
        "Authorization": f"Bearer {two_workspaces_with_competitors_and_matches['read_api_key_a']}"
    }
    resp = client.post(
        "/v1/competitors",
        headers=headers,
        json={"name": "Should Be Refused", "domain": "refused.example.com"},
    )
    assert resp.status_code == 403


def test_write_scoped_credential_can_create_competitor(
    client, two_workspaces_with_competitors_and_matches: dict[str, object]
) -> None:
    headers = {
        "Authorization": f"Bearer {two_workspaces_with_competitors_and_matches['write_api_key_a']}"
    }
    resp = client.post(
        "/v1/competitors",
        headers=headers,
        json={"name": "Should Succeed", "domain": "succeed.example.com"},
    )
    assert resp.status_code == 201


def test_read_only_scoped_credential_cannot_create_match(
    client, two_workspaces_with_competitors_and_matches: dict[str, object]
) -> None:
    headers = {
        "Authorization": f"Bearer {two_workspaces_with_competitors_and_matches['read_api_key_a']}"
    }
    resp = client.post(
        "/v1/matches",
        headers=headers,
        json={
            "product_variant_id": str(
                two_workspaces_with_competitors_and_matches["variant_a_id"]
            ),
            "competitor_id": str(
                two_workspaces_with_competitors_and_matches["competitor_a_id"]
            ),
            "competitor_url": "https://competitor-a.example.com/products/should-be-refused",
        },
    )
    assert resp.status_code == 403


def test_write_scoped_credential_can_create_match(
    client, two_workspaces_with_competitors_and_matches: dict[str, object]
) -> None:
    headers = {
        "Authorization": f"Bearer {two_workspaces_with_competitors_and_matches['write_api_key_a']}"
    }
    resp = client.post(
        "/v1/matches",
        headers=headers,
        json={
            "product_variant_id": str(
                two_workspaces_with_competitors_and_matches["variant_a_id"]
            ),
            "competitor_id": str(
                two_workspaces_with_competitors_and_matches["competitor_a_id"]
            ),
            "competitor_url": "https://competitor-a.example.com/products/should-succeed",
        },
    )
    assert resp.status_code == 201


# --- FR-006: composite-FK cross-workspace reference rejected ----------------


def test_composite_fk_rejects_cross_workspace_competitor_reference(
    two_workspaces_with_competitors_and_matches: dict[str, object], app_engine: Engine
) -> None:
    """A `competitor_product_matches` row naming workspace B's id but a
    `competitor_id` that belongs to workspace A is structurally
    impossible — the composite FK `(workspace_id, competitor_id) ->
    competitors(workspace_id, id)` rejects it at the DB (never reaches
    app-layer validation)."""
    ids = two_workspaces_with_competitors_and_matches
    with pytest.raises((IntegrityError, DBAPIError)):
        with app_engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.workspace_id', :w, true)"),
                {"w": str(ids["workspace_b_id"])},
            )
            conn.execute(
                text(
                    "INSERT INTO competitor_product_matches "
                    "(id, workspace_id, product_id, product_variant_id, competitor_id, "
                    "competitor_url, normalized_competitor_url, url_pattern, "
                    "url_pattern_version, priority, status, health_status, "
                    "consecutive_failures, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :ws, :product_id, :variant_id, "
                    ":competitor_id, 'https://cross-ws.example.com/x', "
                    "'https://cross-ws.example.com/x', 'cross-ws.example.com/x', 1, "
                    "'NORMAL', 'ACTIVE', 'UNKNOWN', 0, now(), now())"
                ),
                {
                    "ws": str(ids["workspace_b_id"]),
                    "product_id": str(ids["product_a_id"]),
                    "variant_id": str(ids["variant_a_id"]),
                    "competitor_id": str(ids["competitor_a_id"]),
                },
            )
