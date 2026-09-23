"""`finalize_jobs` must commit ONE transaction per job (2026-09-23 prod incident).

The sweep spans every workspace and `app.workspace_id` is a
transaction-local GUC. Until this fix the task held every job's dirty
counter update in one session and committed once at the end, so as soon
as TWO workspaces had a non-terminal job at the same time the autoflush
after the second `set_workspace_context` ran the first job's UPDATE under
the wrong RLS predicate, matched 0 rows, and `StaleDataError` killed the
whole task every minute -- no job in any workspace finalized (Mushtryati
run 01a0cfd6 sat at stale counters while 992 targets had completed).

Two guarantees, each driven through the shipped task in a subprocess
(the `tests/unit/test_webhook_enqueue_seams.py` pattern, for the same
`apps/api` vs `apps/workers` top-level `app` package clash):

1. a commit happens BEFORE the workspace context is switched to the next job;
2. one job that blows up is rolled back, the others still finalize, and the
   failure is re-raised after the sweep so the task run stays visible as failed.
"""

from __future__ import annotations

import os
import subprocess
import sys

_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def _run(script_body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script_body],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **_ENV},
    )


_SETUP = """
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

events = []


class RecordingSession(FakeOrmSession):
    def commit(self):
        super().commit()
        events.append("commit")

    def rollback(self):
        super().rollback()
        events.append("rollback")


fake_session = RecordingSession()


@contextmanager
def fake_get_session():
    yield fake_session


def fake_set_workspace_context(session, workspace_id):
    events.append("ctx:" + str(workspace_id))


tasks_jobs.get_session = fake_get_session
tasks_jobs.get_system_session = fake_get_session
tasks_jobs.set_workspace_context = fake_set_workspace_context
tasks_jobs.write_outbox_message = lambda *a, **k: None


def make_job(workspace_id):
    now = datetime.now(timezone.utc)
    job = ScrapeJob(
        workspace_id=workspace_id,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.VARIANT,
        status=ScrapeJobStatus.RUNNING,
        total_targets=1,
        source=ScrapeJobSource.API,
        created_at=now,
        started_at=now,
    )
    job.id = uuid.uuid4()
    fake_session.seed(job)
    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=uuid.uuid4(),
        status=ScrapeTargetStatus.COMPLETED,
        created_at=now,
    )
    target.id = uuid.uuid4()
    fake_session.seed(target)
    return job


ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
job_a, job_b = make_job(ws_a), make_job(ws_b)
"""


def test_the_context_switch_to_the_next_workspace_happens_after_a_commit() -> None:
    result = _run(
        _SETUP
        + """
tasks_jobs.finalize_jobs()

expected = ["ctx:" + str(ws_a), "commit", "ctx:" + str(ws_b), "commit"]
if events != expected:
    print("EVENTS:" + repr(events))
    sys.exit(1)
if job_a.status != ScrapeJobStatus.COMPLETED or job_b.status != ScrapeJobStatus.COMPLETED:
    print("STATUSES:" + repr((job_a.status, job_b.status)))
    sys.exit(1)
print("OK")
"""
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "OK"


def test_one_failing_job_is_rolled_back_and_the_others_still_finalize() -> None:
    result = _run(
        _SETUP
        + """
real_refresh = tasks_jobs.refresh_job_counters


def exploding_refresh(session, job, workspace_id):
    if job.id == job_a.id:
        raise RuntimeError("simulated StaleDataError on job A")
    return real_refresh(session, job, workspace_id)


tasks_jobs.refresh_job_counters = exploding_refresh

raised = False
try:
    tasks_jobs.finalize_jobs()
except RuntimeError:
    raised = True  # re-raised AFTER the sweep, so the task run still reads as failed
if not raised:
    print("FAILURE_WAS_SWALLOWED")
    sys.exit(1)

expected = ["ctx:" + str(ws_a), "rollback", "ctx:" + str(ws_b), "commit"]
if events != expected:
    print("EVENTS:" + repr(events))
    sys.exit(1)
if job_a.status != ScrapeJobStatus.RUNNING:
    print("JOB_A_TOUCHED:" + repr(job_a.status))
    sys.exit(1)
if job_b.status != ScrapeJobStatus.COMPLETED:
    print("JOB_B_NOT_FINALIZED:" + repr(job_b.status))
    sys.exit(1)
print("OK")
"""
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "OK"
