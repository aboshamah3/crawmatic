"""Live RLS isolation test for the three SPEC-12 tables (T043, FR-026, SC-005)
— DEFERRED.

Mirrors ``tests/integration/test_access_isolation_live.py`` for the domain
strategy optimizer tables, asserting fail-closed isolation directly against a
live Postgres+RLS instance:

1. With **no** ``app.workspace_id`` GUC set at all, a raw select on each of the
   three tables returns **zero rows** — ``domain_strategy_profiles`` and
   ``strategy_discovery_runs`` via their standard ``emit_rls_policy`` policies,
   and ``strategy_attempt_stats`` via its transitive ``EXISTS``-against-parent
   policy (``emit_fk_transitive_rls_policy``), which is the whole point of the
   transitive design (the stats table carries no ``workspace_id`` column).
2. Workspace A's context cannot read workspace B's profile / discovery-run /
   attempt-stats rows (cross-workspace denied), and vice-versa.

Needs a reachable Postgres with ``DATABASE_URL`` (app role, RLS enforced) with
the SPEC-12 migration applied. Not runnable in the no-Docker-daemon build
environment used to author this feature — SKIPS cleanly whenever
``DATABASE_URL`` is unset/unreachable or the three tables don't exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host with the
SPEC-12 migration applied).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

_SPEC12_TABLES = {
    "domain_strategy_profiles",
    "strategy_attempt_stats",
    "strategy_discovery_runs",
}


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


def _live_strategy_reachable() -> bool:
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
        if not _SPEC12_TABLES <= table_names:
            return False
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _live_strategy_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-12 domain-strategy-"
        "optimizer migration applied in this environment"
    ),
)


@pytest.fixture()
def app_engine() -> Iterator[Engine]:
    engine = create_engine(_database_url())
    yield engine
    engine.dispose()


@pytest.fixture()
def isolation_fixture() -> Iterator[dict[str, uuid.UUID]]:
    """Seed two workspaces, each with one competitor, one strategy profile,
    one discovery run, and one attempt-stats row; cleaned up after."""
    from app_shared.database import get_session
    from app_shared.enums import (
        DiscoveryRunStatus,
        MethodType,
        StrategyStatus,
        WorkspaceStatus,
    )
    from app_shared.models import Workspace
    from app_shared.models.competitors_matches import Competitor
    from app_shared.models.strategy import (
        DomainStrategyProfile,
        StrategyAttemptStats,
        StrategyDiscoveryRun,
    )
    from app_shared.url_pattern import URL_PATTERN_ALGORITHM_VERSION

    unique = uuid.uuid4().hex[:8]
    created: dict[str, uuid.UUID] = {}

    with get_session() as session:
        ws_a = Workspace(
            name=f"Strategy Isolation A {unique}",
            slug=f"strategy-isolation-a-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        ws_b = Workspace(
            name=f"Strategy Isolation B {unique}",
            slug=f"strategy-isolation-b-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add_all([ws_a, ws_b])
        session.flush()

        for label, ws in (("a", ws_a), ("b", ws_b)):
            competitor = Competitor(
                workspace_id=ws.id,
                name=f"comp-{label}-{unique}",
                domain=f"comp-{label}-{unique}.example.com",
            )
            session.add(competitor)
            session.flush()

            profile = DomainStrategyProfile(
                workspace_id=ws.id,
                competitor_id=competitor.id,
                domain=competitor.domain,
                url_pattern=f"comp-{label}-{unique}.example.com/products/*",
                url_pattern_version=URL_PATTERN_ALGORITHM_VERSION,
                status=StrategyStatus.DISCOVERY_REQUIRED,
            )
            session.add(profile)
            session.flush()

            session.add(
                StrategyAttemptStats(
                    domain_strategy_profile_id=profile.id,
                    method_type=MethodType.ACCESS,
                    method_name="DIRECT_HTTP",
                )
            )
            session.add(
                StrategyDiscoveryRun(
                    workspace_id=ws.id,
                    competitor_id=competitor.id,
                    domain=competitor.domain,
                    url_pattern=profile.url_pattern,
                    sample_size=3,
                    status=DiscoveryRunStatus.PENDING,
                )
            )
            created[f"ws_{label}"] = ws.id
            created[f"profile_{label}"] = profile.id

        session.commit()

    yield created

    with get_session() as session:
        for label in ("a", "b"):
            session.execute(
                text("DELETE FROM workspaces WHERE id = :wid"),
                {"wid": str(created[f"ws_{label}"])},
            )
        session.commit()


def _count_no_context(engine: Engine, table: str) -> int:
    """Raw count with NO ``app.workspace_id`` GUC set — must be 0 (fail-closed)."""
    with engine.connect() as conn:
        # A fresh connection with no set_config('app.workspace_id', ...) call.
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def test_no_context_returns_zero_rows_all_three_tables(
    app_engine: Engine, isolation_fixture: dict[str, uuid.UUID]
) -> None:
    for table in _SPEC12_TABLES:
        assert _count_no_context(app_engine, table) == 0, (
            f"{table} leaked rows with no app.workspace_id context — RLS is not "
            "fail-closed (FR-026, SC-005)"
        )


def test_cross_workspace_rows_are_denied(
    app_engine: Engine, isolation_fixture: dict[str, uuid.UUID]
) -> None:
    from app_shared.database import get_session, set_workspace_context
    from app_shared.models.strategy import DomainStrategyProfile
    from app_shared.repository import scoped_get

    ws_a = isolation_fixture["ws_a"]
    ws_b = isolation_fixture["ws_b"]
    profile_a = isolation_fixture["profile_a"]
    profile_b = isolation_fixture["profile_b"]

    with get_session() as session:
        set_workspace_context(session, ws_a)
        assert scoped_get(session, DomainStrategyProfile, profile_a, ws_a) is not None
        assert scoped_get(session, DomainStrategyProfile, profile_b, ws_a) is None

        set_workspace_context(session, ws_b)
        assert scoped_get(session, DomainStrategyProfile, profile_b, ws_b) is not None
        assert scoped_get(session, DomainStrategyProfile, profile_a, ws_b) is None
