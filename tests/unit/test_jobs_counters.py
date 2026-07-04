"""Counter aggregation + target-marking unit tests (SPEC-08 T042, US3, FR-017/018, SC-004).

`app_shared.jobs.targets.aggregate_counts`/`mark_target` and
`apps/workers/app/workers/tasks_jobs.refresh_job_counters` — exercised
against the shared `FakeOrmSession`
(`tests/unit/_jobs_fake_session.py`, extended here to evaluate the
`SELECT status, COUNT(*) ... GROUP BY status` shape + `IS NULL`). Per
`contracts/lifecycle-counters.md`: `aggregate_counts` derives correct
`Counts` from a `GROUP BY status` read; `refresh_job_counters` writes
those totals to the job row in **one** UPDATE, never a per-target
increment; `mark_target` transitions only the target row it resolves and
never mutates the parent job's counters.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import datetime, timezone

from app_shared.enums import (
    ScrapeErrorCode,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.jobs.targets import Counts, aggregate_counts, mark_target
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget

from unit._jobs_fake_session import FakeOrmSession


def _make_job(*, workspace_id: uuid.UUID) -> ScrapeJob:
    now = datetime.now(timezone.utc)
    job = ScrapeJob(
        workspace_id=workspace_id,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.VARIANT,
        status=ScrapeJobStatus.RUNNING,
        total_targets=5,
        source=ScrapeJobSource.API,
        created_at=now,
        started_at=now,
    )
    job.id = uuid.uuid4()
    return job


def _make_target(
    *,
    workspace_id: uuid.UUID,
    scrape_job_id: uuid.UUID,
    status: ScrapeTargetStatus,
) -> ScrapeJobTarget:
    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=scrape_job_id,
        match_id=uuid.uuid4(),
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    target.id = uuid.uuid4()
    return target


# --- aggregate_counts ---------------------------------------------------------


def test_aggregate_counts_groups_by_status() -> None:
    session = FakeOrmSession()
    workspace_id = uuid.uuid4()
    job = _make_job(workspace_id=workspace_id)
    session.seed(job)

    statuses = (
        [ScrapeTargetStatus.COMPLETED] * 3
        + [ScrapeTargetStatus.FAILED] * 2
        + [ScrapeTargetStatus.SKIPPED]
    )
    for status in statuses:
        session.seed(_make_target(workspace_id=workspace_id, scrape_job_id=job.id, status=status))

    counts = aggregate_counts(session, job.id, workspace_id)

    assert counts == Counts(success=3, failure=2, skipped=1, total=6)


def test_aggregate_counts_scoped_to_job_and_workspace() -> None:
    session = FakeOrmSession()
    workspace_id = uuid.uuid4()
    other_workspace_id = uuid.uuid4()
    job = _make_job(workspace_id=workspace_id)
    other_job = _make_job(workspace_id=workspace_id)
    session.seed(job, other_job)

    session.seed(
        _make_target(
            workspace_id=workspace_id, scrape_job_id=job.id, status=ScrapeTargetStatus.COMPLETED
        )
    )
    # A target on a different job in the same workspace must not be counted.
    session.seed(
        _make_target(
            workspace_id=workspace_id,
            scrape_job_id=other_job.id,
            status=ScrapeTargetStatus.FAILED,
        )
    )
    # A target in a different workspace altogether must not be counted.
    session.seed(
        _make_target(
            workspace_id=other_workspace_id,
            scrape_job_id=job.id,
            status=ScrapeTargetStatus.FAILED,
        )
    )

    counts = aggregate_counts(session, job.id, workspace_id)

    assert counts == Counts(success=1, failure=0, skipped=0, total=1)


def test_aggregate_counts_zero_targets() -> None:
    session = FakeOrmSession()
    workspace_id = uuid.uuid4()
    job = _make_job(workspace_id=workspace_id)
    session.seed(job)

    counts = aggregate_counts(session, job.id, workspace_id)

    assert counts == Counts(success=0, failure=0, skipped=0, total=0)


# --- refresh_job_counters: one UPDATE, never per-target -----------------------

_REFRESH_COUNTERS_CHECK = """
import sys
sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

import uuid
from datetime import datetime, timezone

from _jobs_fake_session import FakeOrmSession
from app_shared.enums import (
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget

import app.workers.tasks_jobs as tasks_jobs

session = FakeOrmSession()
workspace_id = uuid.uuid4()
now = datetime.now(timezone.utc)

job = ScrapeJob(
    workspace_id=workspace_id,
    type=ScrapeJobType.MANUAL,
    scope=ScrapeScope.VARIANT,
    status=ScrapeJobStatus.RUNNING,
    total_targets=3,
    source=ScrapeJobSource.API,
    created_at=now,
    started_at=now,
)
job.id = uuid.uuid4()
session.seed(job)

for status in (
    ScrapeTargetStatus.COMPLETED,
    ScrapeTargetStatus.COMPLETED,
    ScrapeTargetStatus.FAILED,
):
    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=uuid.uuid4(),
        status=status,
        created_at=now,
    )
    target.id = uuid.uuid4()
    session.seed(target)

# `refresh_job_counters` must resolve its counts via exactly ONE scoped
# read (aggregate_counts's GROUP BY) -- never a per-target query/loop.
# Track `session.execute` call count around the call to prove this.
execute_calls = []
original_execute = FakeOrmSession.execute


def tracking_execute(self, stmt):
    execute_calls.append(1)
    return original_execute(self, stmt)


FakeOrmSession.execute = tracking_execute

before = len(execute_calls)
counts = tasks_jobs.refresh_job_counters(session, job, workspace_id)
after = len(execute_calls)

FakeOrmSession.execute = original_execute

if after - before != 1:
    print("NOT_ONE_READ:" + str(after - before))
    sys.exit(1)

if job.success_count != 2 or job.failure_count != 1 or job.skipped_count != 0:
    print("WRONG_COUNTS:" + str((job.success_count, job.failure_count, job.skipped_count)))
    sys.exit(1)

if counts.success != 2 or counts.failure != 1 or counts.total != 3:
    print("WRONG_RETURNED_COUNTS:" + str(counts))
    sys.exit(1)

print("OK")
sys.exit(0)
"""


def test_refresh_job_counters_writes_counts_in_one_update() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _REFRESH_COUNTERS_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **__import__("os").environ,
            "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
            "REDIS_URL": "redis://redis:6379/0",
            "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
            "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
            "SCRAPYD_USERNAME": "scrapyd",
            "SCRAPYD_PASSWORD": "change-me",
            "JWT_SECRET": "test-jwt-secret",
            "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
        },
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


# --- mark_target: single writer, never touches job counters -------------------


def test_mark_target_transitions_status_and_timestamps() -> None:
    session = FakeOrmSession()
    workspace_id = uuid.uuid4()
    job = _make_job(workspace_id=workspace_id)
    session.seed(job)
    target = _make_target(
        workspace_id=workspace_id, scrape_job_id=job.id, status=ScrapeTargetStatus.PENDING
    )
    session.seed(target)

    mark_target(
        session,
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=target.match_id,
        status=ScrapeTargetStatus.STARTED,
    )

    assert target.status == ScrapeTargetStatus.STARTED
    assert target.started_at is not None
    assert target.completed_at is None

    mark_target(
        session,
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=target.match_id,
        status=ScrapeTargetStatus.FAILED,
        error_code=ScrapeErrorCode.HTTP_404,
    )

    assert target.status == ScrapeTargetStatus.FAILED
    assert target.completed_at is not None
    assert target.error_code == ScrapeErrorCode.HTTP_404


def test_mark_target_completed_sets_completed_at_no_error_code() -> None:
    session = FakeOrmSession()
    workspace_id = uuid.uuid4()
    job = _make_job(workspace_id=workspace_id)
    session.seed(job)
    target = _make_target(
        workspace_id=workspace_id, scrape_job_id=job.id, status=ScrapeTargetStatus.STARTED
    )
    session.seed(target)

    mark_target(
        session,
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=target.match_id,
        status=ScrapeTargetStatus.COMPLETED,
    )

    assert target.status == ScrapeTargetStatus.COMPLETED
    assert target.completed_at is not None
    assert target.error_code is None


def test_mark_target_never_mutates_job_counters() -> None:
    session = FakeOrmSession()
    workspace_id = uuid.uuid4()
    job = _make_job(workspace_id=workspace_id)
    job.success_count = 0
    job.failure_count = 0
    job.skipped_count = 0
    session.seed(job)
    target = _make_target(
        workspace_id=workspace_id, scrape_job_id=job.id, status=ScrapeTargetStatus.PENDING
    )
    session.seed(target)

    mark_target(
        session,
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=target.match_id,
        status=ScrapeTargetStatus.FAILED,
        error_code=ScrapeErrorCode.TIMEOUT,
    )

    assert job.success_count == 0
    assert job.failure_count == 0
    assert job.skipped_count == 0


def test_mark_target_unresolvable_target_is_a_no_op() -> None:
    session = FakeOrmSession()
    workspace_id = uuid.uuid4()
    job = _make_job(workspace_id=workspace_id)
    session.seed(job)

    # No target seeded for this match_id -- must not raise.
    mark_target(
        session,
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=uuid.uuid4(),
        status=ScrapeTargetStatus.COMPLETED,
    )
