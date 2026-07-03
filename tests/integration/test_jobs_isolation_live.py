"""Live cross-workspace jobs isolation test (SPEC-08 Principle II, SC-006)
— ⏸ DEFERRED.

Mirrors `tests/integration/test_observations_isolation_live.py` (SPEC-07)
and `tests/integration/test_competitors_matches_isolation_live.py`
(SPEC-05), substituting the two SPEC-08 tables (`scrape_jobs`/
`scrape_job_targets`). Unlike the SPEC-07 observations precedent, this
spec's target row DOES carry one real (non-soft) FK worth proving here:
the workspace-local **composite** FK from `scrape_job_targets
(workspace_id, scrape_job_id)` to `scrape_jobs (workspace_id, id)` —
structurally impossible for a target to reference a job in a *different*
workspace, not just app-filtered (`match_id` itself stays a soft ref, no
FK, matching §22).

Proves, on `scrape_jobs`/`scrape_job_targets`:

1. `app_shared.repository.scoped_select` never returns another
   workspace's job/target row.
2. A deliberately app-**unscoped** raw query (no `WHERE workspace_id =
   ...` at all) with `app.workspace_id` set to workspace A still
   returns **0** of workspace B's rows for both tables — RLS alone
   enforces isolation (FR-004).
3. With **no** `app.workspace_id` context set at all, the same raw
   queries return **0** rows for either workspace's seeded row on both
   tables (fail closed).
4. The workspace-local composite FK blocks a cross-workspace
   target->job reference: inserting a `scrape_job_targets` row with
   `workspace_id = A` but `scrape_job_id` = workspace B's job id raises
   an `IntegrityError` — the DB itself refuses the row, not just an
   app-layer check.

Needs a reachable Postgres with `DATABASE_URL` (app role, RLS enforced)
with the SPEC-08 migration already applied. Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever `DATABASE_URL` is unset/unreachable or the
`scrape_jobs`/`scrape_job_targets` tables don't exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-08 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

_REQUIRED_TABLES = frozenset({"workspaces", "scrape_jobs", "scrape_job_targets"})


def _database_url() -> str | None:
    import os

    return os.environ.get("DATABASE_URL")


def _jobs_isolation_reachable() -> bool:
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
    not _jobs_isolation_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-08 "
        "scrape_jobs/scrape_job_targets migration applied in this environment"
    ),
)


@dataclass
class _SeededPair:
    workspace_a_id: uuid.UUID
    workspace_b_id: uuid.UUID
    job_a_id: uuid.UUID
    job_b_id: uuid.UUID
    target_a_id: uuid.UUID
    target_b_id: uuid.UUID


@pytest.fixture()
def app_engine() -> Iterator[Engine]:
    engine = create_engine(_database_url())
    yield engine
    engine.dispose()


@pytest.fixture()
def seeded_pair() -> Iterator[_SeededPair]:
    """One `scrape_jobs` + one `scrape_job_targets` row per workspace, in
    two distinct workspaces."""
    from app_shared.database import get_session
    from app_shared.enums import (
        ScrapeJobSource,
        ScrapeJobStatus,
        ScrapeJobType,
        ScrapeScope,
        ScrapeTargetStatus,
        WorkspaceStatus,
    )
    from app_shared.models import Workspace
    from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget

    unique = uuid.uuid4().hex[:8]
    now = datetime.now(UTC)

    with get_session() as session:
        ws_a = Workspace(
            name=f"Jobs Isolation A {unique}",
            slug=f"jobs-isolation-a-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        ws_b = Workspace(
            name=f"Jobs Isolation B {unique}",
            slug=f"jobs-isolation-b-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add_all([ws_a, ws_b])
        session.flush()

        job_a = ScrapeJob(
            workspace_id=ws_a.id,
            type=ScrapeJobType.MANUAL,
            scope=ScrapeScope.MATCH,
            status=ScrapeJobStatus.PENDING,
            source=ScrapeJobSource.API,
            total_targets=1,
            created_at=now,
        )
        job_b = ScrapeJob(
            workspace_id=ws_b.id,
            type=ScrapeJobType.MANUAL,
            scope=ScrapeScope.MATCH,
            status=ScrapeJobStatus.PENDING,
            source=ScrapeJobSource.API,
            total_targets=1,
            created_at=now,
        )
        session.add_all([job_a, job_b])
        session.flush()

        target_a = ScrapeJobTarget(
            workspace_id=ws_a.id,
            scrape_job_id=job_a.id,
            match_id=uuid.uuid4(),
            status=ScrapeTargetStatus.PENDING,
            created_at=now,
        )
        target_b = ScrapeJobTarget(
            workspace_id=ws_b.id,
            scrape_job_id=job_b.id,
            match_id=uuid.uuid4(),
            status=ScrapeTargetStatus.PENDING,
            created_at=now,
        )
        session.add_all([target_a, target_b])
        session.commit()

        seeded = _SeededPair(
            workspace_a_id=ws_a.id,
            workspace_b_id=ws_b.id,
            job_a_id=job_a.id,
            job_b_id=job_b.id,
            target_a_id=target_a.id,
            target_b_id=target_b.id,
        )

    try:
        yield seeded
    finally:
        with get_session() as session:
            for ws in (seeded.workspace_a_id, seeded.workspace_b_id):
                session.execute(
                    text("DELETE FROM scrape_job_targets WHERE workspace_id = :ws"), {"ws": ws}
                )
                session.execute(
                    text("DELETE FROM scrape_jobs WHERE workspace_id = :ws"), {"ws": ws}
                )
                session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
            session.commit()


# --- 1. scoped_select never returns another workspace's row -----------------


def test_scoped_select_never_returns_other_workspace_rows(seeded_pair: _SeededPair) -> None:
    from app_shared.database import get_session, set_workspace_context
    from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
    from app_shared.repository import scoped_select

    with get_session() as session:
        set_workspace_context(session, seeded_pair.workspace_a_id)

        job_ids = {
            row.id
            for row in session.execute(
                scoped_select(ScrapeJob, seeded_pair.workspace_a_id)
            ).scalars()
        }
        assert seeded_pair.job_a_id in job_ids
        assert seeded_pair.job_b_id not in job_ids

        target_ids = {
            row.id
            for row in session.execute(
                scoped_select(ScrapeJobTarget, seeded_pair.workspace_a_id)
            ).scalars()
        }
        assert seeded_pair.target_a_id in target_ids
        assert seeded_pair.target_b_id not in target_ids


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
        job_ids = {row[0] for row in conn.execute(text("SELECT id FROM scrape_jobs")).fetchall()}
        target_ids = {
            row[0] for row in conn.execute(text("SELECT id FROM scrape_job_targets")).fetchall()
        }

    assert seeded_pair.job_a_id in job_ids
    assert seeded_pair.job_b_id not in job_ids
    assert seeded_pair.target_a_id in target_ids
    assert seeded_pair.target_b_id not in target_ids


# --- 3. no workspace context at all -> 0 rows, fail closed ------------------


def test_no_workspace_context_returns_zero_rows_fail_closed(
    seeded_pair: _SeededPair, app_engine: Engine
) -> None:
    with app_engine.begin() as conn:
        job_rows = conn.execute(
            text("SELECT id FROM scrape_jobs WHERE id IN (:a, :b)"),
            {"a": seeded_pair.job_a_id, "b": seeded_pair.job_b_id},
        ).fetchall()
        target_rows = conn.execute(
            text("SELECT id FROM scrape_job_targets WHERE id IN (:a, :b)"),
            {"a": seeded_pair.target_a_id, "b": seeded_pair.target_b_id},
        ).fetchall()

    assert job_rows == []
    assert target_rows == []


# --- 4. workspace-local composite FK blocks a cross-ws target->job ref ------


def test_composite_fk_blocks_cross_workspace_target_to_job_reference(
    seeded_pair: _SeededPair, app_engine: Engine
) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with app_engine.begin() as conn:
            # workspace_id = A but scrape_job_id = workspace B's job --
            # (A, job_b_id) does not exist in scrape_jobs(workspace_id, id),
            # so the composite FK must reject this row outright.
            conn.execute(
                text(
                    "INSERT INTO scrape_job_targets "
                    "(id, workspace_id, scrape_job_id, match_id, status, created_at) "
                    "VALUES (gen_random_uuid(), :ws, :job_id, gen_random_uuid(), "
                    "'PENDING', now())"
                ),
                {"ws": str(seeded_pair.workspace_a_id), "job_id": str(seeded_pair.job_b_id)},
            )
