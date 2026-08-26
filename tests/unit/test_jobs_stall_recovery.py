"""`recover_stalled_batches` task unit tests (SPEC-08 T043, US3, FR-015, SC-005).

`apps/workers/app/workers/tasks_jobs.py::recover_stalled_batches` — fake
session (`FakeOrmSession`) + fake Redis + a stubbed HTTP transport wired
through the REAL, unchanged `ScrapydDispatchClient` (never a real
DB/Redis/Scrapyd), plus a monkeypatched `datetime` so the stall-window
bucket can be advanced deterministically without sleeping. Per
`contracts/stall-recovery.md`: a target still bare PENDING past
`SCRAPE_STALL_TIMEOUT_SECONDS` (measured from its OWN `dispatched_at` —
F-2, 2026-08-22; job-age classified every rate-limited tail target
"stalled" from minute 15 of the 2026-08-21 run) whose node is not alive
and working its queue is re-dispatched; STARTED/terminal or
`locked_at`-live targets, never-dispatched targets, targets dispatched
inside the timeout, and batches on a live node are excluded; within one
stall window a duplicate
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


def fake_get(url, *, auth=None, timeout=None):
    # F-2: every node this suite reaps is DEAD -- an unreachable node is
    # the one case the reaper exists for, so re-dispatch must proceed.
    raise requests.ConnectionError("node down")


fake_redis = FakeRedis()


def client_factory(*, settings=None, intents=None):
    http_session = requests.Session()
    http_session.post = fake_post
    http_session.get = fake_get
    return RealClient(settings=settings, redis_client=fake_redis, session=http_session)


tasks_jobs.ScrapydDispatchClient = client_factory
# EPA C3: the paid-dispatch sites authorize before they POST. That is a
# real DB transaction (budget locks + breaker/entitlement/domain evidence),
# so these DB-free tests stub it; the gate's own behaviour is proven in
# tests/integration/test_cost_authorization.py. `stub_cost_authorization`
# FAILS if the gate is missing, so this cannot hide a deleted gate.
from _costauth_test_stub import stub_cost_authorization
stub_cost_authorization(tasks_jobs)

fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


tasks_jobs.get_session = fake_get_session
# The cross-tenant `_scan_job_refs` sweep runs on the BYPASSRLS system
# session (mushtryati F-1) -- point it at the same fake, so the scan
# still reads the seeded rows and no real engine is ever constructed.
tasks_jobs.get_system_session = fake_get_session
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

# Still bare PENDING, never locked, and its OWN dispatch is long past the
# timeout -- eligible for recovery (F-2: aged per target, not per job).
target_stale = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=stalled_job_id,
    match_id=match_stale.id,
    status=ScrapeTargetStatus.PENDING,
    created_at=base_now,
    dispatched_at=base_now - timedelta(seconds=TIMEOUT * 2),
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
    dispatched_at=base_now - timedelta(seconds=TIMEOUT * 2),
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
    dispatched_at=base_now - timedelta(seconds=TIMEOUT * 2),
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
    dispatched_at=base_now - timedelta(seconds=TIMEOUT * 2),
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
    dispatched_at=base_now - timedelta(seconds=10),
)
target_fresh.id = uuid.uuid4()
fake_session.seed(target_fresh)

# --- first recovery pass ---------------------------------------------------

tasks_jobs.recover_stalled_batches()

if len(calls) != 1:
    print("EXPECTED_ONE_POST_GOT:" + str(len(calls)))
    sys.exit(1)

first_call = calls[0]
if first_call["data"]["match_ids"] != str(match_stale.id):
    print("WRONG_MATCH_IDS_DISPATCHED:" + str(first_call["data"]["match_ids"]))
    sys.exit(1)

first_node_url = first_call["url"]

# --- duplicate delivery within the SAME stall window: no second POST ------

tasks_jobs.recover_stalled_batches()

if len(calls) != 1:
    print("DUPLICATE_WITHIN_WINDOW_CAUSED_EXTRA_POST:" + str(len(calls)))
    sys.exit(1)

# --- a fresh stall window: a genuine re-dispatch is permitted -------------

# Two windows on, so the re-dispatched target's OWN refreshed
# `dispatched_at` (stamped by the first pass) is itself now past the
# timeout -- a target re-POSTed to a node that stayed dead is stalled
# again, and a fresh window key permits the genuine retry.
_FakeDatetime._now = base_now + timedelta(seconds=TIMEOUT * 2)
# Keep the still-fresh job's `started_at` -- and, since F-2 ages per
# TARGET, its target's own `dispatched_at` -- pinned relative to the
# advanced clock; otherwise simply teleporting "now" forward would
# spuriously stall it too, which isn't what this section is testing.
fresh_job.started_at = _FakeDatetime._now - timedelta(seconds=10)
target_fresh.dispatched_at = _FakeDatetime._now - timedelta(seconds=10)

tasks_jobs.recover_stalled_batches()

if len(calls) != 2:
    print("EXPECTED_SECOND_POST_IN_NEW_WINDOW_GOT:" + str(len(calls)))
    sys.exit(1)

second_call = calls[1]
if second_call["data"]["match_ids"] != str(match_stale.id):
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


# --- F-2: per-target aging + node-liveness probe ---------------------------
#
# Shared prelude for the three reaper regression checks below. Same
# subprocess idiom and same fakes as `_STALL_RECOVERY_CHECK` above, but
# the fixture rows are minted by helpers so each check can seed exactly
# the target shape it is about, and the fake `daemonstatus.json` payload
# is switchable per check (`daemon["payload"] = None` -> node dead).
_REAPER_PRELUDE = """
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
probes = []
# The `daemonstatus.json` payload every probed node returns. `None` means
# the node is unreachable -- the reaper's "the node died" case.
daemon = {"payload": None}


def fake_post(url, *, data, auth, timeout):
    calls.append({"url": url, "data": dict(data)})
    return FakeResponse(200, {"status": "ok", "jobid": "job-" + str(len(calls))})


def fake_get(url, *, auth=None, timeout=None):
    probes.append(url)
    if daemon["payload"] is None:
        raise requests.ConnectionError("node down")
    return FakeResponse(200, daemon["payload"])


fake_redis = FakeRedis()


def client_factory(*, settings=None, intents=None):
    http_session = requests.Session()
    http_session.post = fake_post
    http_session.get = fake_get
    return RealClient(settings=settings, redis_client=fake_redis, session=http_session)


tasks_jobs.ScrapydDispatchClient = client_factory
# EPA C3: the paid-dispatch sites authorize before they POST. That is a
# real DB transaction (budget locks + breaker/entitlement/domain evidence),
# so these DB-free tests stub it; the gate's own behaviour is proven in
# tests/integration/test_cost_authorization.py. `stub_cost_authorization`
# FAILS if the gate is missing, so this cannot hide a deleted gate.
from _costauth_test_stub import stub_cost_authorization
stub_cost_authorization(tasks_jobs)

fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


tasks_jobs.get_session = fake_get_session
tasks_jobs.get_system_session = fake_get_session
tasks_jobs.set_workspace_context = lambda session, workspace_id: None

TIMEOUT = 900
base_now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class _FakeDatetime(datetime):
    _now = base_now

    @classmethod
    def now(cls, tz=None):
        return cls._now


tasks_jobs.datetime = _FakeDatetime

workspace_id = uuid.uuid4()

competitor_id = uuid.uuid4()
competitor = Competitor(workspace_id=workspace_id, name="Shop", domain="shop.example.com")
competitor.id = competitor_id
fake_session.seed(competitor)


def new_job(started_seconds_ago):
    job = ScrapeJob(
        workspace_id=workspace_id,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.MATCH,
        status=ScrapeJobStatus.RUNNING,
        total_targets=0,
        source=ScrapeJobSource.API,
        created_at=base_now - timedelta(seconds=started_seconds_ago),
        started_at=base_now - timedelta(seconds=started_seconds_ago),
    )
    job.id = uuid.uuid4()
    fake_session.seed(job)
    return job


def new_match():
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
    fake_session.seed(match)
    return match


def new_target(job, dispatched_seconds_ago):
    match = new_match()
    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=match.id,
        status=ScrapeTargetStatus.PENDING,
        created_at=base_now,
        dispatched_at=(
            None
            if dispatched_seconds_ago is None
            else base_now - timedelta(seconds=dispatched_seconds_ago)
        ),
    )
    target.id = uuid.uuid4()
    fake_session.seed(target)
    return target


def fail(message):
    print(message)
    sys.exit(1)
"""

# Job started 2h ago; t1 dispatched 20s ago, t2 dispatched 2h ago, t3
# never dispatched. Only t2 is stall-eligible -- job-age eligibility
# classified every rate-limited tail target "stalled" from minute 15 of
# the 2026-08-21 run (the feedback loop behind the 2.71x duplicate
# attempts).
_AGES_PER_TARGET_CHECK = _REAPER_PRELUDE + """
job = new_job(7200)
t1 = new_target(job, 20)
t2 = new_target(job, 7200)
t3 = new_target(job, None)

tasks_jobs.recover_stalled_batches()

if len(calls) != 1:
    fail("EXPECTED_EXACTLY_ONE_STALLED_TARGET_REDISPATCHED_GOT:" + str(len(calls)))

dispatched = calls[0]["data"]["match_ids"].split(",")
if dispatched != [str(t2.match_id)]:
    fail("WRONG_TARGETS_AGED_AS_STALLED:" + str(dispatched))

print("OK")
sys.exit(0)
"""

# A node answering `daemonstatus.json` with a non-empty queue is alive and
# working: its targets are queued behind max_proc/rate limits, not stalled.
_ALIVE_NODE_CHECK = _REAPER_PRELUDE + """
daemon["payload"] = {"status": "ok", "pending": 3, "running": 8, "finished": 41}

job = new_job(7200)
target = new_target(job, 7200)

tasks_jobs.recover_stalled_batches()

if not probes:
    fail("NODE_WAS_NEVER_PROBED_BEFORE_REDISPATCH")

if calls:
    fail("ALIVE_AND_WORKING_NODE_WAS_RE_POSTED:" + str(len(calls)))

print("OK")
sys.exit(0)
"""

# A node that cannot be reached at all is the scenario the reaper exists
# for: re-POST proceeds, and the re-POSTed targets get a fresh
# `dispatched_at` so the very next sweep does not reap them again.
_DEAD_NODE_CHECK = _REAPER_PRELUDE + """
daemon["payload"] = None

job = new_job(7200)
target = new_target(job, 7200)
stale_stamp = target.dispatched_at

tasks_jobs.recover_stalled_batches()

if len(calls) != 1:
    fail("UNREACHABLE_NODE_WAS_NOT_REAPED_GOT:" + str(len(calls)))

if calls[0]["data"]["match_ids"] != str(target.match_id):
    fail("WRONG_TARGET_REAPED:" + str(calls[0]["data"]["match_ids"]))

if target.dispatched_at == stale_stamp:
    fail("REDISPATCHED_TARGET_KEPT_ITS_STALE_DISPATCHED_AT")

if target.dispatched_at != _FakeDatetime._now:
    fail("REDISPATCHED_TARGET_NOT_STAMPED_NOW:" + str(target.dispatched_at))

# ... and the immediately-following sweep must therefore find nothing.
tasks_jobs.recover_stalled_batches()

if len(calls) != 1:
    fail("FRESH_STAMP_DID_NOT_PREVENT_IMMEDIATE_RE_REAP:" + str(len(calls)))

print("OK")
sys.exit(0)
"""


# The scrapers container restarted: scrapyd's queue is ephemeral, so the
# node is ALIVE and answering but holds nothing (`pending=0, running=0`).
# Queue cleared is not queue busy -- the target really is stranded and the
# reaper is the ONLY path left to it (`dispatch_job` and
# `redispatch_pending_jobs` both require `dispatched_at IS NULL`). A
# one-character slip in the gate (`>` -> `>=`, or `is not None` ->
# truthiness) would strand it permanently with every other test green.
_RESTARTED_NODE_CHECK = _REAPER_PRELUDE + """
daemon["payload"] = {"status": "ok", "pending": 0, "running": 0, "finished": 12}

job = new_job(7200)
target = new_target(job, 7200)
stale_stamp = target.dispatched_at

tasks_jobs.recover_stalled_batches()

if not probes:
    fail("NODE_WAS_NEVER_PROBED_BEFORE_REDISPATCH")

if len(calls) != 1:
    fail("EMPTY_QUEUE_ALIVE_NODE_WAS_NOT_REAPED_GOT:" + str(len(calls)))

if calls[0]["data"]["match_ids"] != str(target.match_id):
    fail("WRONG_TARGET_REAPED:" + str(calls[0]["data"]["match_ids"]))

if target.dispatched_at != _FakeDatetime._now or target.dispatched_at == stale_stamp:
    fail("RESTART_REDISPATCH_NOT_RESTAMPED:" + str(target.dispatched_at))

print("OK")
sys.exit(0)
"""

# A node whose `daemonstatus.json` carries unusable counts is not evidence
# that it is working a queue. `daemon_status` never raises; coercing its
# payload with a bare `int(...)` reintroduced the raise one call up, and it
# would abort the whole sweep (losing every commit) on one bad node.
_MALFORMED_PAYLOAD_CHECK = _REAPER_PRELUDE + """
daemon["payload"] = {"status": "ok", "pending": None, "running": ["nope"]}

job = new_job(7200)
target = new_target(job, 7200)

try:
    tasks_jobs.recover_stalled_batches()
except Exception as exc:
    fail("MALFORMED_PAYLOAD_RAISED_OUT_OF_THE_SWEEP:" + repr(exc))

if len(calls) != 1:
    fail("MALFORMED_PAYLOAD_NODE_NOT_TREATED_AS_DEAD_GOT:" + str(len(calls)))

if target.dispatched_at != _FakeDatetime._now:
    fail("REAPED_TARGET_NOT_RESTAMPED:" + str(target.dispatched_at))

print("OK")
sys.exit(0)
"""

# F-1 (2026-08-22 review): batch A's node is dead (reap proceeds, POST
# succeeds); batch B's POST then blows up. A's stamp must be COMMITTED
# before the failure propagates -- one commit after the whole sweep rolled
# it back, and past the 900s guard TTL A got re-POSTed all over again.
_REAPER_STAMP_DURABILITY_CHECK = _REAPER_PRELUDE + """
daemon["payload"] = None

# Snapshot every target's `dispatched_at` at each commit: on a fake
# session an in-memory attribute survives regardless, so only what was
# COMMITTED before the failure propagated proves anything.
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


def new_target_on(job, domain, dispatched_seconds_ago):
    competitor = Competitor(workspace_id=workspace_id, name=domain, domain=domain)
    competitor.id = uuid.uuid4()
    match = CompetitorProductMatch(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=competitor.id,
        competitor_url="https://" + domain + "/p",
        normalized_competitor_url="https://" + domain + "/p",
        url_pattern="https://" + domain + "/p",
        url_pattern_version=1,
        priority=MatchPriority.NORMAL,
        status=MatchStatus.ACTIVE,
    )
    match.id = uuid.uuid4()
    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=job.id,
        match_id=match.id,
        status=ScrapeTargetStatus.PENDING,
        created_at=base_now,
        dispatched_at=base_now - timedelta(seconds=dispatched_seconds_ago),
    )
    target.id = uuid.uuid4()
    fake_session.seed(competitor, match, target)
    return target


job = new_job(7200)
t_a = new_target_on(job, "alpha.example.com", 7200)
t_b = new_target_on(job, "beta.example.com", 7200)
stale = {t_a.match_id: t_a.dispatched_at, t_b.match_id: t_b.dispatched_at}

raised = None
try:
    tasks_jobs.recover_stalled_batches()
except Exception as exc:
    raised = exc

if raised is None:
    fail("SECOND_BATCH_FAILURE_WAS_SWALLOWED")

if len(calls) != 1:
    fail("EXPECTED_EXACTLY_ONE_SUCCESSFUL_POST_GOT:" + str(len(calls)))

posted = calls[0]["data"]["match_ids"].split(",")
succeeded = t_a if str(t_a.match_id) in posted else t_b
failed = t_b if succeeded is t_a else t_a

if not committed:
    fail("NOTHING_WAS_COMMITTED_BEFORE_THE_FAILURE_PROPAGATED")

snapshot = committed[-1]
if snapshot.get(succeeded.match_id) != _FakeDatetime._now:
    fail(
        "POSTED_BATCH_STAMP_WAS_ROLLED_BACK_BY_A_LATER_FAILURE:"
        + str(snapshot.get(succeeded.match_id))
    )
if snapshot.get(failed.match_id) != stale[failed.match_id]:
    fail("UNPOSTED_BATCH_WAS_STAMPED:" + str(snapshot.get(failed.match_id)))

print("OK")
sys.exit(0)
"""


def _run_reaper_check(script: str) -> None:
    env = {**os.environ, **_STALL_RECOVERY_ENV}
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


def test_reaper_ages_per_target_not_per_job() -> None:
    """Job started 2h ago; t1 dispatched 20s ago, t2 dispatched 2h ago,
    t3 never dispatched. Only t2 is stall-eligible: job-age eligibility
    classified every rate-limited tail target 'stalled' from minute 15
    of the 2026-08-21 run (the feedback loop behind the 2.71x)."""
    _run_reaper_check(_AGES_PER_TARGET_CHECK)


def test_reaper_skips_batches_whose_node_is_alive_and_working() -> None:
    """A node reporting pending+running > 0 is working its queue — its
    targets are queued, not stalled, so nothing is re-POSTed."""
    _run_reaper_check(_ALIVE_NODE_CHECK)


def test_reaper_reaps_when_node_unreachable() -> None:
    """An unreachable node IS the stall the reaper exists for: re-POST
    proceeds, and the re-POSTed targets get a fresh `dispatched_at` so
    the next sweep cannot immediately reap them again."""
    _run_reaper_check(_DEAD_NODE_CHECK)


def test_reaper_reaps_when_node_is_alive_but_its_queue_is_empty() -> None:
    """The scrapers-restart strand: scrapyd's queue is ephemeral, so a
    restarted node answers `pending=0, running=0` while the target it
    was holding is gone. Queue cleared is not queue busy — and the
    reaper is the only path left (dispatch and redispatch both require
    `dispatched_at IS NULL`), so it must re-POST and re-stamp."""
    _run_reaper_check(_RESTARTED_NODE_CHECK)


def test_reaper_treats_a_malformed_daemonstatus_payload_as_a_dead_node() -> None:
    """`daemon_status` never raises; a bare `int(payload["pending"])` put
    the raise back one call up, where it would abort the whole sweep.
    Unusable counts are not evidence of a working queue — reap."""
    _run_reaper_check(_MALFORMED_PAYLOAD_CHECK)


def test_reaper_commits_a_posted_batchs_stamp_before_a_later_failure() -> None:
    """Batch A is re-POSTed, batch B's POST raises. A's `dispatched_at`
    must already be COMMITTED when the failure propagates: with one
    commit after the whole sweep it was rolled back, and past the 900s
    Redis guard TTL A got re-POSTed all over again — the 2.71x
    mechanism this phase exists to remove."""
    _run_reaper_check(_REAPER_STAMP_DURABILITY_CHECK)


# --- EPA W4 gate review (2026-08-26): the UNSUPPORTED skip on the RETRY ---
# path, not just the first dispatch ----------------------------------------
#
# `tests/unit/test_jobs_dispatch_task.py` pins the same skip for
# `dispatch_job`. `recover_stalled_batches` is the path that matters more
# for this rule: a stall recovery IS a retry, so a domain certified
# UNSUPPORTED *after* its targets were first dispatched must be marked
# SKIPPED/BLOCKED rather than re-POSTed -- otherwise "zero retries for an
# unsupported domain" would hold only for domains certified before their
# first dispatch. Same subprocess idiom and same fakes as the checks
# above; the sibling target on an unremarkable (UNKNOWN) domain proves
# the skip is per-domain, not sweep-wide.
_UNSUPPORTED_STALL_CHECK = """
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

import requests

from _jobs_fake_session import FakeOrmSession
from app_shared.enums import (
    AccessMethod,
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
from app_shared.models.domain_playbooks import DomainPlaybook, DomainState
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
    calls.append({"url": url, "data": dict(data)})
    return FakeResponse(200, {"status": "ok", "jobid": "job-" + str(len(calls))})


def fake_get(url, *, auth=None, timeout=None):
    # Every node in this check is DEAD -- the one case the reaper exists
    # for, so a NON-skipped batch definitely would be re-POSTed. That is
    # what makes "no POST for the UNSUPPORTED batch" meaningful.
    raise requests.ConnectionError("node down")


fake_redis = FakeRedis()


def client_factory(*, settings=None, intents=None):
    http_session = requests.Session()
    http_session.post = fake_post
    http_session.get = fake_get
    return RealClient(settings=settings, redis_client=fake_redis, session=http_session)


tasks_jobs.ScrapydDispatchClient = client_factory
from _costauth_test_stub import stub_cost_authorization
stub_cost_authorization(tasks_jobs)

fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


tasks_jobs.get_session = fake_get_session
tasks_jobs.get_system_session = fake_get_session
tasks_jobs.set_workspace_context = lambda session, workspace_id: None

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
job_id = uuid.uuid4()

job = ScrapeJob(
    workspace_id=workspace_id,
    type=ScrapeJobType.MANUAL,
    scope=ScrapeScope.MATCH,
    status=ScrapeJobStatus.RUNNING,
    total_targets=2,
    source=ScrapeJobSource.API,
    created_at=base_now - timedelta(seconds=TIMEOUT * 3),
    started_at=base_now - timedelta(seconds=TIMEOUT * 2),
)
job.id = job_id
fake_session.seed(job)

competitor_ok_id = uuid.uuid4()
competitor_ok = Competitor(workspace_id=workspace_id, name="OK", domain="ok.example.com")
competitor_ok.id = competitor_ok_id
competitor_blocked_id = uuid.uuid4()
competitor_blocked = Competitor(
    workspace_id=workspace_id, name="Blocked", domain="blocked.example.com"
)
competitor_blocked.id = competitor_blocked_id
fake_session.seed(competitor_ok, competitor_blocked)


def _match(competitor_id, domain):
    match = CompetitorProductMatch(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=competitor_id,
        competitor_url="https://" + domain + "/p",
        normalized_competitor_url="https://" + domain + "/p",
        url_pattern="https://" + domain + "/p",
        url_pattern_version=1,
        priority=MatchPriority.NORMAL,
        status=MatchStatus.ACTIVE,
    )
    match.id = uuid.uuid4()
    return match


match_ok = _match(competitor_ok_id, "ok.example.com")
match_blocked = _match(competitor_blocked_id, "blocked.example.com")
fake_session.seed(match_ok, match_blocked)

# Certified UNSUPPORTED only AFTER the first dispatch -- which is exactly
# the case the retry path has to catch. "ok.example.com" gets no
# `domain_playbooks` row at all, so `get_domain_state` resolves it to its
# documented UNKNOWN default (not UNSUPPORTED) and it dispatches normally.
blocked_playbook = DomainPlaybook(
    domain="blocked.example.com",
    preferred_access_method=AccessMethod.DIRECT_HTTP,
    method_templates=[],
    state=DomainState.UNSUPPORTED,
)
fake_session.seed(blocked_playbook)


def _stalled_target(match_id):
    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=job_id,
        match_id=match_id,
        status=ScrapeTargetStatus.PENDING,
        created_at=base_now,
        dispatched_at=base_now - timedelta(seconds=TIMEOUT * 2),
    )
    target.id = uuid.uuid4()
    return target


target_ok = _stalled_target(match_ok.id)
target_blocked = _stalled_target(match_blocked.id)
fake_session.seed(target_ok, target_blocked)

# --- the recovery sweep --------------------------------------------------

tasks_jobs.recover_stalled_batches()

# 1. The UNSUPPORTED domain's batch is never re-POSTed; the sibling on an
#    unremarkable domain still is.
if len(calls) != 1:
    print("EXPECTED_ONE_POST_GOT:" + str(len(calls)))
    sys.exit(1)

if calls[0]["data"]["match_ids"] != str(match_ok.id):
    print("UNEXPECTED_REDISPATCHED_MATCH_IDS:" + str(calls[0]["data"]["match_ids"]))
    sys.exit(1)

# 2. The skipped target got the product-visible, terminal outcome from
#    `unsupported_target_outcome()` via `mark_target` -- SKIPPED/BLOCKED.
if target_blocked.status is not ScrapeTargetStatus.SKIPPED:
    print("BLOCKED_TARGET_STATUS_NOT_SKIPPED:" + str(target_blocked.status))
    sys.exit(1)

if target_blocked.error_code is not ScrapeErrorCode.BLOCKED:
    print("BLOCKED_TARGET_ERROR_CODE_WRONG:" + str(target_blocked.error_code))
    sys.exit(1)

# 3. It was never re-dispatched: its `dispatched_at` is still the ORIGINAL
#    stamp, not a fresh one from this sweep.
if target_blocked.dispatched_at != base_now - timedelta(seconds=TIMEOUT * 2):
    print("BLOCKED_TARGET_DISPATCH_STAMP_REFRESHED:" + str(target_blocked.dispatched_at))
    sys.exit(1)

# --- a later sweep in a FRESH stall window must still not retry it -------
#
# SKIPPED is terminal, so the selection query never offers this target
# again -- "zero retries" holds structurally, not because one window's
# Redis SET NX guard happened to suppress it.

_FakeDatetime._now = base_now + timedelta(seconds=TIMEOUT * 2)

tasks_jobs.recover_stalled_batches()

if any(call["data"]["match_ids"] == str(match_blocked.id) for call in calls):
    print("UNSUPPORTED_TARGET_WAS_RETRIED_IN_A_LATER_WINDOW")
    sys.exit(1)

if target_blocked.status is not ScrapeTargetStatus.SKIPPED:
    print("BLOCKED_TARGET_LEFT_TERMINAL_STATUS:" + str(target_blocked.status))
    sys.exit(1)

print("OK")
sys.exit(0)
"""


def test_recover_stalled_batches_skips_unsupported_domain_without_retry() -> None:
    """EPA W4 gate-review follow-up F5: a stall recovery is a retry, so
    `recover_stalled_batches` applies the same `DomainState.UNSUPPORTED`
    skip `dispatch_job` does (`tests/unit/test_jobs_dispatch_task.py`'s
    `test_dispatch_job_skips_unsupported_domain_target_without_dispatch_or_retry`).
    A domain certified UNSUPPORTED after its targets were first dispatched
    must be marked SKIPPED/BLOCKED instead of re-POSTed, must not have its
    dispatch stamp refreshed, and must stay unretried in a later stall
    window -- while a sibling target on an unremarkable (UNKNOWN) domain
    in the SAME job is recovered normally."""
    env = {**os.environ, **_STALL_RECOVERY_ENV}
    result = subprocess.run(
        [sys.executable, "-c", _UNSUPPORTED_STALL_CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=None,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


# --- EPA Phase C F2 + F6: the grant, the denial, and the release ----------
#
# Three claims the phase's gate review found had no test at all. The
# always-grant stub every dispatch test uses proved the happy path and
# only the happy path, so a call site could ignore a denial, or drop the
# grant on the floor, and every suite would stay green.

# F2: the RETRY's grant must reach the POST. Without it the operations a
# recovery batch opens are NULL-linked, C3's sweeper cannot see them in
# C1's ledger, the lease lapses while the re-POSTed fetches are still
# running, and the retry's spend is never settled against any budget.
_STALL_THREADS_THE_GRANT_CHECK = _REAPER_PRELUDE + """
daemon["payload"] = None

job = new_job(7200)
target = new_target(job, 7200)

tasks_jobs.recover_stalled_batches()

if len(calls) != 1:
    fail("EXPECTED_ONE_RECOVERY_POST_GOT:" + str(len(calls)))

posted = calls[0]["data"]
if not posted.get("authorization_id"):
    fail("RECOVERY_POST_CARRIED_NO_AUTHORIZATION_ID:" + str(sorted(posted)))

# ...and the decision facts behind it (F3), so the operations it opens can
# be audited without joining back to the reservation.
for field in ("budget_decision_version", "entitlement_version", "breaker_decision"):
    if not posted.get(field):
        fail("RECOVERY_POST_MISSING_DECISION_FACT:" + field)

print("OK")
sys.exit(0)
"""

# F6: a DENIED recovery must leave the batch exactly as it found it --
# still stalled, still unstamped, nothing POSTed. A denial that re-POSTs
# anyway is the failure mode the gate exists to remove; a denial that
# stamps the targets is worse, because it makes the work look dispatched
# and no later sweep will ever offer it again.
_STALL_DENIAL_CHECK = _REAPER_PRELUDE + """
from _costauth_test_stub import DenyingCostAuthorizationService
stub_cost_authorization(tasks_jobs, DenyingCostAuthorizationService)

daemon["payload"] = None

job = new_job(7200)
target = new_target(job, 7200)
stale_stamp = target.dispatched_at

tasks_jobs.recover_stalled_batches()

if calls:
    fail("DENIED_RECOVERY_STILL_POSTED:" + str(len(calls)))

if target.dispatched_at != stale_stamp:
    fail("DENIED_RECOVERY_STAMPED_ITS_TARGETS:" + str(target.dispatched_at))

if target.status is not ScrapeTargetStatus.PENDING:
    fail("DENIED_RECOVERY_MOVED_THE_TARGET:" + str(target.status))

# Still stalled, so a later sweep re-offers it once the denial clears.
from _costauth_test_stub import AlwaysGrantCostAuthorizationService
stub_cost_authorization(tasks_jobs, AlwaysGrantCostAuthorizationService)
tasks_jobs.recover_stalled_batches()

if len(calls) != 1:
    fail("TARGET_WAS_NOT_RE_OFFERED_AFTER_THE_DENIAL_CLEARED:" + str(len(calls)))

print("OK")
sys.exit(0)
"""

# F6: a POST that BLOWS UP must return the hold immediately rather than
# leaving it to the lease. Nothing was spent, so nothing is settled.
_STALL_RELEASES_ON_POST_FAILURE_CHECK = _REAPER_PRELUDE + """
from _costauth_test_stub import AlwaysGrantCostAuthorizationService

released = []


class _RecordingCostAuth(AlwaysGrantCostAuthorizationService):
    def release(self, authorization_id, **kwargs):
        released.append(authorization_id)


stub_cost_authorization(tasks_jobs, _RecordingCostAuth)

daemon["payload"] = None


def exploding_post(url, *, data, auth, timeout):
    raise requests.ConnectionError("scrapyd unreachable")


def exploding_client_factory(*, settings=None, intents=None):
    http_session = requests.Session()
    http_session.post = exploding_post
    http_session.get = fake_get
    return RealClient(settings=settings, redis_client=fake_redis, session=http_session)


tasks_jobs.ScrapydDispatchClient = exploding_client_factory

job = new_job(7200)
target = new_target(job, 7200)

try:
    tasks_jobs.recover_stalled_batches()
except Exception:
    pass

if len(released) != 1:
    fail("HOLD_WAS_NOT_RELEASED_AFTER_A_FAILED_POST:" + str(len(released)))

print("OK")
sys.exit(0)
"""


def test_recovery_post_carries_the_retrys_own_grant() -> None:
    """EPA Phase C F2: `recover_stalled_batches` authorized but did not
    forward `authorization_id` to `schedule()`, so every RETRY operation
    was NULL-linked — invisible to the sweeper's ledger-liveness check,
    which then lapsed the lease and released the hold while the re-POSTed
    fetches were still running."""
    _run_reaper_check(_STALL_THREADS_THE_GRANT_CHECK)


def test_a_denied_recovery_posts_nothing_and_leaves_the_batch_stalled() -> None:
    """EPA Phase C F6: the denial branch of the reaper's gate, tested.

    Denied means untouched: no POST, no stamp, no status change — and the
    target is re-offered by the next sweep once the denial clears."""
    _run_reaper_check(_STALL_DENIAL_CHECK)


def test_a_failed_recovery_post_returns_the_hold_immediately() -> None:
    """EPA Phase C F6: `costauth.release` on the `schedule()` exception
    path. Nothing was spent, so the hold goes back now rather than after
    a 900s lease — and `release` is CAS-idempotent, so a Celery retry
    cannot double-credit the budget."""
    _run_reaper_check(_STALL_RELEASES_ON_POST_FAILURE_CHECK)
