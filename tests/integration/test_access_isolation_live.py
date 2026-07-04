"""Live dual-scope repository + RLS isolation test (SPEC-10 US1 T021,
`contracts/access-repository.md` + `contracts/migration-access.md`
Acceptance, FR-006/SC-005) — DEFERRED.

Mirrors `tests/integration/test_scrape_profiles_isolation_live.py`
(SPEC-06) substituting the two SPEC-10 dual-scope tables
(`proxy_providers`, `access_policies`) plus the tenant-only
`domain_access_rules` fail-closed check, asserting the
`app_shared.access.repository` query semantics directly against a live
Postgres+RLS instance (not through the API):

1. A workspace's `visible_*` select returns its own rows **and** every
   global (`workspace_id IS NULL`) row; another workspace's tenant rows
   are absent.
2. `owned_*` never returns a global row (the write path cannot mutate a
   system default) even when explicitly filtered by the global row's id.
3. With **no** `app.workspace_id` context set at all, `visible_*`
   returns only the global rows — zero tenant rows for either workspace
   (fail-closed on own rows, RLS backs the app-layer filter).
4. `domain_access_rules` (tenant-only) fail-closed isolation: own-only
   via `scoped_select`, and a no-context raw query returns zero rows
   for either workspace (no global carve-out — it binds a
   workspace-owned `competitor_id`, so a global row is nonsensical).

Needs a reachable Postgres with `DATABASE_URL` (app role, RLS enforced)
with the SPEC-10 migration already applied. Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever `DATABASE_URL` is unset/unreachable or the
`proxy_providers`/`access_policies`/`domain_access_rules` tables don't
exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-10 migration applied).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


def _live_access_reachable() -> bool:
    url = _database_url()
    if not url:
        return False
    try:
        engine = create_engine(url)
        with engine.connect():
            pass
        from sqlalchemy import inspect

        table_names = set(inspect(engine).get_table_names())
        engine.dispose()
        if not {"proxy_providers", "access_policies", "domain_access_rules"} <= table_names:
            return False
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _live_access_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-10 access-policies-proxies "
        "migration applied in this environment"
    ),
)


@pytest.fixture()
def app_engine() -> Iterator[Engine]:
    engine = create_engine(_database_url())
    yield engine
    engine.dispose()


@pytest.fixture()
def isolation_fixture() -> Iterator[dict[str, object]]:
    """Seed two workspaces, one own `ProxyProvider`/`AccessPolicy`/
    `DomainAccessRule` each, plus a global `ProxyProvider`/`AccessPolicy`
    (`workspace_id IS NULL`, out-of-band per research D11), cleaned up
    after."""
    from app_shared.database import get_session
    from app_shared.enums import WorkspaceStatus
    from app_shared.models import Workspace
    from app_shared.models.access import AccessPolicy, DomainAccessRule, ProxyProvider
    from app_shared.models.competitors_matches import Competitor

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        ws_a = Workspace(
            name=f"Access Isolation A {unique}",
            slug=f"access-isolation-a-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        ws_b = Workspace(
            name=f"Access Isolation B {unique}",
            slug=f"access-isolation-b-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add_all([ws_a, ws_b])
        session.flush()

        provider_a = ProxyProvider(
            workspace_id=ws_a.id,
            name=f"provider-a-{unique}",
            type="DATACENTER",
            base_url="https://proxy-a.example.com:8080",
        )
        provider_b = ProxyProvider(
            workspace_id=ws_b.id,
            name=f"provider-b-{unique}",
            type="DATACENTER",
            base_url="https://proxy-b.example.com:8080",
        )
        global_provider = ProxyProvider(
            workspace_id=None,
            name=f"provider-global-{unique}",
            type="DATACENTER",
            base_url="https://proxy-global.example.com:8080",
        )
        session.add_all([provider_a, provider_b, global_provider])

        policy_a = AccessPolicy(workspace_id=ws_a.id, name=f"policy-a-{unique}", strategy="DIRECT_ONLY")
        policy_b = AccessPolicy(workspace_id=ws_b.id, name=f"policy-b-{unique}", strategy="DIRECT_ONLY")
        global_policy = AccessPolicy(
            workspace_id=None, name=f"policy-global-{unique}", strategy="DIRECT_ONLY"
        )
        session.add_all([policy_a, policy_b, global_policy])
        session.flush()

        competitor_a = Competitor(
            workspace_id=ws_a.id, name=f"comp-a-{unique}", domain=f"comp-a-{unique}.example.com"
        )
        competitor_b = Competitor(
            workspace_id=ws_b.id, name=f"comp-b-{unique}", domain=f"comp-b-{unique}.example.com"
        )
        session.add_all([competitor_a, competitor_b])
        session.flush()

        rule_a = DomainAccessRule(
            workspace_id=ws_a.id,
            competitor_id=competitor_a.id,
            domain=f"rule-a-{unique}.example.com",
            access_policy_id=policy_a.id,
            max_concurrent_requests=1,
            max_requests_per_minute=10,
            cooldown_seconds=1,
        )
        rule_b = DomainAccessRule(
            workspace_id=ws_b.id,
            competitor_id=competitor_b.id,
            domain=f"rule-b-{unique}.example.com",
            access_policy_id=policy_b.id,
            max_concurrent_requests=1,
            max_requests_per_minute=10,
            cooldown_seconds=1,
        )
        session.add_all([rule_a, rule_b])
        session.commit()

        ids = {
            "workspace_a_id": ws_a.id,
            "workspace_b_id": ws_b.id,
            "provider_a_id": provider_a.id,
            "provider_b_id": provider_b.id,
            "global_provider_id": global_provider.id,
            "policy_a_id": policy_a.id,
            "policy_b_id": policy_b.id,
            "global_policy_id": global_policy.id,
            "rule_a_id": rule_a.id,
            "rule_b_id": rule_b.id,
        }

    try:
        yield ids
    finally:
        with get_session() as session:
            session.execute(
                text("DELETE FROM domain_access_rules WHERE id IN (:a, :b)"),
                {"a": ids["rule_a_id"], "b": ids["rule_b_id"]},
            )
            session.execute(
                text("DELETE FROM competitors WHERE workspace_id IN (:a, :b)"),
                {"a": ids["workspace_a_id"], "b": ids["workspace_b_id"]},
            )
            session.execute(
                text("DELETE FROM access_policies WHERE id IN (:a, :b, :g)"),
                {"a": ids["policy_a_id"], "b": ids["policy_b_id"], "g": ids["global_policy_id"]},
            )
            session.execute(
                text("DELETE FROM proxy_providers WHERE id IN (:a, :b, :g)"),
                {
                    "a": ids["provider_a_id"],
                    "b": ids["provider_b_id"],
                    "g": ids["global_provider_id"],
                },
            )
            session.execute(
                text("DELETE FROM workspaces WHERE id IN (:a, :b)"),
                {"a": ids["workspace_a_id"], "b": ids["workspace_b_id"]},
            )
            session.commit()


# --- visible_* = own + global; owned_* = own-only ---------------------------


def test_visible_providers_returns_own_plus_global_never_other_workspace(
    isolation_fixture: dict[str, object],
) -> None:
    from app_shared.access.repository import visible_providers_select
    from app_shared.database import get_session

    with get_session() as session:
        from app_shared.database import set_workspace_context

        set_workspace_context(session, isolation_fixture["workspace_a_id"])
        ids = {
            row.id
            for row in session.execute(
                visible_providers_select(isolation_fixture["workspace_a_id"])
            ).scalars()
        }

    assert isolation_fixture["provider_a_id"] in ids
    assert isolation_fixture["global_provider_id"] in ids
    assert isolation_fixture["provider_b_id"] not in ids


def test_owned_providers_never_returns_global_row(isolation_fixture: dict[str, object]) -> None:
    from app_shared.access.repository import owned_provider_get
    from app_shared.database import get_session, set_workspace_context

    with get_session() as session:
        set_workspace_context(session, isolation_fixture["workspace_a_id"])
        result = owned_provider_get(
            session, isolation_fixture["global_provider_id"], isolation_fixture["workspace_a_id"]
        )

    assert result is None


def test_visible_policies_returns_own_plus_global_never_other_workspace(
    isolation_fixture: dict[str, object],
) -> None:
    from app_shared.access.repository import visible_policies_select
    from app_shared.database import get_session, set_workspace_context

    with get_session() as session:
        set_workspace_context(session, isolation_fixture["workspace_b_id"])
        ids = {
            row.id
            for row in session.execute(
                visible_policies_select(isolation_fixture["workspace_b_id"])
            ).scalars()
        }

    assert isolation_fixture["policy_b_id"] in ids
    assert isolation_fixture["global_policy_id"] in ids
    assert isolation_fixture["policy_a_id"] not in ids


def test_owned_policies_never_returns_global_row(isolation_fixture: dict[str, object]) -> None:
    from app_shared.access.repository import owned_policy_get
    from app_shared.database import get_session, set_workspace_context

    with get_session() as session:
        set_workspace_context(session, isolation_fixture["workspace_b_id"])
        result = owned_policy_get(
            session, isolation_fixture["global_policy_id"], isolation_fixture["workspace_b_id"]
        )

    assert result is None


# --- no-context: visible_* -> only globals, zero tenant rows ----------------


def test_no_context_visible_providers_returns_only_globals(
    isolation_fixture: dict[str, object], app_engine: Engine
) -> None:
    from app_shared.access.repository import visible_providers_select

    with app_engine.connect() as conn:
        # Deliberately no set_config('app.workspace_id', ...) call.
        stmt = visible_providers_select(isolation_fixture["workspace_a_id"])
        rows = conn.execute(stmt).fetchall()

    ids = {row.id for row in rows}
    assert isolation_fixture["provider_a_id"] not in ids
    assert isolation_fixture["provider_b_id"] not in ids
    assert isolation_fixture["global_provider_id"] in ids


def test_no_context_visible_policies_returns_only_globals(
    isolation_fixture: dict[str, object], app_engine: Engine
) -> None:
    from app_shared.access.repository import visible_policies_select

    with app_engine.connect() as conn:
        stmt = visible_policies_select(isolation_fixture["workspace_a_id"])
        rows = conn.execute(stmt).fetchall()

    ids = {row.id for row in rows}
    assert isolation_fixture["policy_a_id"] not in ids
    assert isolation_fixture["policy_b_id"] not in ids
    assert isolation_fixture["global_policy_id"] in ids


# --- domain_access_rules: tenant-only fail-closed isolation -----------------


def test_domain_access_rules_scoped_select_is_own_only(
    isolation_fixture: dict[str, object],
) -> None:
    from app_shared.database import get_session, set_workspace_context
    from app_shared.models.access import DomainAccessRule
    from app_shared.repository import scoped_select

    with get_session() as session:
        set_workspace_context(session, isolation_fixture["workspace_a_id"])
        ids = {
            row.id
            for row in session.execute(
                scoped_select(DomainAccessRule, isolation_fixture["workspace_a_id"])
            ).scalars()
        }

    assert isolation_fixture["rule_a_id"] in ids
    assert isolation_fixture["rule_b_id"] not in ids


def test_no_context_domain_access_rules_returns_zero_rows(
    isolation_fixture: dict[str, object], app_engine: Engine
) -> None:
    with app_engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM domain_access_rules")).fetchall()

    ids = {row[0] for row in rows}
    assert isolation_fixture["rule_a_id"] not in ids
    assert isolation_fixture["rule_b_id"] not in ids
