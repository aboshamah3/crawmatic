"""The five-step dispatch protocol end to end, against a fake Scrapyd node.

EPA B2 / F06. Unlike `test_dispatch_kill_after_post.py` (which drives the
client and the store directly), this file drives the REAL
`app.workers.tasks_jobs.dispatch_job` — the planner, the intent store in
its short-transaction mode, the real `ScrapydDispatchClient`, the real
`stamp_targets_dispatched`, and the real
`app_shared.jobs.dispatch_intents.reconcile_inflight_intents` — so what
is asserted is the *task's* behaviour, not a reconstruction of it.

Two scenarios, both starting from the same crash:

**A. The node has the run.** A worker dies between Scrapyd's `status=ok`
and the `CONFIRMED` write. The targets are NOT stamped (nothing proved
the POST landed), the intent is committed `POSTED` with its node and its
`scrapyd_job_id`, and exactly one POST was issued. The reconciler asks
that node, finds the run, and confirms. A subsequent Celery redelivery of
`dispatch_job` then issues **no** second POST and stamps
`dispatched_at`/`remote_accepted_at` through the single stamping path,
because the durable intent is now the proof that path demands.

**B. The node has forgotten it.** Same crash, but every node answers and
none knows the job. The intent moves to `RECONCILED_MISSING` — the only
state that authorizes a re-POST — and the redelivery re-POSTs with the
**same** `jobid`, which is what makes the duplicate safe.

## Why this lives in `tests/integration/` and needs no Postgres

Same reasoning as `test_dispatch_stamping.py`: the file path matches the
plan, and everything runs against `FakeOrmSession` (which evaluates real
SQLAlchemy `WHERE` clauses in memory) plus a fake Redis and a fake HTTP
transport. No Postgres connection is opened and **no real Scrapyd node
can be reached** — `SCRAPYD_*_URLS` point at `http://127.0.0.1:1` and the
only `post`/`get` in play belong to the fake node object.

## Why a subprocess

`apps/api/app` and `apps/workers/app` are both named `app`, so
`app.workers.tasks_jobs` is only unambiguously importable in a process
that has not already resolved `app` to the other package — the identical
isolation pattern `test_jobs_dispatch_task.py`, `test_dispatch_routing.py`
and `test_dispatch_stamping.py` each use.
"""

from __future__ import annotations

import os
import subprocess
import sys

_FIXTURES = '''
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

import requests

from _jobs_fake_session import FakeOrmSession
from app_shared.enums import (
    DispatchIntentState,
    MatchPriority,
    MatchStatus,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.jobs.dispatch_intents import (
    DispatchIntentStore as RealStore,
    reconcile_inflight_intents,
)
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.dispatch import DispatchIntent
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.scrapyd.client import ScrapydDispatchClient as RealClient

import app.workers.tasks_jobs as tasks_jobs


def fail(message):
    print(message)
    sys.exit(1)


class ProcessDied(BaseException):
    """The worker being killed -- deliberately not an `Exception`.

    `dispatch_job`'s own `except Exception` cleanup must NOT run: a
    killed worker leaves the row `POSTED`, which is the honest state,
    rather than tidying it into `FAILED` on the strength of a guess.
    """


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


class FakeNode:
    """One Scrapyd node with a memory that can be wiped."""

    def __init__(self):
        self.posted_jobids = []
        self.known = set()

    @property
    def post_count(self):
        return len(self.posted_jobids)

    def forget_all(self):
        self.known.clear()

    def post(self, url, *, data, auth, timeout):
        jobid = str(data.get("jobid") or uuid.uuid1().hex)
        self.posted_jobids.append(jobid)
        self.known.add(jobid)
        return FakeResponse(200, {"status": "ok", "jobid": jobid})

    def list_jobs(self, node_url, project=None):
        return set(self.known)


node = FakeNode()
fake_redis = FakeRedis()

# The kill switch: armed for the first `confirm`, then disarmed, so the
# SAME code path is exercised before and after the crash.
kill_the_worker = {"armed": True}


class KillingStore(RealStore):
    def confirm(self, identity, scrapyd_job_id):
        if kill_the_worker["armed"]:
            kill_the_worker["armed"] = False
            raise ProcessDied("SIGKILL between the accepted POST and the confirm")
        return super().confirm(identity, scrapyd_job_id)


def client_factory(*, settings=None, intents=None):
    http_session = requests.Session()
    http_session.post = node.post
    return RealClient(
        settings=settings,
        redis_client=fake_redis,
        session=http_session,
        intents=intents,
    )


tasks_jobs.ScrapydDispatchClient = client_factory
tasks_jobs.DispatchIntentStore = KillingStore
tasks_jobs.set_workspace_context = lambda session, workspace_id: None

from _costauth_test_stub import stub_cost_authorization
stub_cost_authorization(tasks_jobs)

fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


tasks_jobs.get_session = fake_get_session

workspace_id = uuid.uuid4()
job_id = uuid.uuid4()
now = datetime.now(timezone.utc)

job = ScrapeJob(
    workspace_id=workspace_id,
    type=ScrapeJobType.MANUAL,
    scope=ScrapeScope.MATCH,
    status=ScrapeJobStatus.PENDING,
    total_targets=1,
    source=ScrapeJobSource.API,
    created_at=now,
)
job.id = job_id
fake_session.seed(job)

competitor_id = uuid.uuid4()
match_id = uuid.uuid4()
competitor = Competitor(
    workspace_id=workspace_id, name="t1", domain="t1.example.com"
)
competitor.id = competitor_id
match = CompetitorProductMatch(
    workspace_id=workspace_id,
    product_id=uuid.uuid4(),
    product_variant_id=uuid.uuid4(),
    competitor_id=competitor_id,
    competitor_url="https://t1.example.com/p",
    normalized_competitor_url="https://t1.example.com/p",
    url_pattern="https://t1.example.com/p",
    url_pattern_version=1,
    priority=MatchPriority.NORMAL,
    status=MatchStatus.ACTIVE,
)
match.id = match_id
target = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=job_id,
    match_id=match_id,
    status=ScrapeTargetStatus.PENDING,
    created_at=now,
)
target.id = uuid.uuid4()
fake_session.seed(competitor, match, target)


def the_intent():
    rows = fake_session._rows.get(DispatchIntent) or []
    if len(rows) != 1:
        fail("EXPECTED_EXACTLY_ONE_INTENT_GOT:" + str(len(rows)))
    return rows[0]


def crash_the_first_dispatch():
    raised = None
    try:
        tasks_jobs.dispatch_job(str(job_id), str(workspace_id))
    except ProcessDied as exc:
        raised = exc
    if raised is None:
        fail("THE_WORKER_DEATH_WAS_SWALLOWED")
    row = the_intent()
    if row.state != DispatchIntentState.POSTED:
        fail("INTENT_IS_NOT_POSTED_AFTER_THE_CRASH:" + str(row.state))
    if not row.node_url:
        fail("POSTED_INTENT_RECORDED_NO_NODE_URL")
    if not row.scrapyd_job_id:
        fail("POSTED_INTENT_RECORDED_NO_JOBID")
    if node.post_count != 1:
        fail("EXPECTED_EXACTLY_ONE_POST_GOT:" + str(node.post_count))
    # Step 1 claimed the target; step 4 never ran, so nothing was stamped.
    if target.claimed_at is None:
        fail("TARGET_WAS_NEVER_CLAIMED_IN_THE_PLAN_TRANSACTION")
    if target.dispatched_at is not None:
        fail("TARGET_WAS_STAMPED_WITHOUT_A_CONFIRMED_DISPATCH")
    return row
'''


_RECONCILE_CONFIRMS_AND_NEVER_REPOSTS = (
    _FIXTURES
    + """
row = crash_the_first_dispatch()
jobid = str(row.scrapyd_job_id)

report = reconcile_inflight_intents(fake_get_session, node)

if report.examined != 1 or report.confirmed != 1 or report.missing != 0:
    fail("UNEXPECTED_RECONCILE_REPORT:" + repr(report))
if node.post_count != 1:
    fail("RECONCILIATION_POSTED_SOMETHING:" + str(node.post_count))
if the_intent().state != DispatchIntentState.CONFIRMED:
    fail("INTENT_WAS_NOT_CONFIRMED:" + str(the_intent().state))
if str(the_intent().scrapyd_job_id) != jobid:
    fail("THE_JOBID_MOVED:" + str(the_intent().scrapyd_job_id))

# The Celery redelivery that follows the crash. The durable intent is now
# the proof `stamp_targets_dispatched` requires, so the targets are
# stamped -- without a second POST.
fake_redis.store.clear()   # the guard aged out; it is not the authority
tasks_jobs.dispatch_job(str(job_id), str(workspace_id))

if node.post_count != 1:
    fail("A_SECOND_POST_WAS_ISSUED:" + str(node.posted_jobids))
if target.dispatched_at is None:
    fail("TARGET_WAS_NEVER_STAMPED_AFTER_RECOVERY")
if target.remote_accepted_at is None:
    fail("TARGET_HAS_NO_REMOTE_ACCEPTED_AT")
if target.dispatch_intent_id != the_intent().id:
    fail("TARGET_POINTS_AT_THE_WRONG_INTENT")

print("OK")
sys.exit(0)
"""
)


_ABSENT_ON_NODE_REPOSTS_THE_SAME_JOBID = (
    _FIXTURES
    + """
row = crash_the_first_dispatch()
jobid = str(row.scrapyd_job_id)

# The node lost it: restarted, or the entry aged out of the 100-deep
# finished history. Absence is bounded evidence -- which is exactly why
# the re-POST has to carry the same name.
node.forget_all()
report = reconcile_inflight_intents(fake_get_session, node)

if report.missing != 1 or report.confirmed != 0:
    fail("UNEXPECTED_RECONCILE_REPORT:" + repr(report))
if the_intent().state != DispatchIntentState.RECONCILED_MISSING:
    fail("INTENT_IS_NOT_RECONCILED_MISSING:" + str(the_intent().state))
if node.post_count != 1:
    fail("RECONCILIATION_POSTED_SOMETHING:" + str(node.post_count))

fake_redis.store.clear()
tasks_jobs.dispatch_job(str(job_id), str(workspace_id))

if node.posted_jobids != [jobid, jobid]:
    fail("THE_RE_POST_USED_A_DIFFERENT_JOBID:" + repr(node.posted_jobids))
if the_intent().state != DispatchIntentState.CONFIRMED:
    fail("THE_RE_POST_DID_NOT_CONFIRM:" + str(the_intent().state))
if target.dispatched_at is None:
    fail("TARGET_WAS_NEVER_STAMPED_AFTER_THE_RE_POST")

print("OK")
sys.exit(0)
"""
)


#: Deliberately unreachable endpoints (plan task 0.4): nothing in this
#: file may touch a real Scrapyd node, DB or Redis, and the environment
#: is what enforces that rather than reviewer discipline.
_ENV = {
    "DATABASE_URL": "postgresql+psycopg://invalid:invalid@127.0.0.1:1/invalid",
    "REDIS_URL": "redis://127.0.0.1:1/0",
    "SCRAPYD_HTTP_URLS": "http://127.0.0.1:1",
    "SCRAPYD_BROWSER_URLS": "http://127.0.0.1:1",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def _run(script: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **_ENV},
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")


def test_crash_after_post_is_reconciled_confirmed_and_never_re_posted() -> None:
    """Scenario A — the run exists on the node, so it is adopted, not re-sent."""
    _run(_RECONCILE_CONFIRMS_AND_NEVER_REPOSTS)


def test_a_node_that_forgot_the_job_is_re_posted_with_the_same_jobid() -> None:
    """Scenario B — absence authorizes a re-POST, and only with the same id."""
    _run(_ABSENT_ON_NODE_REPOSTS_THE_SAME_JOBID)
