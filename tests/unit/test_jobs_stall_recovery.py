"""`recover_stalled_batches` task unit tests (SPEC-08 T043, US3, FR-015, SC-005).

`apps/workers/app/workers/tasks_jobs.py::recover_stalled_batches` — fake
session (`FakeOrmSession`) + fake Redis + a stubbed HTTP transport wired
through the REAL, unchanged `ScrapydDispatchClient` (never a real
DB/Redis/Scrapyd), plus a monkeypatched `datetime` so the stall-window
bucket can be advanced deterministically without sleeping. Per
`contracts/stall-recovery.md`: a target still bare PENDING past
`SCRAPE_STALL_TIMEOUT_SECONDS` (measured from the job's `started_at`) is
re-dispatched; STARTED/terminal or `locked_at`-live targets, and jobs not
yet past the timeout, are excluded; within one stall window a duplicate
recovery delivery produces no second POST (the client's `SET NX` guard);
crossing into a fresh window mints a new suffixed key and permits a
genuine re-dispatch; the same domain always maps to the same node.

Loaded in a fresh subprocess (mirrors `test_jobs_dispatch_task.py`) for
the same two reasons: `apps/api`/`apps/workers` each ship a top-level
`app` package (ambiguous once another test module has imported one), and
`celery_app.py` calls `get_settings()` at module scope.
"""

from __future__ import annotations

import os
import subprocess
import sys

_STALL_RECOVERY_CHECK = """
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

import requests

from _jobs_fake_session import FakeOrmSession
from app_shared.enums import (
    MatchPriority,
    MatchStatus,
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
    calls.append({"url": url, "data": dict(data)})
    jobid = "job-" + str(len(calls))
    return FakeResponse(200, {"status": "ok", "jobid": jobid})


fake_redis = FakeRedis()


def client_factory(*, settings=None):
    http_session = requests.Session()
    http_session.post = fake_post
    return RealClient(settings=settings, redis_client=fake_redis, session=http_session)


tasks_jobs.ScrapydDispatchClient = client_factory

fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


tasks_jobs.get_session = fake_get_session
tasks_jobs.set_workspace_context = lambda session, workspace_id: None

# A controllable clock: `recover_stalled_batches` reads `datetime.now(tz)`
# to both age-check targets against `job.started_at` and to derive the
# stall-window bucket -- advancing `_FakeDatetime._now` simulates the
# passage of one stall window without a real sleep.
TIMEOUT = 900

base_now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class _FakeDatetime(datetime):
    _now = base_now

    @classmethod
    def now(cls, tz=None):
        return cls._now


tasks_jobs.datetime = _FakeDatetime

# --- fixture data --------------------------------------------------------

workspace_id = uuid.uuid4()

# A job well past the stall timeout (started_at far before base_now).
stalled_job_id = uuid.uuid4()
stalled_job = ScrapeJob(
    workspace_id=workspace_id,
    type=ScrapeJobType.MANUAL,
    scope=ScrapeScope.MATCH,
    status=ScrapeJobStatus.RUNNING,
    total_targets=4,
    source=ScrapeJobSource.API,
    created_at=base_now - timedelta(seconds=TIMEOUT * 3),
    started_at=base_now - timedelta(seconds=TIMEOUT * 2),
)
stalled_job.id = stalled_job_id
fake_session.seed(stalled_job)

# A job started recently -- NOT past the timeout, must be left alone.
fresh_job_id = uuid.uuid4()
fresh_job = ScrapeJob(
    workspace_id=workspace_id,
    type=ScrapeJobType.MANUAL,
    scope=ScrapeScope.MATCH,
    status=ScrapeJobStatus.RUNNING,
    total_targets=1,
    source=ScrapeJobSource.API,
    created_at=base_now,
    started_at=base_now - timedelta(seconds=10),
)
fresh_job.id = fresh_job_id
fake_session.seed(fresh_job)

competitor_id = uuid.uuid4()
competitor = Competitor(workspace_id=workspace_id, name="Shop", domain="shop.example.com")
competitor.id = competitor_id
fake_session.seed(competitor)


def _match():
    match = CompetitorProductMatch(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=competitor_id,
        competitor_url="https://shop.example.com/p",
        normalized_competitor_url="https://shop.example.com/p",
        url_pattern="https://shop.example.com/p",
        url_pattern_version=1,
        priority=MatchPriority.NORMAL,
        status=MatchStatus.ACTIVE,
    )
    match.id = uuid.uuid4()
    return match


match_stale = _match()
match_started = _match()
match_locked = _match()
match_completed = _match()
match_fresh = _match()
fake_session.seed(match_stale, match_started, match_locked, match_completed, match_fresh)

# Still bare PENDING, never locked -- eligible for recovery.
target_stale = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=stalled_job_id,
    match_id=match_stale.id,
    status=ScrapeTargetStatus.PENDING,
    created_at=base_now,
)
target_stale.id = uuid.uuid4()

# Progressed to STARTED -- excluded even though the job is stalled.
target_started = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=stalled_job_id,
    match_id=match_started.id,
    status=ScrapeTargetStatus.STARTED,
    started_at=base_now - timedelta(seconds=TIMEOUT),
    created_at=base_now,
)
target_started.id = uuid.uuid4()

# Still PENDING but `locked_at`-live -- excluded (in-flight lock, SPEC-11).
target_locked = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=stalled_job_id,
    match_id=match_locked.id,
    status=ScrapeTargetStatus.PENDING,
    locked_at=base_now,
    created_at=base_now,
)
target_locked.id = uuid.uuid4()

# Already terminal -- excluded.
target_completed = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=stalled_job_id,
    match_id=match_completed.id,
    status=ScrapeTargetStatus.COMPLETED,
    completed_at=base_now,
    created_at=base_now,
)
target_completed.id = uuid.uuid4()

fake_session.seed(target_stale, target_started, target_locked, target_completed)

# The fresh (not-yet-stalled) job's lone target -- must never be touched.
target_fresh = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=fresh_job_id,
    match_id=match_fresh.id,
    status=ScrapeTargetStatus.PENDING,
    created_at=base_now,
)
target_fresh.id = uuid.uuid4()
fake_session.seed(target_fresh)

# --- first recovery pass ---------------------------------------------------

tasks_jobs.recover_stalled_batches()

if len(calls) != 1:
    print("EXPECTED_ONE_POST_GOT:" + str(len(calls)))
    sys.exit(1)

first_call = calls[0]
if first_call["data"]["match_ids"] != [match_stale.id]:
    print("WRONG_MATCH_IDS_DISPATCHED:" + str(first_call["data"]["match_ids"]))
    sys.exit(1)

first_node_url = first_call["url"]

# --- duplicate delivery within the SAME stall window: no second POST ------

tasks_jobs.recover_stalled_batches()

if len(calls) != 1:
    print("DUPLICATE_WITHIN_WINDOW_CAUSED_EXTRA_POST:" + str(len(calls)))
    sys.exit(1)

# --- a fresh stall window: a genuine re-dispatch is permitted -------------

_FakeDatetime._now = base_now + timedelta(seconds=TIMEOUT)
# Keep the still-fresh job's `started_at` pinned relative to the advanced
# clock -- otherwise simply teleporting "now" forward would spuriously
# stall it too, which isn't what this section is testing.
fresh_job.started_at = _FakeDatetime._now - timedelta(seconds=10)

tasks_jobs.recover_stalled_batches()

if len(calls) != 2:
    print("EXPECTED_SECOND_POST_IN_NEW_WINDOW_GOT:" + str(len(calls)))
    sys.exit(1)

second_call = calls[1]
if second_call["data"]["match_ids"] != [match_stale.id]:
    print("WRONG_MATCH_IDS_ON_SECOND_DISPATCH:" + str(second_call["data"]["match_ids"]))
    sys.exit(1)

# Same domain -> same node, even across the two separate re-dispatches.
if second_call["url"] != first_node_url:
    print("NODE_CHANGED_ACROSS_REDISPATCH:" + str((first_node_url, second_call["url"])))
    sys.exit(1)

print("OK")
sys.exit(0)
"""

_STALL_RECOVERY_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scraper-a:6800,http://scraper-b:6800,http://scraper-c:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "SCRAPE_STALL_TIMEOUT_SECONDS": "900",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def test_recover_stalled_batches_redispatches_idempotently() -> None:
    env = {**os.environ, **_STALL_RECOVERY_ENV}
    result = subprocess.run(
        [sys.executable, "-c", _STALL_RECOVERY_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=None,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"
