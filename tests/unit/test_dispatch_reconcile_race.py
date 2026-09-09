"""A transient absence is not permission to repost (R11, 2026-09-09).

The finding, exactly: `reconcile_inflight_intents` discarded its own
`now`, scanned every `POSTED` intent with no minimum age, and called
`mark_missing` on the first `listjobs.json` answer that did not mention
the job. Because the five-step protocol commits `POSTED` *before* it
sends `schedule.json`, there is a real window in which that answer is
correct and useless:

    worker commits POSTED -> the sweep lists the node before the schedule
    request arrives -> the node (truthfully) does not have it ->
    RECONCILED_MISSING -> the original POST then succeeds

and the row now says a re-POST is authorized for work that is running.
That re-POST is not harmless. **Scrapyd 1.6.0's `Schedule.render_POST`
hands the client-supplied `jobid` to the scheduler as `_job` with no
deduplication whatsoever** — the deterministic id makes a duplicate
traceable, it does not make it idempotent — so the node queues a second
run under a colliding name. And upstream the re-plan takes a second C3
reservation. One logical job, two executions, two cost authorizations.

What is asserted here, and why each assertion earns its place
------------------------------------------------------------
* **The race itself, end to end.** `test_the_exact_race` drives the REAL
  client, store and reconciler, and runs the reconciler from *inside*
  the fake node's `post` handler — that is literally "the reconciler
  lists jobs before the schedule request arrives", not a reconstruction
  of it. Then the POST succeeds. One execution, and the intent is
  CONFIRMED, not RECONCILED_MISSING.
* **One denial is never proof.** Past the lease, the first negative
  answer parks the row in `RECONCILED_AMBIGUOUS`, which authorizes
  nothing; only a second independent denial a real interval later
  reaches `RECONCILED_MISSING`.
* **AMBIGUOUS is a hard stop on the dispatch path too**, not merely a
  label the sweep writes: `schedule()` refuses rather than re-POSTing.
* **A node that has forgotten a NEWER run cannot testify about an older
  one.** This is the "recently disappeared history entry" half:
  `MemoryJobStorage` keeps 100 finished entries and loses them on
  restart, so a node whose memory does not reach back as far as the
  intent under test proves nothing by omitting it.
* **Execution identity is verified AT THE RECEIVER before a recovery
  re-POST**, because Scrapyd will not do it for us.
* **One logical job, one cost authorization**, through the real
  `dispatch_job` in a subprocess — the planner consults the durable
  ledger before it reserves, so an unsettled intent never takes a second
  hold against the workspace's budget.

Everything in-process here uses `FakeOrmSession`, a fake Redis and a
fake node; nothing can reach a real node, Redis or Postgres.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_shared.enums import DispatchIntentState, ScrapeJobStatus
from app_shared.jobs.dispatch_intents import (
    REPOST_AUTHORIZING_STATES,
    AbsencePolicy,
    AbsenceVerdict,
    DispatchIntentStore,
    receiver_holds_execution,
    reconcile_inflight_intents,
)
from app_shared.models.dispatch import DispatchIntent
from app_shared.models.jobs import ScrapeJob
from app_shared.scrapyd.client import ScrapydDispatchClient
from app_shared.scrapyd.errors import ScrapydDispatchError
from app_shared.scrapyd.identity import build_dispatch_identity

from unit._jobs_fake_session import FakeOrmSession

_NODE = "http://scrapers-a:6800"
_PROJECT = "price_monitor"
_SPIDER = "generic_price_spider"


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, name, value, *, nx=False, ex=None):
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True

    def get(self, name):
        return self.store.get(name)

    def delete(self, *names):
        return sum(1 for name in names if self.store.pop(name, None) is not None)


class FakeScrapydNode:
    """A node that accepts POSTs and can be made to answer mid-flight.

    ``on_post`` is the seam the race needs: it runs *before* the job is
    registered, which is the state of the world while a ``schedule.json``
    request is still travelling. ``list_jobs`` keeps the real client's
    contract — a set, and a raise (never an empty set) when unreachable.
    """

    def __init__(self) -> None:
        self.posted_jobids: list[str] = []
        self.known: set[str] = set()
        self.reachable = True
        self.on_post = None

    @property
    def post_count(self) -> int:
        return len(self.posted_jobids)

    def forget(self, jobid: str) -> None:
        self.known.discard(str(jobid))

    def post(self, url, *, data, auth, timeout):
        if self.on_post is not None:
            self.on_post()
        jobid = str(data.get("jobid") or uuid.uuid1().hex)
        self.posted_jobids.append(jobid)
        self.known.add(jobid)
        return SimpleNamespace(
            status_code=200, json=lambda: {"status": "ok", "jobid": jobid}
        )

    def list_jobs(self, node_url, project=None):
        if not self.reachable:
            raise requests.ConnectionError(f"{node_url} unreachable")
        return set(self.known)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        SCRAPYD_HTTP_URLS=[_NODE],
        SCRAPYD_BROWSER_URLS=["http://scrapers-browser:6800"],
        SCRAPYD_USERNAME="scrapyd",
        SCRAPYD_PASSWORD="change-me",
        SCRAPYD_DISPATCH_GUARD_TTL_SECONDS=900,
        SCRAPYD_DETERMINISTIC_JOBID=True,
    )


class _World:
    def __init__(self) -> None:
        self.workspace_id = uuid.uuid4()
        self.job_id = uuid.uuid4()
        self.session = FakeOrmSession()
        job = ScrapeJob(
            workspace_id=self.workspace_id,
            status=ScrapeJobStatus.RUNNING,
            total_targets=1,
            cancellation_generation=0,
            planning_generation=1,
        )
        job.id = self.job_id
        self.session.seed(job)
        self.node = FakeScrapydNode()
        self.redis = FakeRedis()
        self.identity = self.make_identity()

    def make_identity(self, match_ids=("m1", "m2")):
        return build_dispatch_identity(
            scrape_job_id=str(self.job_id),
            planning_generation=1,
            strategy_method="HTTP/JSONLD/v1",
            domain="shop.example.com",
            mode="HTTP",
            node_class=f"{_PROJECT}:{_SPIDER}",
            match_ids=list(match_ids),
        )

    @contextmanager
    def factory(self) -> Iterator[FakeOrmSession]:
        yield self.session

    def store(self) -> DispatchIntentStore:
        return DispatchIntentStore(
            None,
            workspace_id=self.workspace_id,
            scrape_job_id=self.job_id,
            session_factory=self.factory,
        )

    def client(self, intents: DispatchIntentStore) -> ScrapydDispatchClient:
        http = requests.Session()
        http.post = self.node.post
        return ScrapydDispatchClient(
            settings=_settings(),
            redis_client=self.redis,
            session=http,
            intents=intents,
        )

    def rows(self) -> list[DispatchIntent]:
        return self.session._rows[DispatchIntent]  # noqa: SLF001 - the fake's storage

    def intent(self, identity=None) -> DispatchIntent:
        key = (identity or self.identity).key
        matching = [row for row in self.rows() if row.identity_key == key]
        assert len(matching) == 1, f"expected one intent for {key}, got {len(matching)}"
        return matching[0]

    def schedule(self, client, jobid: str, identity=None) -> str:
        identity = identity or self.identity
        return client.schedule(
            _PROJECT,
            _SPIDER,
            workspace_id=str(self.workspace_id),
            scrape_job_id=str(self.job_id),
            match_ids=["m1", "m2"],
            mode="HTTP",
            batch_index=0,
            node_url=_NODE,
            identity=identity,
            jobid=jobid,
        )


@pytest.fixture()
def world() -> _World:
    return _World()


def test_the_exact_race(world: _World) -> None:
    """POSTED committed -> sweep lists the node -> the POST then lands.

    The reconciler runs from inside the node's `post` handler, so it sees
    exactly what a sweep racing an in-flight `schedule.json` sees: a
    committed `POSTED` row, and a node that has never heard of the job.
    Before R11 that produced `RECONCILED_MISSING` and cleared a re-POST
    for work that was at that instant being accepted.
    """
    store = world.store()
    planned = store.plan(world.identity, match_ids=("m1", "m2"), node_url=_NODE)
    jobid = str(planned.scrapyd_job_id)

    reports = []
    world.node.on_post = lambda: reports.append(
        reconcile_inflight_intents(world.factory, world.node)
    )

    answered = world.schedule(world.client(store), jobid)

    assert answered == jobid
    # One execution. The reconciler never POSTs, and it did not authorize
    # anyone else to.
    assert world.node.post_count == 1
    assert world.node.posted_jobids == [jobid]

    assert len(reports) == 1
    report = reports[0]
    assert report.examined == 1
    assert report.in_flight == 1, "the row is inside the in-flight lease"
    assert report.missing == 0 and report.ambiguous == 0

    # And the row is settled by the POST that succeeded, not by the sweep
    # that raced it.
    assert world.intent().state == DispatchIntentState.CONFIRMED
    assert world.intent().absent_observations == 0


def test_a_denial_inside_the_lease_is_not_even_recorded(world: _World) -> None:
    """It is not weak evidence — it is evidence about a different question.

    Recording it "at a discount" would still let a fast enough pass
    cadence accumulate a quorum out of answers taken before the request
    could possibly have arrived.
    """
    store = world.store()
    store.plan(world.identity, match_ids=("m1", "m2"), node_url=_NODE)
    store.record_post(world.identity, node_url=_NODE)

    for _ in range(5):
        outcome = store.record_absence(
            world.identity, node_url=_NODE, policy=AbsencePolicy()
        )
        assert outcome.verdict is AbsenceVerdict.IN_FLIGHT

    row = world.intent()
    assert row.state == DispatchIntentState.POSTED
    assert row.absent_observations == 0
    assert row.first_absent_at is None


def test_one_denial_past_the_lease_is_ambiguous_not_missing(world: _World) -> None:
    store = world.store()
    store.plan(world.identity, match_ids=("m1", "m2"), node_url=_NODE)
    store.record_post(world.identity, node_url=_NODE)
    posted_at = world.intent().posted_at

    outcome = store.record_absence(
        world.identity, now=posted_at + timedelta(seconds=300), node_url=_NODE
    )

    assert outcome.verdict is AbsenceVerdict.AMBIGUOUS
    assert not outcome.authorizes_repost
    row = world.intent()
    assert row.state == DispatchIntentState.RECONCILED_AMBIGUOUS
    assert row.state not in REPOST_AUTHORIZING_STATES
    assert row.absent_observations == 1

    # A second denial in the SAME instant is not a second observation in
    # any useful sense: the window has not elapsed.
    outcome = store.record_absence(
        world.identity, now=posted_at + timedelta(seconds=300), node_url=_NODE
    )
    assert outcome.verdict is AbsenceVerdict.AMBIGUOUS
    assert world.intent().state == DispatchIntentState.RECONCILED_AMBIGUOUS

    # Separated by the absence window, it corroborates.
    outcome = store.record_absence(
        world.identity, now=posted_at + timedelta(seconds=600), node_url=_NODE
    )
    assert outcome.verdict is AbsenceVerdict.MISSING
    assert outcome.authorizes_repost
    assert world.intent().state == DispatchIntentState.RECONCILED_MISSING


def test_an_ambiguous_intent_is_a_hard_stop_on_the_dispatch_path(
    world: _World,
) -> None:
    """The label has to bind the sender, not just describe the row.

    A `RECONCILED_AMBIGUOUS` row reaching `schedule()` must be refused.
    Letting the dispatch path ask the node again would hand it a second,
    softer answer to the question the sweep already declined to answer —
    `reconcile_inflight` would report ABSENT, roll the row back to
    PLANNED, and re-POST.
    """
    store = world.store()
    planned = store.plan(world.identity, match_ids=("m1", "m2"), node_url=_NODE)
    jobid = str(planned.scrapyd_job_id)
    store.record_post(world.identity, node_url=_NODE)
    posted_at = world.intent().posted_at
    store.record_absence(
        world.identity, now=posted_at + timedelta(seconds=300), node_url=_NODE
    )
    assert world.intent().state == DispatchIntentState.RECONCILED_AMBIGUOUS

    world.redis.store.clear()  # the guard aged out; it is not the authority
    with pytest.raises(ScrapydDispatchError, match="refusing"):
        world.schedule(world.client(world.store()), jobid)

    assert world.node.post_count == 0


def test_a_redelivery_inside_the_lease_is_refused_not_re_posted(
    world: _World,
) -> None:
    """The dispatch-path half of the same race.

    An at-least-once redelivery arriving seconds behind the original
    would ask the node about a request still on the wire, be told "no
    such job", and take that as authorization to send it again.
    """
    store = world.store()
    planned = store.plan(world.identity, match_ids=("m1", "m2"), node_url=_NODE)
    jobid = str(planned.scrapyd_job_id)
    store.record_post(world.identity, node_url=_NODE)

    world.redis.store.clear()
    with pytest.raises(ScrapydDispatchError, match="in-flight lease"):
        world.schedule(world.client(world.store()), jobid)

    assert world.node.post_count == 0
    assert world.intent().state == DispatchIntentState.POSTED


def test_a_node_that_forgot_a_newer_run_cannot_testify(world: _World) -> None:
    """The "recently disappeared history entry" case.

    Both nodes run `MemoryJobStorage` with `finished_to_keep = 100` and
    lose the whole history on restart. The sweep dates the node's memory
    from the outside using intents it CONFIRMED there: if a run the node
    accepted *after* ours is no longer listed, the node's memory does not
    reach back as far as ours, so its silence about ours proves nothing.
    """
    store = world.store()
    store.plan(world.identity, match_ids=("m1", "m2"), node_url=_NODE)
    store.record_post(world.identity, node_url=_NODE)
    posted_at = world.intent().posted_at

    # A second, NEWER dispatch on the same node, confirmed and then
    # forgotten by the node — the restart/rollover signal.
    witness_identity = world.make_identity(match_ids=("m9",))
    witness_store = world.store()
    witness = witness_store.plan(
        witness_identity, match_ids=("m9",), node_url=_NODE
    )
    witness_jobid = str(witness.scrapyd_job_id)
    witness_store.record_post(witness_identity, node_url=_NODE)
    witness_store.confirm(witness_identity, witness_jobid)
    world.node.known.add(witness_jobid)
    assert world.intent(witness_identity).confirmed_at >= posted_at

    # With the witness still listed, the node's memory is intact and a
    # denial past the lease counts (one denial -> ambiguous, but the
    # ledger records it).
    world.node.on_post = None
    report = reconcile_inflight_intents(
        world.factory, world.node, now=posted_at + timedelta(seconds=300)
    )
    assert report.ambiguous == 1
    assert world.intent().absent_observations == 1

    # Now the node forgets the newer run. It has demonstrably lost
    # something more recent than our intent, so nothing it fails to
    # mention is evidence — the denial is not recorded.
    world.node.forget(witness_jobid)
    report = reconcile_inflight_intents(
        world.factory, world.node, now=posted_at + timedelta(seconds=900)
    )
    assert report.ambiguous >= 1 and report.missing == 0
    assert world.intent().state == DispatchIntentState.RECONCILED_AMBIGUOUS
    assert world.intent().absent_observations == 1, (
        "a denial from a node with a rolled history must not be recorded"
    )


def test_past_the_horizon_absence_never_becomes_proof(world: _World) -> None:
    """A run older than the node's history cannot be spoken about at all."""
    store = world.store()
    store.plan(world.identity, match_ids=("m1", "m2"), node_url=_NODE)
    store.record_post(world.identity, node_url=_NODE)
    posted_at = world.intent().posted_at
    stale = posted_at + timedelta(days=3)

    for offset in (0, 3600, 7200):
        outcome = store.record_absence(
            world.identity, now=stale + timedelta(seconds=offset), node_url=_NODE
        )
        assert outcome.verdict is AbsenceVerdict.AMBIGUOUS

    assert world.intent().state == DispatchIntentState.RECONCILED_AMBIGUOUS
    assert "horizon" in world.intent().error_message


def test_mark_missing_refuses_an_uncorroborated_row(world: _World) -> None:
    """The gate is in the writer, not only in its caller.

    `mark_missing` is the one transition in the module that authorizes
    spending, so it re-asserts the evidence itself rather than trusting
    whoever called it.
    """
    store = world.store()
    store.plan(world.identity, match_ids=("m1", "m2"), node_url=_NODE)
    store.record_post(world.identity, node_url=_NODE)

    store.mark_missing(world.identity, "the node said no, honest")

    assert world.intent().state == DispatchIntentState.POSTED


def test_the_receiver_is_asked_before_a_recovery_repost(world: _World) -> None:
    """Scrapyd 1.6.0 will not dedup `_job`, so we check it ourselves."""
    jobid = str(uuid.uuid4())
    node_class = f"{_PROJECT}:{_SPIDER}"

    # The receiver does not hold it: the re-POST may proceed.
    assert (
        receiver_holds_execution(
            world.node, node_url=_NODE, node_class=node_class, scrapyd_job_id=jobid
        )
        is False
    )

    # The receiver holds it: never send a second copy.
    world.node.known.add(jobid)
    assert (
        receiver_holds_execution(
            world.node, node_url=_NODE, node_class=node_class, scrapyd_job_id=jobid
        )
        is True
    )

    # The receiver cannot be asked: `None`, which is not `False`, so a
    # caller that branches on `is not False` refuses to send.
    world.node.reachable = False
    assert (
        receiver_holds_execution(
            world.node, node_url=_NODE, node_class=node_class, scrapyd_job_id=jobid
        )
        is None
    )

    # And an intent with no recorded node cannot be checked at all.
    world.node.reachable = True
    assert (
        receiver_holds_execution(
            world.node, node_url="", node_class=node_class, scrapyd_job_id=jobid
        )
        is None
    )


# --- the whole thing, through the real `dispatch_job` ------------------------
#
# Subprocess-loaded per this repo's convention for anything importing a
# service's top-level `app` package (`apps/api` and `apps/workers` each
# ship one, and `celery_app.py` calls `get_settings()` at import).

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

_ONE_EXECUTION_ONE_AUTHORIZATION = """
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

import requests

from _jobs_fake_session import FakeOrmSession
from _costauth_test_stub import (
    AlwaysGrantCostAuthorizationService,
    stub_cost_authorization,
)
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
from app_shared.jobs.dispatch_intents import reconcile_inflight_intents
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.dispatch import DispatchIntent
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.scrapyd.client import ScrapydDispatchClient as RealClient

import app.workers.tasks_jobs as tasks_jobs


def fail(message):
    print(message)
    sys.exit(1)


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
        return sum(1 for n in names if self.store.pop(n, None) is not None)


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


# --- the node, and the sweep that races it ---------------------------------

posts = []
known = set()
sweeps = []


def fake_post(url, *, data, auth, timeout):
    # THE RACE: the maintenance sweep runs while this request is still on
    # the wire. The node has not registered the job yet, so listjobs.json
    # truthfully omits it -- which is precisely the answer the pre-R11
    # reconciler read as "this dispatch never happened".
    sweeps.append(reconcile_inflight_intents(fake_get_session, node_client))
    jobid = str(data.get("jobid") or uuid.uuid1().hex)
    posts.append(jobid)
    known.add(jobid)
    return FakeResponse({"status": "ok", "jobid": jobid})


class NodeLister:
    def list_jobs(self, node_url, project=None):
        return set(known)

    def daemon_status(self, node_url):
        return {"pending": 0, "running": 0, "finished": 0}


node_client = NodeLister()
fake_redis = FakeRedis()


def client_factory(*, settings=None, intents=None):
    http_session = requests.Session()
    http_session.post = fake_post
    # `intents=intents` is load-bearing here in a way it is not in the
    # older dispatch-task test: without the durable authority the client
    # never writes `POSTED`, and there would be no in-flight row for the
    # racing sweep to get wrong.
    client = RealClient(
        settings=settings,
        redis_client=fake_redis,
        session=http_session,
        intents=intents,
    )
    client.list_jobs = node_client.list_jobs
    return client


tasks_jobs.ScrapydDispatchClient = client_factory


# --- the C3 gate: counted, not stubbed away --------------------------------

authorizations = []


class CountingCostAuthorization(AlwaysGrantCostAuthorizationService):
    def authorize(self, req):
        grant = super().authorize(req)
        authorizations.append(getattr(req, "dedupe_key", None))
        return grant


stub_cost_authorization(tasks_jobs, CountingCostAuthorization)

fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


tasks_jobs.get_session = fake_get_session
tasks_jobs.get_system_session = fake_get_session
tasks_jobs.set_workspace_context = lambda session, workspace_id: None

# --- one job, one target, one batch ----------------------------------------

workspace_id = uuid.uuid4()
job_id = uuid.uuid4()
competitor_id = uuid.uuid4()
match_id = uuid.uuid4()
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

match = CompetitorProductMatch(
    workspace_id=workspace_id,
    product_id=uuid.uuid4(),
    product_variant_id=uuid.uuid4(),
    competitor_id=competitor_id,
    competitor_url="https://a.example.com/p",
    normalized_competitor_url="https://a.example.com/p",
    url_pattern="https://a.example.com/p",
    url_pattern_version=1,
    priority=MatchPriority.NORMAL,
    status=MatchStatus.ACTIVE,
)
match.id = match_id
competitor = Competitor(workspace_id=workspace_id, name="A", domain="a.example.com")
competitor.id = competitor_id
target = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=job_id,
    match_id=match_id,
    status=ScrapeTargetStatus.PENDING,
    created_at=now,
)
target.id = uuid.uuid4()
fake_session.seed(match, competitor, target)

# --- dispatch, with the sweep racing every POST ----------------------------

tasks_jobs.dispatch_job(str(job_id), str(workspace_id))

if len(posts) != 1:
    fail("EXPECTED_ONE_EXECUTION_GOT:" + str(len(posts)))
if not sweeps or sweeps[0].in_flight != 1:
    fail("SWEEP_DID_NOT_TREAT_THE_DISPATCH_AS_IN_FLIGHT:" + repr(sweeps))
if sweeps[0].missing != 0:
    fail("SWEEP_DECLARED_AN_IN_FLIGHT_DISPATCH_MISSING:" + repr(sweeps[0]))
if len(authorizations) != 1:
    fail("EXPECTED_ONE_COST_AUTHORIZATION_GOT:" + str(len(authorizations)))

intents = fake_session._rows[DispatchIntent]
if len(intents) != 1:
    fail("EXPECTED_ONE_INTENT_GOT:" + str(len(intents)))
if intents[0].state != DispatchIntentState.CONFIRMED:
    fail("INTENT_NOT_CONFIRMED:" + str(intents[0].state))

# --- and a Celery redelivery of the same job adds neither ------------------

fake_redis.store.clear()
tasks_jobs.dispatch_job(str(job_id), str(workspace_id))

if len(posts) != 1:
    fail("REDELIVERY_PRODUCED_A_SECOND_EXECUTION:" + str(posts))
if len(authorizations) != 1:
    fail("REDELIVERY_PRODUCED_A_SECOND_AUTHORIZATION:" + str(authorizations))

print("OK")
"""


def test_one_logical_job_one_execution_one_cost_authorization() -> None:
    """The acceptance criterion, against the real `dispatch_job`.

    The sweep runs from inside every `schedule.json` POST, so it always
    sees the intent in exactly the racing state R11 describes. Nothing is
    re-POSTed, and the workspace's budget is asked for money exactly once
    for the one logical batch.
    """
    result = subprocess.run(
        [sys.executable, "-c", _ONE_EXECUTION_ONE_AUTHORIZATION],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, **_ENV},
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip().endswith("OK")
