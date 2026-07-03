"""`dispatch_job` task unit tests (SPEC-08 T030, US1, FR-011/012/013, SC-003).

`apps/workers/app/workers/tasks_jobs.py::dispatch_job` — fake session
(`FakeOrmSession`) + fake Redis + a stubbed HTTP transport wired through
the REAL, unchanged `ScrapydDispatchClient` (never a real DB/Redis/
Scrapyd). Per `contracts/dispatch-task.md`: `set_workspace_context` runs
before any query; the job transitions to `RUNNING` + `started_at` set
exactly once; one `schedule` call per planned batch, carrying the
selected node + `batch_index`; a duplicate delivery of the same
`(scrape_job_id, batch_index)` issues no second POST (the client's
`SET NX` guard neutralizes it).

Loaded in a fresh subprocess (mirrors `test_jobs_fork_safety.py` /
`test_engine_hygiene.py`'s `_CELERY_HOOK_WIRING_CHECK`), for the same
two reasons: (1) `apps/api` and `apps/workers` each ship their own
top-level ``app`` package, so importing `app.workers.tasks_jobs` in the
shared test process is ambiguous once another test module has already
imported `apps/api`'s `app` package; (2) `celery_app.py` calls
`get_settings()` at module scope, needing a clean, self-contained env.
"""

from __future__ import annotations

import os
import subprocess
import sys

_DISPATCH_TASK_CHECK = """
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

call_order = []


def fake_set_workspace_context(session, workspace_id):
    call_order.append("set_workspace_context")


tasks_jobs.set_workspace_context = fake_set_workspace_context

fake_session = FakeOrmSession()
_original_execute = FakeOrmSession.execute


def _tracking_execute(self, stmt):
    call_order.append("execute")
    return _original_execute(self, stmt)


FakeOrmSession.execute = _tracking_execute


@contextmanager
def fake_get_session():
    yield fake_session


tasks_jobs.get_session = fake_get_session

# --- fixture data: one job, two targets on two distinct domains --------

workspace_id = uuid.uuid4()
job_id = uuid.uuid4()
competitor_a_id = uuid.uuid4()
competitor_b_id = uuid.uuid4()
match_a_id = uuid.uuid4()
match_b_id = uuid.uuid4()
now = datetime.now(timezone.utc)

job = ScrapeJob(
    workspace_id=workspace_id,
    type=ScrapeJobType.MANUAL,
    scope=ScrapeScope.MATCH,
    status=ScrapeJobStatus.PENDING,
    total_targets=2,
    source=ScrapeJobSource.API,
    created_at=now,
)
job.id = job_id
fake_session.seed(job)

match_a = CompetitorProductMatch(
    workspace_id=workspace_id,
    product_id=uuid.uuid4(),
    product_variant_id=uuid.uuid4(),
    competitor_id=competitor_a_id,
    competitor_url="https://a.example.com/p",
    normalized_competitor_url="https://a.example.com/p",
    url_pattern="https://a.example.com/p",
    url_pattern_version=1,
    priority=MatchPriority.NORMAL,
    status=MatchStatus.ACTIVE,
)
match_a.id = match_a_id
match_b = CompetitorProductMatch(
    workspace_id=workspace_id,
    product_id=uuid.uuid4(),
    product_variant_id=uuid.uuid4(),
    competitor_id=competitor_b_id,
    competitor_url="https://b.example.com/p",
    normalized_competitor_url="https://b.example.com/p",
    url_pattern="https://b.example.com/p",
    url_pattern_version=1,
    priority=MatchPriority.NORMAL,
    status=MatchStatus.ACTIVE,
)
match_b.id = match_b_id
fake_session.seed(match_a, match_b)

competitor_a = Competitor(workspace_id=workspace_id, name="A", domain="a.example.com")
competitor_a.id = competitor_a_id
competitor_b = Competitor(workspace_id=workspace_id, name="B", domain="b.example.com")
competitor_b.id = competitor_b_id
fake_session.seed(competitor_a, competitor_b)

target_a = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=job_id,
    match_id=match_a_id,
    status=ScrapeTargetStatus.PENDING,
    created_at=now,
)
target_a.id = uuid.uuid4()
target_b = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=job_id,
    match_id=match_b_id,
    status=ScrapeTargetStatus.PENDING,
    created_at=now,
)
target_b.id = uuid.uuid4()
fake_session.seed(target_a, target_b)

# --- first dispatch ------------------------------------------------------

tasks_jobs.dispatch_job(str(job_id), str(workspace_id))

if not call_order or call_order[0] != "set_workspace_context":
    print("ORDER_WRONG:" + str(call_order[:3]))
    sys.exit(1)

if job.status != ScrapeJobStatus.RUNNING:
    print("STATUS_NOT_RUNNING:" + str(job.status))
    sys.exit(1)

if job.started_at is None:
    print("STARTED_AT_NOT_SET")
    sys.exit(1)

started_at_first = job.started_at

if len(calls) != 2:
    print("EXPECTED_TWO_SCHEDULE_CALLS_GOT:" + str(len(calls)))
    sys.exit(1)

urls = {call["url"] for call in calls}
if urls != {"http://scrapers:6800/schedule.json"}:
    print("UNEXPECTED_URLS:" + str(urls))
    sys.exit(1)

for call in calls:
    if call["data"]["workspace_id"] != str(workspace_id):
        print("WORKSPACE_ID_MISMATCH")
        sys.exit(1)
    if call["data"]["scrape_job_id"] != str(job_id):
        print("SCRAPE_JOB_ID_MISMATCH")
        sys.exit(1)
    if call["data"]["project"] != "price_monitor":
        print("PROJECT_MISMATCH")
        sys.exit(1)
    if call["data"]["spider"] != "generic_price_spider":
        print("SPIDER_MISMATCH")
        sys.exit(1)

dispatched_match_ids = set()
for call in calls:
    for match_id in call["data"]["match_ids"]:
        dispatched_match_ids.add(str(match_id))
if dispatched_match_ids != {str(match_a_id), str(match_b_id)}:
    print("MATCH_IDS_MISMATCH:" + str(dispatched_match_ids))
    sys.exit(1)

# --- duplicate delivery: no second POST, started_at unchanged -----------

tasks_jobs.dispatch_job(str(job_id), str(workspace_id))

if job.started_at != started_at_first:
    print("STARTED_AT_CHANGED_ON_DUPLICATE")
    sys.exit(1)

if len(calls) != 2:
    print("DUPLICATE_CAUSED_EXTRA_POST:" + str(len(calls)))
    sys.exit(1)

print("OK")
sys.exit(0)
"""

_DISPATCH_TASK_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
}


def test_dispatch_job_dispatches_batches_idempotently() -> None:
    env = {**os.environ, **_DISPATCH_TASK_ENV}
    result = subprocess.run(
        [sys.executable, "-c", _DISPATCH_TASK_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=None,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"
