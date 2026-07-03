"""Live counters + finalization test (SPEC-08 US3, FR-018/FR-019, SC-004/SC-007)
— ⏸ DEFERRED.

`tests/unit/test_jobs_counters.py` + `tests/unit/test_jobs_lifecycle.py`
already prove `aggregate_counts`/`resolve_finalized_status`/`mark_target`
exhaustively against a fake session. This live test's distinguishing
contribution: real `scrape_job_targets` rows in real Postgres, a real
`GROUP BY status` aggregate query, and a real single `UPDATE` writing
the job row's counters — the DB round trip a fake session cannot stand
in for.

Simulates target-state transitions directly via `mark_target` (the same
FR-017 seam `scrape_core.pipelines._flush_batch` calls in production,
T052), then aggregates + finalizes via `aggregate_counts` +
`resolve_finalized_status` (the same pure functions
`apps/workers/app/workers/tasks_jobs.finalize_jobs`/
`refresh_job_counters` call) — proving the constituent DB/pure
functions the maintenance task is built from against real rows, per
`contracts/lifecycle-counters.md`:

1. All targets COMPLETED (no failures) -> job finalizes COMPLETED.
2. A mix of COMPLETED + FAILED targets -> job finalizes PARTIAL_FAILED.
3. All targets FAILED (zero successes) -> job finalizes FAILED.
4. In every case: counters (`success_count`/`failure_count`/
   `skipped_count`) match the real `GROUP BY status` aggregate, and
   `completed_at` is set on finalization.

Needs only a reachable Postgres (`DATABASE_URL`, SPEC-08 migration
applied) — no Redis/Scrapyd (this test never dispatches or enqueues).
Not runnable in the no-Docker-daemon build environment used to author
this feature — SKIPS cleanly whenever Postgres isn't reachable or the
`scrape_jobs`/`scrape_job_targets` tables don't exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-08 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

_REQUIRED_TABLES = frozenset({"workspaces", "scrape_jobs", "scrape_job_targets"})


def _jobs_counters_reachable() -> bool:
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
        table_names = set(inspect(get_engine()).get_table_names())
        if not _REQUIRED_TABLES <= table_names:
            return False
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _jobs_counters_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-08 "
        "scrape_jobs/scrape_job_targets migration applied in this environment"
    ),
)


@dataclass
class _SeededJob:
    workspace_id: uuid.UUID
    job_id: uuid.UUID
    target_match_ids: list[uuid.UUID] = field(default_factory=list)


def _seed_job_with_targets(*, workspace_id: uuid.UUID, n_targets: int) -> _SeededJob:
    from app_shared.database import get_session
    from app_shared.enums import (
        ScrapeJobSource,
        ScrapeJobStatus,
        ScrapeJobType,
        ScrapeScope,
        ScrapeTargetStatus,
    )
    from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget

    now = datetime.now(UTC)
    match_ids = [uuid.uuid4() for _ in range(n_targets)]

    with get_session() as session:
        job = ScrapeJob(
            workspace_id=workspace_id,
            type=ScrapeJobType.MANUAL,
            scope=ScrapeScope.MATCH,
            status=ScrapeJobStatus.RUNNING,
            source=ScrapeJobSource.API,
            total_targets=n_targets,
            started_at=now,
            created_at=now,
        )
        session.add(job)
        session.flush()

        for match_id in match_ids:
            session.add(
                ScrapeJobTarget(
                    workspace_id=workspace_id,
                    scrape_job_id=job.id,
                    match_id=match_id,
                    status=ScrapeTargetStatus.PENDING,
                    created_at=now,
                )
            )
        session.commit()
        job_id = job.id

    return _SeededJob(workspace_id=workspace_id, job_id=job_id, target_match_ids=match_ids)


@pytest.fixture()
def workspace_id() -> Iterator[uuid.UUID]:
    from app_shared.database import get_session
    from app_shared.enums import WorkspaceStatus
    from app_shared.models import Workspace

    unique = uuid.uuid4().hex[:8]
    with get_session() as session:
        ws = Workspace(
            name=f"Jobs Counters Live {unique}",
            slug=f"jobs-counters-live-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(ws)
        session.commit()
        ws_id = ws.id

    try:
        yield ws_id
    finally:
        from sqlalchemy import text

        with get_session() as session:
            session.execute(
                text("DELETE FROM scrape_job_targets WHERE workspace_id = :ws"), {"ws": ws_id}
            )
            session.execute(
                text("DELETE FROM scrape_jobs WHERE workspace_id = :ws"), {"ws": ws_id}
            )
            session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws_id})
            session.commit()


def _finalize(workspace_id: uuid.UUID, job_id: uuid.UUID) -> None:
    """Inline the same aggregate -> resolve -> write sequence
    `apps/workers/app/workers/tasks_jobs.finalize_jobs` performs for one
    job, against a real session/transaction."""
    from app_shared.database import get_session, set_workspace_context
    from app_shared.jobs.lifecycle import resolve_finalized_status
    from app_shared.jobs.targets import aggregate_counts
    from app_shared.models.jobs import ScrapeJob
    from app_shared.repository import scoped_get

    with get_session() as session:
        set_workspace_context(session, workspace_id)
        job = scoped_get(session, ScrapeJob, job_id, workspace_id)
        assert job is not None

        counts = aggregate_counts(session, job_id, workspace_id)
        job.success_count = counts.success
        job.failure_count = counts.failure
        job.skipped_count = counts.skipped
        job.status = resolve_finalized_status(
            counts.success, counts.failure, counts.skipped, counts.total
        )
        job.completed_at = datetime.now(UTC)
        session.commit()


def _fetch_job(workspace_id: uuid.UUID, job_id: uuid.UUID):
    from app_shared.database import get_session, set_workspace_context
    from app_shared.models.jobs import ScrapeJob
    from app_shared.repository import scoped_get

    with get_session() as session:
        set_workspace_context(session, workspace_id)
        return scoped_get(session, ScrapeJob, job_id, workspace_id)


# --- all-success -> COMPLETED, counters match ---------------------------


def test_all_targets_completed_finalizes_completed_with_matching_counts(
    workspace_id: uuid.UUID,
) -> None:
    from app_shared.database import get_session, set_workspace_context
    from app_shared.enums import ScrapeTargetStatus
    from app_shared.jobs.targets import mark_target

    seeded = _seed_job_with_targets(workspace_id=workspace_id, n_targets=3)

    with get_session() as session:
        set_workspace_context(session, workspace_id)
        for match_id in seeded.target_match_ids:
            mark_target(
                session,
                workspace_id=workspace_id,
                scrape_job_id=seeded.job_id,
                match_id=match_id,
                status=ScrapeTargetStatus.COMPLETED,
            )
        session.commit()

    _finalize(workspace_id, seeded.job_id)

    job = _fetch_job(workspace_id, seeded.job_id)
    assert job.status == "COMPLETED"
    assert job.success_count == 3
    assert job.failure_count == 0
    assert job.completed_at is not None


# --- mixed success + failure -> PARTIAL_FAILED --------------------------


def test_mixed_success_and_failure_finalizes_partial_failed(
    workspace_id: uuid.UUID,
) -> None:
    from app_shared.database import get_session, set_workspace_context
    from app_shared.enums import ScrapeErrorCode, ScrapeTargetStatus
    from app_shared.jobs.targets import mark_target

    seeded = _seed_job_with_targets(workspace_id=workspace_id, n_targets=4)

    with get_session() as session:
        set_workspace_context(session, workspace_id)
        for match_id in seeded.target_match_ids[:2]:
            mark_target(
                session,
                workspace_id=workspace_id,
                scrape_job_id=seeded.job_id,
                match_id=match_id,
                status=ScrapeTargetStatus.COMPLETED,
            )
        for match_id in seeded.target_match_ids[2:]:
            mark_target(
                session,
                workspace_id=workspace_id,
                scrape_job_id=seeded.job_id,
                match_id=match_id,
                status=ScrapeTargetStatus.FAILED,
                error_code=ScrapeErrorCode.HTTP_403,
            )
        session.commit()

    _finalize(workspace_id, seeded.job_id)

    job = _fetch_job(workspace_id, seeded.job_id)
    assert job.status == "PARTIAL_FAILED"
    assert job.success_count == 2
    assert job.failure_count == 2
    assert job.completed_at is not None


# --- all failed (zero success) -> FAILED --------------------------------


def test_all_targets_failed_finalizes_failed(workspace_id: uuid.UUID) -> None:
    from app_shared.database import get_session, set_workspace_context
    from app_shared.enums import ScrapeErrorCode, ScrapeTargetStatus
    from app_shared.jobs.targets import mark_target

    seeded = _seed_job_with_targets(workspace_id=workspace_id, n_targets=2)

    with get_session() as session:
        set_workspace_context(session, workspace_id)
        for match_id in seeded.target_match_ids:
            mark_target(
                session,
                workspace_id=workspace_id,
                scrape_job_id=seeded.job_id,
                match_id=match_id,
                status=ScrapeTargetStatus.FAILED,
                error_code=ScrapeErrorCode.TIMEOUT,
            )
        session.commit()

    _finalize(workspace_id, seeded.job_id)

    job = _fetch_job(workspace_id, seeded.job_id)
    assert job.status == "FAILED"
    assert job.success_count == 0
    assert job.failure_count == 2
    assert job.completed_at is not None


# --- mark_target never touches job counters (only aggregate_counts does) ---


def test_mark_target_alone_never_mutates_job_counters(workspace_id: uuid.UUID) -> None:
    from app_shared.database import get_session, set_workspace_context
    from app_shared.enums import ScrapeTargetStatus
    from app_shared.jobs.targets import mark_target

    seeded = _seed_job_with_targets(workspace_id=workspace_id, n_targets=2)

    with get_session() as session:
        set_workspace_context(session, workspace_id)
        mark_target(
            session,
            workspace_id=workspace_id,
            scrape_job_id=seeded.job_id,
            match_id=seeded.target_match_ids[0],
            status=ScrapeTargetStatus.COMPLETED,
        )
        session.commit()

    # Never finalized/aggregated -- the job row's own counters are
    # untouched by mark_target alone (aggregate_counts/finalize is a
    # separate, explicit step).
    job = _fetch_job(workspace_id, seeded.job_id)
    assert job.success_count == 0
    assert job.failure_count == 0
    assert job.status == "RUNNING"
