"""Live cross-workspace observations isolation test (SPEC-07 Principle II
T049, FR-002/FR-017) — ⏸ DEFERRED.

Mirrors `tests/integration/test_workspace_isolation_live.py` (SPEC-04) and
`tests/integration/test_competitors_matches_isolation_live.py` (SPEC-05),
substituting the three SPEC-07 observation tables
(`price_observations`/`request_attempts`/`match_current_prices`). Unlike
those two, this spec adds **no** `/v1/...` API surface for these tables
(the spider stops at persistence, FR-020) — there is no HTTP layer to
drive, so this test proves isolation directly at the app-scoping-helper
and RLS layers the spider's own DB seam
(`scrape_core.db.workspace_txn` -> `app_shared.database.set_workspace_context`)
relies on:

1. `app_shared.repository.scoped_select` (the same helper
   `generic_price_spider.load_targets` and
   `scrape_core.pipelines._flush_batch` use implicitly via
   `workspace_txn`) never returns another workspace's row for any of the
   three tables, with `app.workspace_id` set to the caller's own
   workspace.
2. A deliberately app-**unscoped** raw query (no `WHERE workspace_id =
   ...` at all) with `app.workspace_id` set to workspace A still returns
   **0** of workspace B's rows for all three tables — RLS alone enforces
   isolation (FR-002), exactly as the SPEC-04/05 precedents prove for
   their own tables.
3. With **no** `app.workspace_id` context set at all, the same raw
   queries return **0** rows for either workspace's seeded row on all
   three tables (fail closed).

Not applicable here (unlike the SPEC-04/05 precedents): a composite-FK
cross-workspace-parent-reference test — `price_observations`/
`request_attempts`/`match_current_prices` carry only a real FK on
`workspace_id`; `match_id`/`product_id`/`product_variant_id`/
`competitor_id` are deliberately **soft** references (no FK, §22), so
there is no DB constraint to prove here (this is by design, not a gap —
see `contracts/models-observations.md`).

Needs a reachable Postgres with `DATABASE_URL` (app role, RLS enforced)
with the SPEC-07 migration already applied. Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever `DATABASE_URL` is unset/unreachable or the observation
tables don't exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-07 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

_REQUIRED_TABLES = frozenset(
    {"price_observations", "request_attempts", "match_current_prices", "workspaces"}
)


def _database_url() -> str | None:
    import os

    return os.environ.get("DATABASE_URL")


def _observations_reachable() -> bool:
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
        if not _REQUIRED_TABLES <= table_names:
            return False
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _observations_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-07 observations "
        "migration applied in this environment"
    ),
)


@dataclass
class _SeededPair:
    workspace_a_id: uuid.UUID
    workspace_b_id: uuid.UUID
    observation_a_id: uuid.UUID
    observation_b_id: uuid.UUID
    attempt_a_id: uuid.UUID
    attempt_b_id: uuid.UUID
    current_price_a_id: uuid.UUID
    current_price_b_id: uuid.UUID


@pytest.fixture()
def app_engine() -> Iterator[Engine]:
    engine = create_engine(_database_url())
    yield engine
    engine.dispose()


@pytest.fixture()
def seeded_pair() -> Iterator[_SeededPair]:
    """One `price_observations` + one `request_attempts` + one
    `match_current_prices` row per workspace, in two distinct workspaces."""
    from app_shared.database import get_session
    from app_shared.enums import AccessMethod, WorkspaceStatus
    from app_shared.models import Workspace
    from app_shared.models.observations import MatchCurrentPrice, PriceObservation, RequestAttempt

    unique = uuid.uuid4().hex[:8]
    now = datetime.now(UTC)

    with get_session() as session:
        ws_a = Workspace(
            name=f"Observations Isolation A {unique}",
            slug=f"observations-isolation-a-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        ws_b = Workspace(
            name=f"Observations Isolation B {unique}",
            slug=f"observations-isolation-b-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add_all([ws_a, ws_b])
        session.flush()

        def _row_ids() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
            return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        match_a, product_a, variant_a = _row_ids()
        match_b, product_b, variant_b = _row_ids()
        competitor_a, competitor_b = uuid.uuid4(), uuid.uuid4()

        obs_a = PriceObservation(
            workspace_id=ws_a.id,
            scraped_at=now,
            match_id=match_a,
            product_id=product_a,
            product_variant_id=variant_a,
            price=Decimal("9.9900"),
            currency="USD",
            success=True,
            comparable=True,
        )
        obs_b = PriceObservation(
            workspace_id=ws_b.id,
            scraped_at=now,
            match_id=match_b,
            product_id=product_b,
            product_variant_id=variant_b,
            price=Decimal("19.9900"),
            currency="USD",
            success=True,
            comparable=True,
        )
        session.add_all([obs_a, obs_b])
        session.flush()

        attempt_a = RequestAttempt(
            workspace_id=ws_a.id,
            created_at=now,
            match_id=match_a,
            url="http://fixture-store.invalid/isolation-a",
            access_method=AccessMethod.DIRECT_HTTP,
            success=True,
        )
        attempt_b = RequestAttempt(
            workspace_id=ws_b.id,
            created_at=now,
            match_id=match_b,
            url="http://fixture-store.invalid/isolation-b",
            access_method=AccessMethod.DIRECT_HTTP,
            success=True,
        )
        session.add_all([attempt_a, attempt_b])
        session.flush()

        current_a = MatchCurrentPrice(
            workspace_id=ws_a.id,
            match_id=match_a,
            product_id=product_a,
            product_variant_id=variant_a,
            competitor_id=competitor_a,
            price=Decimal("9.9900"),
            currency="USD",
            comparable=True,
            success=True,
            scraped_at=now,
        )
        current_b = MatchCurrentPrice(
            workspace_id=ws_b.id,
            match_id=match_b,
            product_id=product_b,
            product_variant_id=variant_b,
            competitor_id=competitor_b,
            price=Decimal("19.9900"),
            currency="USD",
            comparable=True,
            success=True,
            scraped_at=now,
        )
        session.add_all([current_a, current_b])
        session.commit()

        seeded = _SeededPair(
            workspace_a_id=ws_a.id,
            workspace_b_id=ws_b.id,
            observation_a_id=obs_a.id,
            observation_b_id=obs_b.id,
            attempt_a_id=attempt_a.id,
            attempt_b_id=attempt_b.id,
            current_price_a_id=current_a.id,
            current_price_b_id=current_b.id,
        )

    try:
        yield seeded
    finally:
        with get_session() as session:
            for ws in (seeded.workspace_a_id, seeded.workspace_b_id):
                session.execute(
                    text("DELETE FROM match_current_prices WHERE workspace_id = :ws"), {"ws": ws}
                )
                session.execute(
                    text("DELETE FROM request_attempts WHERE workspace_id = :ws"), {"ws": ws}
                )
                session.execute(
                    text("DELETE FROM price_observations WHERE workspace_id = :ws"), {"ws": ws}
                )
                session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
            session.commit()


# --- 1. scoped_select never returns another workspace's row -----------------


def test_scoped_select_never_returns_other_workspace_rows(seeded_pair: _SeededPair) -> None:
    from app_shared.database import get_session, set_workspace_context
    from app_shared.models.observations import MatchCurrentPrice, PriceObservation, RequestAttempt
    from app_shared.repository import scoped_select

    with get_session() as session:
        set_workspace_context(session, seeded_pair.workspace_a_id)

        obs_ids = {
            row.id
            for row in session.execute(
                scoped_select(PriceObservation, seeded_pair.workspace_a_id)
            ).scalars()
        }
        assert seeded_pair.observation_a_id in obs_ids
        assert seeded_pair.observation_b_id not in obs_ids

        attempt_ids = {
            row.id
            for row in session.execute(
                scoped_select(RequestAttempt, seeded_pair.workspace_a_id)
            ).scalars()
        }
        assert seeded_pair.attempt_a_id in attempt_ids
        assert seeded_pair.attempt_b_id not in attempt_ids

        current_price_ids = {
            row.id
            for row in session.execute(
                scoped_select(MatchCurrentPrice, seeded_pair.workspace_a_id)
            ).scalars()
        }
        assert seeded_pair.current_price_a_id in current_price_ids
        assert seeded_pair.current_price_b_id not in current_price_ids


# --- 2. app-filter-omitted query still returns 0 other-workspace rows (RLS) -


def test_app_filter_omitted_query_returns_zero_other_workspace_rows_via_rls(
    seeded_pair: _SeededPair, app_engine: Engine
) -> None:
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": str(seeded_pair.workspace_a_id)},
        )
        # Deliberately app-unscoped -- no WHERE workspace_id = ... at all;
        # RLS is the only thing standing between this query and workspace
        # B's rows.
        obs_ids = {
            row[0] for row in conn.execute(text("SELECT id FROM price_observations")).fetchall()
        }
        attempt_ids = {
            row[0] for row in conn.execute(text("SELECT id FROM request_attempts")).fetchall()
        }
        current_price_ids = {
            row[0] for row in conn.execute(text("SELECT id FROM match_current_prices")).fetchall()
        }

    assert seeded_pair.observation_a_id in obs_ids
    assert seeded_pair.observation_b_id not in obs_ids
    assert seeded_pair.attempt_a_id in attempt_ids
    assert seeded_pair.attempt_b_id not in attempt_ids
    assert seeded_pair.current_price_a_id in current_price_ids
    assert seeded_pair.current_price_b_id not in current_price_ids


# --- 3. no workspace context at all -> 0 rows, fail closed ------------------


def test_no_workspace_context_returns_zero_rows_fail_closed(
    seeded_pair: _SeededPair, app_engine: Engine
) -> None:
    with app_engine.begin() as conn:
        obs_rows = conn.execute(
            text("SELECT id FROM price_observations WHERE id IN (:a, :b)"),
            {"a": seeded_pair.observation_a_id, "b": seeded_pair.observation_b_id},
        ).fetchall()
        attempt_rows = conn.execute(
            text("SELECT id FROM request_attempts WHERE id IN (:a, :b)"),
            {"a": seeded_pair.attempt_a_id, "b": seeded_pair.attempt_b_id},
        ).fetchall()
        current_price_rows = conn.execute(
            text("SELECT id FROM match_current_prices WHERE id IN (:a, :b)"),
            {"a": seeded_pair.current_price_a_id, "b": seeded_pair.current_price_b_id},
        ).fetchall()

    assert obs_rows == []
    assert attempt_rows == []
    assert current_price_rows == []
