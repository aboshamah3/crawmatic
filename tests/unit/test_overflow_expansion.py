"""Overflow re-dispatch expansion unit tests (SPEC-11 US3 T025, FR-018/019).

Two independent checks, both required to actually run (no Docker daemon
in this build env -- these are pure-logic/fake-session unit tests, not
integration tests, so neither may skip):

1. ``apps/workers/app/workers/tasks_jobs.py::dispatch_job``'s target-
   expansion query (~line 212, `contracts/overflow-dispatch.md` §4)
   selects targets in **both** ``PENDING`` and ``DEFERRED`` -- an
   overflowed target is picked up by the next dispatch, not stranded.
   Loaded in a fresh subprocess (mirrors ``test_jobs_dispatch_task.py``):
   ``apps/api`` and ``apps/workers`` both ship a top-level ``app``
   package (ambiguous import if another test already imported the other
   one in-process), and ``celery_app.py`` calls ``get_settings()`` at
   module scope.
2. ``app_shared.jobs.targets.mark_target(status=DEFERRED,
   error_code=RATE_LIMITED)`` stamps no ``completed_at``/``started_at``
   and persists the error code (T026) -- pure SQLAlchemy, no scrapy/
   celery import, so this half runs in-process against the shared
   ``FakeOrmSession`` (no subprocess isolation needed).

The stalled-target reaper query (``recover_stalled_batches``, ~line 352)
is a *separate* query that still matches only bare ``PENDING`` --
unaffected by this change and not exercised here (T028 leaves it as-is).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

from app_shared.enums import ScrapeErrorCode, ScrapeTargetStatus
from app_shared.jobs.targets import mark_target
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget

from unit._jobs_fake_session import FakeOrmSession

# --- 1. dispatch_job expansion query selects PENDING + DEFERRED -----------

_EXPANSION_CHECK = """
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

import requests

from _jobs_fake_session import FakeOrmSession
from app_shared.enums import (
    MatchPriority,
    MatchStatus,
    ScrapeErrorCode,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.scrapyd.client import ScrapydDispatchClient as RealClient

import app.workers.tasks_jobs as tasks_jobs

# --- fakes -------------------------------------------------------------

class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, name, value, *, nx=False, ex=None):
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True

    def get(self, name):
        return self.store.get(name)

    def delete(self, *names):
        removed = 0
        for name in names:
            if self.store.pop(name, None) is not None:
                removed += 1
        return removed


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


calls = []


def fake_post(url, *, data, auth, timeout):
    calls.append({"url": url, "data": dict(data), "auth": auth})
    jobid = "job-" + str(len(calls))
    return FakeResponse(200, {"status": "ok", "jobid": jobid})


fake_redis = FakeRedis()


def client_factory(*, settings=None):
    http_session = requests.Session()
    http_session.post = fake_post
    return RealClient(settings=settings, redis_client=fake_redis, session=http_session)


tasks_jobs.ScrapydDispatchClient = client_factory


def fake_set_workspace_context(session, workspace_id):
    pass


tasks_jobs.set_workspace_context = fake_set_workspace_context

fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


tasks_jobs.get_session = fake_get_session

# --- fixture data: one job, one PENDING target, one DEFERRED target ------
# (distinct domains so each lands in its own batch -- makes call-count
# assertions unambiguous.)

workspace_id = uuid.uuid4()
job_id = uuid.uuid4()
competitor_pending_id = uuid.uuid4()
competitor_deferred_id = uuid.uuid4()
match_pending_id = uuid.uuid4()
match_deferred_id = uuid.uuid4()
now = datetime.now(timezone.utc)

job = ScrapeJob(
    workspace_id=workspace_id,
    type=ScrapeJobType.MANUAL,
    scope=ScrapeScope.MATCH,
    status=ScrapeJobStatus.RUNNING,
    total_targets=2,
    source=ScrapeJobSource.API,
    created_at=now,
    started_at=now,
)
job.id = job_id
fake_session.seed(job)

match_pending = CompetitorProductMatch(
    workspace_id=workspace_id,
    product_id=uuid.uuid4(),
    product_variant_id=uuid.uuid4(),
    competitor_id=competitor_pending_id,
    competitor_url="https://pending.example.com/p",
    normalized_competitor_url="https://pending.example.com/p",
    url_pattern="https://pending.example.com/p",
    url_pattern_version=1,
    priority=MatchPriority.NORMAL,
    status=MatchStatus.ACTIVE,
)
match_pending.id = match_pending_id
match_deferred = CompetitorProductMatch(
    workspace_id=workspace_id,
    product_id=uuid.uuid4(),
    product_variant_id=uuid.uuid4(),
    competitor_id=competitor_deferred_id,
    competitor_url="https://deferred.example.com/p",
    normalized_competitor_url="https://deferred.example.com/p",
    url_pattern="https://deferred.example.com/p",
    url_pattern_version=1,
    priority=MatchPriority.NORMAL,
    status=MatchStatus.ACTIVE,
)
match_deferred.id = match_deferred_id
fake_session.seed(match_pending, match_deferred)

competitor_pending = Competitor(
    workspace_id=workspace_id, name="Pending", domain="pending.example.com"
)
competitor_pending.id = competitor_pending_id
competitor_deferred = Competitor(
    workspace_id=workspace_id, name="Deferred", domain="deferred.example.com"
)
competitor_deferred.id = competitor_deferred_id
fake_session.seed(competitor_pending, competitor_deferred)

target_pending = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=job_id,
    match_id=match_pending_id,
    status=ScrapeTargetStatus.PENDING,
    created_at=now,
)
target_pending.id = uuid.uuid4()
target_deferred = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=job_id,
    match_id=match_deferred_id,
    status=ScrapeTargetStatus.DEFERRED,
    error_code=ScrapeErrorCode.RATE_LIMITED,
    created_at=now,
)
target_deferred.id = uuid.uuid4()
fake_session.seed(target_pending, target_deferred)

# --- re-dispatch ----------------------------------------------------------

tasks_jobs.dispatch_job(str(job_id), str(workspace_id))

if len(calls) != 2:
    print("EXPECTED_TWO_SCHEDULE_CALLS_GOT:" + str(len(calls)))
    sys.exit(1)

dispatched_match_ids = set()
for call in calls:
    for match_id in call["data"]["match_ids"]:
        dispatched_match_ids.add(str(match_id))

if dispatched_match_ids != {str(match_pending_id), str(match_deferred_id)}:
    print("MATCH_IDS_MISMATCH:" + str(dispatched_match_ids))
    sys.exit(1)

print("OK")
sys.exit(0)
"""

_EXPANSION_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def test_dispatch_job_expansion_selects_pending_and_deferred() -> None:
    env = {**os.environ, **_EXPANSION_ENV}
    result = subprocess.run(
        [sys.executable, "-c", _EXPANSION_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=None,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


# --- 2. mark_target(DEFERRED, RATE_LIMITED): no timestamps, code persists -


def _make_job(*, workspace_id: uuid.UUID) -> ScrapeJob:
    from app_shared.enums import ScrapeJobSource, ScrapeJobStatus, ScrapeJobType, ScrapeScope

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
    return job


def test_mark_target_deferred_rate_limited_no_timestamps_persists_code() -> None:
    session = FakeOrmSession()
    workspace_id = uuid.uuid4()
    job = _make_job(workspace_id=workspace_id)
    session.seed(job)
    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=uuid.uuid4(),
        status=ScrapeTargetStatus.STARTED,
        created_at=datetime.now(timezone.utc),
    )
    target.id = uuid.uuid4()
    session.seed(target)

    mark_target(
        session,
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=target.match_id,
        status=ScrapeTargetStatus.DEFERRED,
        error_code=ScrapeErrorCode.RATE_LIMITED,
    )

    assert target.status == ScrapeTargetStatus.DEFERRED
    assert target.error_code == ScrapeErrorCode.RATE_LIMITED
    # Non-terminal: no completed_at. Not a fresh STARTED transition
    # either (it was already STARTED before this call), so started_at
    # is untouched by this transition -- overflow-dispatch.md §2.
    assert target.completed_at is None
