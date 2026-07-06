"""`dispatch_job` project/spider selector unit tests (SPEC-14 T022, US2,
FR-015/016, `contracts/dispatch-routing.md`).

`apps/workers/app/workers/tasks_jobs.py::dispatch_job` already picks the
Scrapyd node pool by `batch.mode` (`SCRAPYD_BROWSER_URLS` vs
`SCRAPYD_HTTP_URLS`); this test isolates the *project + spider* half of
that selection — a BROWSER-mode batch must schedule
`(price_monitor_browser, generic_browser_price_spider)` on the browser
pool, an HTTP-mode batch `(price_monitor, generic_price_spider)` on the
HTTP pool, and an all-HTTP job must never touch the browser pool.

Mirrors `test_jobs_dispatch_task.py`'s fixture/mocking style (fake
Redis, a stubbed `requests.Session.post` wired through the REAL
`ScrapydDispatchClient`, `FakeOrmSession`) but monkeypatches
`tasks_jobs.plan_batches` to return a fixed, mixed-mode `Batch` list
directly — the routing fix under test lives entirely in what
`dispatch_job` does with each already-planned `Batch`, not in planning
itself (`plan_batches` is covered separately and left untouched here).

Loaded in a fresh subprocess for the same reasons as
`test_jobs_dispatch_task.py`: (1) `apps/api` and `apps/workers` each ship
their own top-level ``app`` package — importing `app.workers.tasks_jobs`
in the shared test process is ambiguous once another test module has
already imported `apps/api`'s `app` package; (2) `celery_app.py` calls
`get_settings()` at module scope, needing a clean, self-contained env.
"""

from __future__ import annotations

import os
import subprocess
import sys

_DISPATCH_ROUTING_CHECK = """
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

import requests

from _jobs_fake_session import FakeOrmSession
from app_shared.enums import (
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeProfileMode,
    ScrapeScope,
)
from app_shared.jobs.batching import Batch
from app_shared.models.jobs import ScrapeJob
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
tasks_jobs.set_workspace_context = lambda session, workspace_id: None

fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


tasks_jobs.get_session = fake_get_session

# No real target/match/competitor rows are seeded -- `plan_batches` is
# monkeypatched below to return a fixed, mixed-mode batch list directly,
# so `_resolve_domains_and_modes` (which runs first, over zero seeded
# `ScrapeJobTarget` rows) simply resolves to an empty list, which the
# faked `plan_batches` then ignores.

match_http_id = uuid.uuid4()
match_browser_id = uuid.uuid4()

MIXED_BATCHES = [
    Batch(batch_index=0, mode=ScrapeProfileMode.HTTP, domain="a.example.com", match_ids=[match_http_id]),
    Batch(batch_index=1, mode=ScrapeProfileMode.BROWSER, domain="b.example.com", match_ids=[match_browser_id]),
]

ALL_HTTP_BATCHES = [
    Batch(batch_index=0, mode=ScrapeProfileMode.HTTP, domain="c.example.com", match_ids=[uuid.uuid4()]),
]

now = datetime.now(timezone.utc)


def make_job():
    job = ScrapeJob(
        workspace_id=uuid.uuid4(),
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.MATCH,
        status=ScrapeJobStatus.PENDING,
        total_targets=1,
        source=ScrapeJobSource.API,
        created_at=now,
    )
    job.id = uuid.uuid4()
    return job


# --- scenario 1: mixed HTTP/BROWSER job ---------------------------------

mixed_job = make_job()
fake_session.seed(mixed_job)

tasks_jobs.plan_batches = lambda *args, **kwargs: MIXED_BATCHES
tasks_jobs.dispatch_job(str(mixed_job.id), str(mixed_job.workspace_id))

if len(calls) != 2:
    print("MIXED_EXPECTED_TWO_CALLS_GOT:" + str(len(calls)))
    sys.exit(1)

by_domain_order = calls  # batch_index 0 (HTTP) then 1 (BROWSER), planned in order

http_call = calls[0]
browser_call = calls[1]

if http_call["url"] != "http://scrapers:6800/schedule.json":
    print("HTTP_URL_MISMATCH:" + str(http_call["url"]))
    sys.exit(1)
if http_call["data"]["project"] != "price_monitor":
    print("HTTP_PROJECT_MISMATCH:" + str(http_call["data"]["project"]))
    sys.exit(1)
if http_call["data"]["spider"] != "generic_price_spider":
    print("HTTP_SPIDER_MISMATCH:" + str(http_call["data"]["spider"]))
    sys.exit(1)

if browser_call["url"] != "http://scrapers-browser:6800/schedule.json":
    print("BROWSER_URL_MISMATCH:" + str(browser_call["url"]))
    sys.exit(1)
if browser_call["data"]["project"] != "price_monitor_browser":
    print("BROWSER_PROJECT_MISMATCH:" + str(browser_call["data"]["project"]))
    sys.exit(1)
if browser_call["data"]["spider"] != "generic_browser_price_spider":
    print("BROWSER_SPIDER_MISMATCH:" + str(browser_call["data"]["spider"]))
    sys.exit(1)

if str(match_http_id) not in http_call["data"]["match_ids"] and str(match_http_id) not in [str(m) for m in http_call["data"]["match_ids"]]:
    print("HTTP_MATCH_ID_MISSING")
    sys.exit(1)
if str(match_browser_id) not in [str(m) for m in browser_call["data"]["match_ids"]]:
    print("BROWSER_MATCH_ID_MISSING")
    sys.exit(1)

# --- scenario 2: all-HTTP job never touches the browser pool ------------

calls.clear()
all_http_job = make_job()
fake_session.seed(all_http_job)

tasks_jobs.plan_batches = lambda *args, **kwargs: ALL_HTTP_BATCHES
tasks_jobs.dispatch_job(str(all_http_job.id), str(all_http_job.workspace_id))

if len(calls) != 1:
    print("ALL_HTTP_EXPECTED_ONE_CALL_GOT:" + str(len(calls)))
    sys.exit(1)

browser_urls = [call for call in calls if call["url"] == "http://scrapers-browser:6800/schedule.json"]
if browser_urls:
    print("ALL_HTTP_JOB_REACHED_BROWSER_POOL")
    sys.exit(1)

if calls[0]["data"]["project"] != "price_monitor" or calls[0]["data"]["spider"] != "generic_price_spider":
    print("ALL_HTTP_PROJECT_SPIDER_MISMATCH")
    sys.exit(1)

print("OK")
sys.exit(0)
"""

_DISPATCH_ROUTING_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def test_dispatch_job_routes_project_and_spider_by_batch_mode() -> None:
    env = {**os.environ, **_DISPATCH_ROUTING_ENV}
    result = subprocess.run(
        [sys.executable, "-c", _DISPATCH_ROUTING_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=None,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"
