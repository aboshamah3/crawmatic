"""`redispatch_pending_jobs` task unit tests (PLAN_AMAZON_NOON_PRICING Phase 1).

`apps/workers/app/workers/tasks_jobs.py::redispatch_pending_jobs` — fake
session (`FakeOrmSession`) + a stubbed `enqueue` (never a real DB/broker).
The sweep must re-enqueue `SCRAPE_DISPATCH_JOB` for exactly two job
shapes and nothing else:

- a job still `PENDING` with `started_at IS NULL` (lost dispatch delivery);
- a RUNNING job holding >= 1 `DEFERRED` target (requeue-cap overflow that
  `dispatch_job` selects but nothing ever re-delivered).

A RUNNING job whose targets are all STARTED/terminal/bare-PENDING is left
alone — stalled bare-PENDING targets belong to `recover_stalled_batches`,
not this sweep.

Loaded in a fresh subprocess (mirrors `test_jobs_stall_recovery.py`) for
the same two reasons: `apps/api`/`apps/workers` each ship a top-level
`app` package, and `celery_app.py` calls `get_settings()` at module scope.
"""

from __future__ import annotations

import os
import subprocess
import sys

_REDISPATCH_CHECK = """
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

from _jobs_fake_session import FakeOrmSession
from app_shared.enums import (
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.task_names import SCRAPE_DISPATCH_JOB

import app.workers.tasks_jobs as tasks_jobs

fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


enqueued = []


def fake_enqueue(name, *, queue=None, kwargs=None):
    enqueued.append({"name": name, "queue": queue, "kwargs": kwargs})


tasks_jobs.get_session = fake_get_session
# The cross-tenant `_scan_job_refs` sweep runs on the BYPASSRLS system
# session (mushtryati F-1) -- point it at the same fake, so the scan
# still reads the seeded rows and no real engine is ever constructed.
tasks_jobs.get_system_session = fake_get_session
tasks_jobs.set_workspace_context = lambda session, workspace_id: None
tasks_jobs.enqueue = fake_enqueue

workspace_id = uuid.uuid4()
now = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _job(status, started):
    job = ScrapeJob(
        workspace_id=workspace_id,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.MATCH,
        status=status,
        total_targets=1,
        source=ScrapeJobSource.API,
        created_at=now,
        started_at=now if started else None,
    )
    job.id = uuid.uuid4()
    fake_session.seed(job)
    return job


def _target(job, status, dispatched=False):
    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=uuid.uuid4(),
        status=status,
        created_at=now,
        dispatched_at=now if dispatched else None,
    )
    target.id = uuid.uuid4()
    fake_session.seed(target)
    return target


# 1) Never-started PENDING job (lost dispatch delivery) -> re-enqueued.
job_lost = _job(ScrapeJobStatus.PENDING, started=False)
_target(job_lost, ScrapeTargetStatus.PENDING)

# 2) RUNNING job holding a DEFERRED target -> re-enqueued.
job_deferred = _job(ScrapeJobStatus.RUNNING, started=True)
_target(job_deferred, ScrapeTargetStatus.DEFERRED)
_target(job_deferred, ScrapeTargetStatus.COMPLETED)

# 3) RUNNING job, targets STARTED/terminal/already-dispatched-PENDING only
#    -> untouched (a PENDING target that HAS a `dispatched_at` stamp is
#    queued on a scrapyd node; if it never progresses it belongs to
#    `recover_stalled_batches`, never to this sweep).
job_inflight = _job(ScrapeJobStatus.RUNNING, started=True)
_target(job_inflight, ScrapeTargetStatus.STARTED)
_target(job_inflight, ScrapeTargetStatus.PENDING, dispatched=True)

# 4) Terminal job with a leftover DEFERRED target -> untouched.
job_done = _job(ScrapeJobStatus.COMPLETED, started=True)
_target(job_done, ScrapeTargetStatus.DEFERRED)

tasks_jobs.redispatch_pending_jobs()

expected = {str(job_lost.id), str(job_deferred.id)}
got = {call["kwargs"]["scrape_job_id"] for call in enqueued}
if got != expected:
    print("WRONG_JOBS_REDISPATCHED:" + str(got))
    sys.exit(1)

for call in enqueued:
    if call["name"] != SCRAPE_DISPATCH_JOB or call["queue"] != "scrape_dispatch":
        print("WRONG_TASK_OR_QUEUE:" + str(call))
        sys.exit(1)
    if call["kwargs"]["workspace_id"] != str(workspace_id):
        print("WRONG_WORKSPACE:" + str(call))
        sys.exit(1)

# Idempotent sweep: a second pass re-enqueues the same two jobs again
# (pacing lives in the dispatch client's TTL guard, not here).
enqueued.clear()
tasks_jobs.redispatch_pending_jobs()
if {call["kwargs"]["scrape_job_id"] for call in enqueued} != expected:
    print("SECOND_PASS_DIVERGED")
    sys.exit(1)

print("OK")
sys.exit(0)
"""

_MID_JOB_LOSS_CHECK = """
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

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

fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


enqueued = []

tasks_jobs.get_session = fake_get_session
tasks_jobs.get_system_session = fake_get_session
tasks_jobs.set_workspace_context = lambda session, workspace_id: None
tasks_jobs.enqueue = lambda name, *, queue=None, kwargs=None: enqueued.append(kwargs)

workspace_id = uuid.uuid4()
now = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _job(status, started):
    job = ScrapeJob(
        workspace_id=workspace_id,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.MATCH,
        status=status,
        total_targets=1,
        source=ScrapeJobSource.API,
        created_at=now,
        started_at=now if started else None,
    )
    job.id = uuid.uuid4()
    fake_session.seed(job)
    return job


def _target(job, status, dispatched):
    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=uuid.uuid4(),
        status=status,
        created_at=now,
        dispatched_at=now if dispatched else None,
    )
    target.id = uuid.uuid4()
    fake_session.seed(target)
    return target


# A dispatch delivery lost AFTER the job started: the job is RUNNING with
# `started_at` set, holds no DEFERRED target, and its remaining targets are
# PENDING with NO `dispatched_at` stamp -- nothing else in the system will
# ever re-enqueue them (`recover_stalled_batches` owns only targets that
# WERE dispatched). This sweep must.
job_mid_loss = _job(ScrapeJobStatus.RUNNING, started=True)
_target(job_mid_loss, ScrapeTargetStatus.COMPLETED, dispatched=True)
_target(job_mid_loss, ScrapeTargetStatus.PENDING, dispatched=False)

# Control: same shape but every PENDING target already dispatched.
job_inflight = _job(ScrapeJobStatus.RUNNING, started=True)
_target(job_inflight, ScrapeTargetStatus.PENDING, dispatched=True)

tasks_jobs.redispatch_pending_jobs()

got = {call["scrape_job_id"] for call in enqueued}
if got != {str(job_mid_loss.id)}:
    print("WRONG_JOBS_REDISPATCHED:" + str(sorted(got)))
    sys.exit(1)

print("OK")
sys.exit(0)
"""

_REDISPATCH_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scraper-a:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def test_redispatch_pending_jobs_targets_only_lost_and_deferred() -> None:
    env = {**os.environ, **_REDISPATCH_ENV}
    result = subprocess.run(
        [sys.executable, "-c", _REDISPATCH_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=None,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


def test_redispatch_covers_undispatched_pending_mid_job() -> None:
    """A dispatch delivery lost AFTER job start leaves PENDING/NULL targets
    that nothing re-enqueues: eligibility must include 'has PENDING targets
    never dispatched', not only `started_at IS NULL` or DEFERRED-exists."""
    env = {**os.environ, **_REDISPATCH_ENV}
    result = subprocess.run(
        [sys.executable, "-c", _MID_JOB_LOSS_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=None,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"
