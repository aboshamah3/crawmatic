"""`app_shared.jobs.reaper` — the STARTED-target reaper + hard job deadline (EPA A3/B2).

The failure this covers, observed in production: a scrapyd container is
replaced mid-job. Every target that had already been claimed sits at
`STARTED` forever — the spider that owned it no longer exists, so nothing
will ever stamp its terminal status, `finalize_jobs` never sees "all
targets terminal", and the job dangles `RUNNING` indefinitely while the
customer's refresh silently never completes.

Two independent sweeps close it, and both are tested here against a real
(in-memory SQLite) session rather than a fake, because the whole point of
the module is the SQL: set-based `UPDATE ... WHERE` statements whose
`WHERE` clauses are the safety property. A fake that does not evaluate
predicates could not distinguish "reverts the stale target" from "reverts
every target".

1. `revert_stale_started_targets` — a target STARTED longer ago than the
   browser lock could possibly have been held (`MATCH_LOCK_BROWSER_TTL_SECONDS`
   1800 + grace) goes back to PENDING with its dispatch stamps cleared, so
   the ordinary redispatch path picks it up again. Clearing `dispatched_at`
   is load-bearing, not cosmetic: BOTH `redispatch_pending_jobs` (which
   decides whether a job needs re-enqueueing) and `dispatch_job` (which
   selects the rows to POST) match `PENDING AND dispatched_at IS NULL`.
   A revert that left the stamp in place would produce a PENDING target
   that no dispatcher ever selects — the same wedge in a new costume.

2. `fail_targets_past_job_deadline` — a job past its hard runtime ceiling
   has every non-terminal target failed with `JOB_DEADLINE_EXCEEDED`, so
   `finalize_jobs` can close it on the very next sweep. This is the
   backstop for the case sweep 1 cannot fix (a target that keeps being
   re-dispatched and re-lost), and it is deliberately scoped to RUNNING
   jobs only: an old COMPLETED job's rows are history, not work in flight.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app_shared.enums import (
    MatchPriority,
    ScrapeErrorCode,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.jobs.reaper import (
    fail_targets_past_job_deadline,
    revert_stale_started_targets,
)
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget

NOW = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)

#: Mirrors the `SCRAPE_STARTED_REAP_AFTER_SECONDS` default (browser lock
#: TTL 1800 + 300s grace). The functions take it as an argument; the
#: settings wiring is the task's business, not the sweep's.
REAP_AFTER = 2_100
MAX_RUNTIME = 43_200


@pytest.fixture()
def session() -> Iterator[Session]:
    """A real engine over the two job tables.

    SQLite does not enforce foreign keys unless `PRAGMA foreign_keys=ON`
    (not set here), so the `workspaces` / `dispatch_intents` /
    `domain_strategy_methods` parents these tables reference never need
    to exist for this test's purposes.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    ScrapeJob.metadata.create_all(
        engine, tables=[ScrapeJob.__table__, ScrapeJobTarget.__table__]
    )
    factory = sessionmaker(bind=engine)
    db = factory()
    yield db
    db.close()
    engine.dispose()


WORKSPACE_ID = uuid.uuid4()


def _make_job(
    session: Session,
    *,
    started_seconds_ago: int,
    status: ScrapeJobStatus = ScrapeJobStatus.RUNNING,
) -> ScrapeJob:
    job = ScrapeJob(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.MATCH,
        status=status,
        priority=MatchPriority.NORMAL,
        total_targets=0,
        success_count=0,
        failure_count=0,
        skipped_count=0,
        source=ScrapeJobSource.API,
        created_at=NOW - timedelta(seconds=started_seconds_ago),
        started_at=NOW - timedelta(seconds=started_seconds_ago),
    )
    session.add(job)
    session.flush()
    return job


def _make_target(
    session: Session,
    job: ScrapeJob,
    *,
    status: ScrapeTargetStatus,
    started_seconds_ago: int | None = None,
    dispatched_seconds_ago: int | None = None,
    locked_seconds_ago: int | None = None,
) -> ScrapeJobTarget:
    target = ScrapeJobTarget(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        scrape_job_id=job.id,
        match_id=uuid.uuid4(),
        status=status,
        created_at=NOW - timedelta(seconds=7_200),
        started_at=(
            None
            if started_seconds_ago is None
            else NOW - timedelta(seconds=started_seconds_ago)
        ),
        dispatched_at=(
            None
            if dispatched_seconds_ago is None
            else NOW - timedelta(seconds=dispatched_seconds_ago)
        ),
        locked_at=(
            None
            if locked_seconds_ago is None
            else NOW - timedelta(seconds=locked_seconds_ago)
        ),
        dispatch_intent_id=uuid.uuid4(),
    )
    session.add(target)
    session.flush()
    return target


# --- revert_stale_started_targets ------------------------------------


def test_a_started_target_orphaned_by_a_redeploy_goes_back_to_pending(
    session: Session,
) -> None:
    """The core B2 case. `started_at` 2200s ago is past any legitimate
    browser lock, so no live spider can still own this row — the container
    that claimed it is gone. Reverting it (stamps cleared) is what puts it
    back in front of `dispatch_job`, whose selection is
    `PENDING AND dispatched_at IS NULL`."""
    job = _make_job(session, started_seconds_ago=3_000)
    target = _make_target(
        session,
        job,
        status=ScrapeTargetStatus.STARTED,
        started_seconds_ago=2_200,
        dispatched_seconds_ago=2_300,
        locked_seconds_ago=2_200,
    )

    reverted = revert_stale_started_targets(
        session, now=NOW, older_than_seconds=REAP_AFTER
    )

    assert reverted == 1
    session.refresh(target)
    assert target.status is ScrapeTargetStatus.PENDING
    assert target.started_at is None
    assert target.dispatched_at is None
    assert target.dispatch_intent_id is None
    # The lock is the reason `recover_stalled_batches` skips a row as
    # "live"; a stale one left behind would make the reverted target look
    # in-flight to every other sweep.
    assert target.locked_at is None


def test_a_freshly_started_target_is_left_alone(session: Session) -> None:
    """A target claimed 60s ago is a spider doing its job. Reverting it
    would double-dispatch live work and corrupt the very counters
    `finalize_jobs` reads."""
    job = _make_job(session, started_seconds_ago=3_000)
    target = _make_target(
        session,
        job,
        status=ScrapeTargetStatus.STARTED,
        started_seconds_ago=60,
        dispatched_seconds_ago=120,
    )

    assert revert_stale_started_targets(
        session, now=NOW, older_than_seconds=REAP_AFTER
    ) == 0
    session.refresh(target)
    assert target.status is ScrapeTargetStatus.STARTED
    assert target.started_at is not None


def test_only_started_targets_are_reverted(session: Session) -> None:
    """PENDING is `recover_stalled_batches`' territory and the terminal
    statuses are history. This sweep owns exactly one status, so an old
    COMPLETED/FAILED row can never be resurrected into the queue."""
    job = _make_job(session, started_seconds_ago=10_000)
    pending = _make_target(
        session,
        job,
        status=ScrapeTargetStatus.PENDING,
        dispatched_seconds_ago=9_000,
    )
    completed = _make_target(
        session,
        job,
        status=ScrapeTargetStatus.COMPLETED,
        started_seconds_ago=9_000,
        dispatched_seconds_ago=9_500,
    )

    assert revert_stale_started_targets(
        session, now=NOW, older_than_seconds=REAP_AFTER
    ) == 0
    session.refresh(pending)
    session.refresh(completed)
    assert pending.status is ScrapeTargetStatus.PENDING
    assert completed.status is ScrapeTargetStatus.COMPLETED


def test_a_started_target_with_no_started_at_is_not_reverted(
    session: Session,
) -> None:
    """`NULL - interval` is NULL, so a bare `started_at < cutoff` would
    silently exclude it anyway — the explicit `IS NOT NULL` says so on
    purpose rather than by accident of three-valued logic."""
    job = _make_job(session, started_seconds_ago=10_000)
    target = _make_target(
        session,
        job,
        status=ScrapeTargetStatus.STARTED,
        started_seconds_ago=None,
        dispatched_seconds_ago=9_000,
    )

    assert revert_stale_started_targets(
        session, now=NOW, older_than_seconds=REAP_AFTER
    ) == 0
    session.refresh(target)
    assert target.status is ScrapeTargetStatus.STARTED


# --- fail_targets_past_job_deadline ----------------------------------


def test_an_expired_job_has_every_non_terminal_target_failed(
    session: Session,
) -> None:
    """13h > the 12h ceiling. PENDING and STARTED are failed with the
    deadline code so the very next `finalize_jobs` sweep sees "all
    targets terminal" and can finally close the job; the COMPLETED
    target's outcome is real data and must survive untouched."""
    job = _make_job(session, started_seconds_ago=13 * 3_600)
    pending = _make_target(session, job, status=ScrapeTargetStatus.PENDING)
    started = _make_target(
        session,
        job,
        status=ScrapeTargetStatus.STARTED,
        started_seconds_ago=300,
        dispatched_seconds_ago=600,
    )
    completed = _make_target(
        session,
        job,
        status=ScrapeTargetStatus.COMPLETED,
        started_seconds_ago=5_000,
        dispatched_seconds_ago=5_200,
    )

    failed = fail_targets_past_job_deadline(
        session, now=NOW, max_runtime_seconds=MAX_RUNTIME
    )

    assert failed == 2
    for target in (pending, started):
        session.refresh(target)
        assert target.status is ScrapeTargetStatus.FAILED
        assert target.error_code is ScrapeErrorCode.JOB_DEADLINE_EXCEEDED
        # SQLite has no timezone-aware storage type, so the aware `NOW`
        # `TZDateTime` bound comes back naive on this engine only —
        # Postgres renders the column `TIMESTAMPTZ` and round-trips the
        # offset. The instant is what this test is about, so compare it
        # after re-attaching UTC rather than weakening the assertion.
        assert target.completed_at is not None
        assert target.completed_at.replace(tzinfo=timezone.utc) == NOW
    session.refresh(completed)
    assert completed.status is ScrapeTargetStatus.COMPLETED
    assert completed.error_code is None


def test_a_deferred_target_on_an_expired_job_is_also_failed(
    session: Session,
) -> None:
    """DEFERRED is non-terminal by design (the requeue-cap overflow
    status), so a job whose remaining work is all DEFERRED would dangle
    forever if the deadline sweep skipped it."""
    job = _make_job(session, started_seconds_ago=13 * 3_600)
    deferred = _make_target(session, job, status=ScrapeTargetStatus.DEFERRED)

    assert fail_targets_past_job_deadline(
        session, now=NOW, max_runtime_seconds=MAX_RUNTIME
    ) == 1
    session.refresh(deferred)
    assert deferred.status is ScrapeTargetStatus.FAILED
    assert deferred.error_code is ScrapeErrorCode.JOB_DEADLINE_EXCEEDED


def test_a_job_inside_its_deadline_is_untouched(session: Session) -> None:
    """2h into a 12h ceiling is an ordinary long run, not a wedge. Failing
    its targets would destroy live work and bill the customer for a
    refresh that was proceeding normally."""
    job = _make_job(session, started_seconds_ago=2 * 3_600)
    target = _make_target(session, job, status=ScrapeTargetStatus.PENDING)

    assert fail_targets_past_job_deadline(
        session, now=NOW, max_runtime_seconds=MAX_RUNTIME
    ) == 0
    session.refresh(target)
    assert target.status is ScrapeTargetStatus.PENDING
    assert target.completed_at is None


def test_targets_of_a_non_running_job_are_untouched(session: Session) -> None:
    """A COMPLETED job that started days ago is finished history. Only a
    job still claiming to be RUNNING can be wedged, so only those are
    swept — otherwise every archived job would be rewritten on every
    tick."""
    job = _make_job(
        session,
        started_seconds_ago=5 * 86_400,
        status=ScrapeJobStatus.COMPLETED,
    )
    target = _make_target(session, job, status=ScrapeTargetStatus.PENDING)

    assert fail_targets_past_job_deadline(
        session, now=NOW, max_runtime_seconds=MAX_RUNTIME
    ) == 0
    session.refresh(target)
    assert target.status is ScrapeTargetStatus.PENDING
    assert target.error_code is None


def test_a_job_that_never_started_has_no_deadline(session: Session) -> None:
    """`started_at IS NULL` means dispatch never began — there is no clock
    to have expired, and `redispatch_pending_jobs` owns that case."""
    job = _make_job(session, started_seconds_ago=13 * 3_600)
    job.started_at = None
    session.flush()
    target = _make_target(session, job, status=ScrapeTargetStatus.PENDING)

    assert fail_targets_past_job_deadline(
        session, now=NOW, max_runtime_seconds=MAX_RUNTIME
    ) == 0
    session.refresh(target)
    assert target.status is ScrapeTargetStatus.PENDING


def test_the_two_sweeps_compose_without_fighting_each_other(
    session: Session,
) -> None:
    """Run order matters: the reaper reverts first, then the deadline
    sweep fails what is left. A target reverted to PENDING on an expired
    job must still be failed by the deadline pass in the SAME tick —
    otherwise the reaper would keep handing the deadline sweep's work
    back to the dispatcher forever."""
    job = _make_job(session, started_seconds_ago=13 * 3_600)
    orphan = _make_target(
        session,
        job,
        status=ScrapeTargetStatus.STARTED,
        started_seconds_ago=2_200,
        dispatched_seconds_ago=2_300,
    )

    assert revert_stale_started_targets(
        session, now=NOW, older_than_seconds=REAP_AFTER
    ) == 1
    assert fail_targets_past_job_deadline(
        session, now=NOW, max_runtime_seconds=MAX_RUNTIME
    ) == 1

    session.refresh(orphan)
    assert orphan.status is ScrapeTargetStatus.FAILED
    assert orphan.error_code is ScrapeErrorCode.JOB_DEADLINE_EXCEEDED
