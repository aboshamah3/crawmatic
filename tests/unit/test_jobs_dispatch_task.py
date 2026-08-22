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
    # The client serializes list-shaped match_ids to the spider's
    # comma-separated form (Scrapyd keeps only one repeated form field).
    if not isinstance(call["data"]["match_ids"], str):
        print("MATCH_IDS_NOT_SERIALIZED:" + repr(call["data"]["match_ids"]))
        sys.exit(1)
    for match_id in call["data"]["match_ids"].split(","):
        dispatched_match_ids.add(match_id)
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

_F2_FIXTURES = """
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
    return FakeResponse(200, {"status": "ok", "jobid": "job-" + str(len(calls))})


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

workspace_id = uuid.uuid4()
job_id = uuid.uuid4()
now = datetime.now(timezone.utc)
old_stamp = now - timedelta(minutes=5)

job = ScrapeJob(
    workspace_id=workspace_id,
    type=ScrapeJobType.MANUAL,
    scope=ScrapeScope.MATCH,
    status=ScrapeJobStatus.PENDING,
    total_targets=4,
    source=ScrapeJobSource.API,
    created_at=now,
)
job.id = job_id
fake_session.seed(job)


def _seed_target(label, status, dispatched_at):
    competitor_id = uuid.uuid4()
    match_id = uuid.uuid4()
    competitor = Competitor(
        workspace_id=workspace_id, name=label, domain=label + ".example.com"
    )
    competitor.id = competitor_id
    match = CompetitorProductMatch(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=competitor_id,
        competitor_url="https://" + label + ".example.com/p",
        normalized_competitor_url="https://" + label + ".example.com/p",
        url_pattern="https://" + label + ".example.com/p",
        url_pattern_version=1,
        priority=MatchPriority.NORMAL,
        status=MatchStatus.ACTIVE,
    )
    match.id = match_id
    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=job_id,
        match_id=match_id,
        status=status,
        created_at=now,
        dispatched_at=dispatched_at,
    )
    target.id = uuid.uuid4()
    fake_session.seed(competitor, match, target)
    return target


# t1: never dispatched PENDING -> must be POSTed.
t1 = _seed_target("t1", ScrapeTargetStatus.PENDING, None)
# t2: PENDING but already stamped -> scrapyd's (or the reaper's) problem.
t2 = _seed_target("t2", ScrapeTargetStatus.PENDING, old_stamp)
# t3: DEFERRED handback -> must be POSTed even though already stamped.
t3 = _seed_target("t3", ScrapeTargetStatus.DEFERRED, old_stamp)
# t4: terminal, never dispatched -> never selected, stays unstamped.
t4 = _seed_target("t4", ScrapeTargetStatus.COMPLETED, None)
"""

# The fixtures above plus one clean dispatch. Split out so the F-1
# durability check below can seed its own failure mode BEFORE the task
# runs, without duplicating the fixture block.
_F2_SETUP = _F2_FIXTURES + """
tasks_jobs.dispatch_job(str(job_id), str(workspace_id))

posted_match_ids = set()
for call in calls:
    for raw in str(call["data"]["match_ids"]).split(","):
        posted_match_ids.add(raw)
"""

_F2_SELECTION_CHECK = (
    _F2_SETUP
    + """
expected = {str(t1.match_id), str(t3.match_id)}
if posted_match_ids != expected:
    print("WRONG_SELECTION:" + str(sorted(posted_match_ids)))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
)

_F2_STAMP_CHECK = (
    _F2_SETUP
    + """
if t1.dispatched_at is None:
    print("T1_NOT_STAMPED")
    sys.exit(1)
if t3.dispatched_at is None or t3.dispatched_at == old_stamp:
    print("T3_NOT_RESTAMPED:" + str(t3.dispatched_at))
    sys.exit(1)
if t2.dispatched_at != old_stamp:
    print("T2_STAMP_MUTATED:" + str(t2.dispatched_at))
    sys.exit(1)
if t4.dispatched_at is not None:
    print("T4_STAMPED_WITHOUT_POST:" + str(t4.dispatched_at))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
)


# F-1 (2026-08-22 review): a stamp is only worth what it survives.
# `get_session()` never commits in its `finally`, so one commit after the
# whole batch loop meant batch 50's unreachable node rolled back the
# stamps of every batch already POSTed -- and once the 900s Redis guard
# TTL expired, the next dispatch delivery re-planned them all. That IS
# the 2.71x mechanism. t1 and t3 sit on distinct domains, so they plan
# into two batches; the second POST blows up.
_F1_STAMP_DURABILITY_CHECK = (
    _F2_FIXTURES
    + """
# On a fake session an in-memory attribute survives regardless, so only
# what was COMMITTED before the failure propagated proves anything.
committed = []
_real_commit = fake_session.commit


def recording_commit():
    committed.append(
        {t.match_id: t.dispatched_at for t in fake_session._rows.get(ScrapeJobTarget, [])}
    )
    _real_commit()


fake_session.commit = recording_commit

# `client_factory` reads `fake_post` out of module globals when the task
# builds its client, so rebinding it here is enough.
_ok_post = fake_post


def failing_second_post(url, *, data, auth, timeout):
    if calls:
        raise requests.ConnectionError("node refused the second batch")
    return _ok_post(url, data=data, auth=auth, timeout=timeout)


fake_post = failing_second_post

stale = {t1.match_id: t1.dispatched_at, t3.match_id: t3.dispatched_at}

raised = None
try:
    tasks_jobs.dispatch_job(str(job_id), str(workspace_id))
except Exception as exc:
    raised = exc

if raised is None:
    print("SECOND_BATCH_FAILURE_WAS_SWALLOWED")
    sys.exit(1)

if len(calls) != 1:
    print("EXPECTED_EXACTLY_ONE_SUCCESSFUL_POST_GOT:" + str(len(calls)))
    sys.exit(1)

posted = str(calls[0]["data"]["match_ids"]).split(",")
succeeded = t1 if str(t1.match_id) in posted else t3
failed = t3 if succeeded is t1 else t1

if not committed:
    print("NOTHING_WAS_COMMITTED_BEFORE_THE_FAILURE_PROPAGATED")
    sys.exit(1)

snapshot = committed[-1]
stamped = snapshot.get(succeeded.match_id)
if stamped is None or stamped == stale[succeeded.match_id]:
    print("POSTED_BATCH_STAMP_WAS_ROLLED_BACK_BY_A_LATER_FAILURE:" + str(stamped))
    sys.exit(1)
if snapshot.get(failed.match_id) != stale[failed.match_id]:
    print("UNPOSTED_BATCH_WAS_STAMPED:" + str(snapshot.get(failed.match_id)))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
)


_DISPATCH_TASK_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
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


def _run_f2(script: str) -> None:
    env = {**os.environ, **_DISPATCH_TASK_ENV}
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=None,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


def test_dispatch_job_skips_already_dispatched_pending_targets() -> None:
    """A PENDING target with `dispatched_at` set is already in scrapyd's
    queue — the 2026-08-21 mushtryati run re-POSTed the whole backlog on
    every guard expiry (11,830 attempts over 4,372 targets = 2.71x,
    ~$0.50 wasted). Selection must be
    `(PENDING AND dispatched_at IS NULL) OR DEFERRED`."""
    _run_f2(_F2_SELECTION_CHECK)


def test_dispatch_job_stamps_dispatched_at_on_post() -> None:
    """Every target carried in a POSTed batch gets stamped (a guard-deduped
    'already scheduled' return counts as dispatched too); a target the
    selection never picked up keeps `dispatched_at` untouched."""
    _run_f2(_F2_STAMP_CHECK)


def test_dispatch_job_commits_a_posted_batchs_stamp_before_a_later_failure() -> None:
    """Batch A is POSTed, batch B's POST raises. A's `dispatched_at` must
    already be COMMITTED when the failure propagates — one commit after
    the whole loop rolled it back, and past the 900s Redis guard TTL the
    next delivery re-POSTed A: the 2.71x mechanism, one layer down."""
    _run_f2(_F1_STAMP_DURABILITY_CHECK)
