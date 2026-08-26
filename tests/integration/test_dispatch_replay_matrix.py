"""The dispatch concurrency/crash/replay matrix (EPA B3, report §12.4.3).

Ten deterministic scenarios over the B1/B2 dispatch machinery, plus B3b's
wiring of :func:`app_shared.scrapyd.reconcile.reconcile_inflight` into
`ScrapydDispatchClient.schedule()` step 0b (item 5c): a `POSTED` intent
behind an aged-out sentinel is now reconciled by `schedule()` itself
before it may claim a slot, not just by a standalone recovery sweep.
Every item drives the REAL, unmodified `ScrapydDispatchClient`,
`DispatchIntentStore`, `get_committed_dispatch`, `stamp_targets_dispatched`
and `tasks_jobs.dispatch_job`.

## Step 0 findings, and what they decided about item 7

Both deployed Scrapyd nodes are **1.6.0** — `apps/scrapers/pyproject.toml`
and `apps/scrapers-browser/pyproject.toml` pin `scrapyd>=1.5,<2`,
`uv.lock` resolves that to `1.6.0`, and both Dockerfiles install with
`uv sync --locked`, so the running image's `scrapyd` is that exact build.
Reading it:

* `Schedule.render_POST` declares
  `@param("jobid", required=False, default=lambda: uuid.uuid1().hex)`
  and passes it through as `_job`, replying `{"jobid": jobid}`.
  **A client-supplied `jobid` is honoured verbatim** (Scrapyd
  `.. versionchanged:: 1.2.0`), which is what
  `SCRAPYD_DETERMINISTIC_JOBID` sends.
* `ListJobs.render_GET` returns custom spider `args` **only in the
  `pending` bucket** (`.. versionchanged:: 1.5.0`); `running` and
  `finished` entries carry `id`/`project`/`spider`/timestamps only.
* Both `scrapyd.conf` files set `finished_to_keep = 100` and leave
  `jobstorage` at the stock `MemoryJobStorage`, so finished history is
  capped at 100 entries and lost on restart.

The consequence is that the two correlation mechanisms are **not**
interchangeable, and item 7 exercises both:

| `SCRAPYD_DETERMINISTIC_JOBID` | correlator | works while job is |
| --- | --- | --- |
| on | the intent id IS the remote jobid | pending, running, finished |
| off (today's default) | pending-queue spider `args` | pending only |

With the flag off, a run that has already started is **uncorrelatable**,
and `reconcile_inflight` answers `AMBIGUOUS` rather than guessing —
which is the concrete case for turning the flag on.

## Test-double choices, per group

`FakeOrmSession` (real-`WHERE`-clause evaluation, no engine) + a fake
Redis + a stubbed HTTP transport — the established convention for this
area (`tests/unit/test_jobs_dispatch_task.py`,
`tests/unit/test_jobs_stall_recovery.py`,
`tests/integration/test_dispatch_stamping.py`) — for **items 1, 3–10**.
Every claim in those items is about application ordering (reconcile ->
claim -> POST -> commit -> release, identity derivation, stamping
refusal), which the fakes reproduce exactly and a real database would
only slow down.

**Item 2 is the exception and runs against a real, isolated Postgres.**
"Two simultaneous planners cannot both create an intent" is a claim about
`uq_dispatch_intents_identity_key` and two concurrent transactions —
something `FakeOrmSession` cannot express at all, since it has no
constraints and no isolation. That test creates its own scratch database
(`B3_SCRATCH_DATABASE_URL`), builds only `scrape_jobs`,
`scrape_job_targets` and `dispatch_intents` from the ORM metadata with
foreign keys stripped, and skips when no scratch DSN is configured.

> NOTE: the scratch DDL is generated from the ORM models, so it includes
> `scrape_job_targets.dispatch_intent_id` — a column B2 declares on the
> model but which has **no migration yet**. Item 2 does not touch that
> column; the DB-free items reach it only through in-memory ORM objects.
> Nothing here depends on it existing in a real deployed database.

## Why subprocesses

`apps/api/app` and `apps/workers/app` are both top-level `app` packages,
so `app.workers.*` only resolves unambiguously in a process that has not
already imported the other one, and `celery_app` calls `get_settings()`
at import. Same isolation pattern as the neighbouring suites.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",  # noqa: S105 - throwaway, never a real credential
    "JWT_SECRET": "test-jwt-secret",  # noqa: S105 - throwaway, never a real credential
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


#: Item 2's scratch database. **Never** `.env`'s DSN: this test creates and
#: drops tables, so it must only ever reach a throwaway database. Overridable
#: with `B3_SCRATCH_DATABASE_URL`; when neither that nor the local default is
#: reachable, item 2 skips rather than silently degrading to fakes.
_DEFAULT_SCRATCH_DSN = (
    "postgresql+psycopg://b3_matrix:b3_matrix_scratch"  # noqa: S105 - scratch only
    "@127.0.0.1:5432/crawmatic_b3_matrix"
)
_SCRATCH_DSN = os.environ.get("B3_SCRATCH_DATABASE_URL") or _DEFAULT_SCRATCH_DSN


def _scratch_db_reachable() -> bool:
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(_SCRATCH_DSN, connect_args={"connect_timeout": 3})
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:  # noqa: BLE001 - unreachable is a skip, not a failure
        return False
    return True


def _run(script: str, *, extra_env: dict[str, str] | None = None) -> None:
    """Run one scenario in a fresh interpreter; require a bare ``OK``."""
    env = {**os.environ, **_ENV, **(extra_env or {})}
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr[-4000:]!r}"
    )
    assert result.stdout.strip().splitlines()[-1] == "OK", result.stdout


# ===========================================================================
# Shared preamble: the doubles every group uses.
# ===========================================================================

_DOUBLES = '''
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")


def fail(message):
    print("FAIL:" + str(message))
    sys.exit(1)


class FakeRedis:
    """`set`/`get`/`delete` with the NX semantics the client relies on.

    `expire(key)` models a TTL elapsing -- the ONLY way a key disappears
    in these tests, so "the guard aged out" is always an explicit,
    visible act rather than a timing accident.
    """

    def __init__(self):
        self.store = {}
        self.set_calls = []

    def set(self, name, value, *, nx=False, ex=None):
        self.set_calls.append((name, value, nx, ex))
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

    # -- test affordances ---------------------------------------------------
    expire = delete


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class RecordingTransport:
    """Stands in for `requests`; records every POST and can be scripted.

    `behaviour` is consulted per call: `"ok"` schedules, `"error"`
    answers a non-ok payload, `"boom"` raises (a dead node), `"401"`
    answers Unauthorized.
    """

    def __init__(self):
        self.calls = []
        self.behaviour = ["ok"]
        self.jobid_prefix = "scrapyd-job-"

    def post(self, url, *, data, auth, timeout):
        index = len(self.calls)
        self.calls.append({"url": url, "data": dict(data)})
        mode = self.behaviour[min(index, len(self.behaviour) - 1)]
        if mode == "boom":
            raise OSError("connection refused")
        if mode == "401":
            return FakeResponse(401, {})
        if mode == "error":
            return FakeResponse(200, {"status": "error", "message": "nope"})
        # Scrapyd 1.6.0 honours a client-supplied `jobid` verbatim
        # (Step 0); mirror that so the deterministic-jobid path is really
        # exercised rather than assumed.
        supplied = data.get("jobid")
        jobid = str(supplied) if supplied else self.jobid_prefix + str(index + 1)
        return FakeResponse(200, {"status": "ok", "jobid": jobid})

    @property
    def post_count(self):
        return len(self.calls)


def make_settings(**overrides):
    """A settings object with exactly the attributes the client reads.

    Deliberately NOT `get_settings()`: this keeps `.env` (and its real
    DSNs) entirely out of the picture, which is the standing rule for
    this repo's tests.
    """
    values = dict(
        SCRAPYD_HTTP_URLS=["http://scrapers-1:6800", "http://scrapers-2:6800"],
        SCRAPYD_BROWSER_URLS=["http://scrapers-browser-1:6800"],
        SCRAPYD_USERNAME="scrapyd",
        SCRAPYD_PASSWORD="change-me",
        SCRAPYD_DISPATCH_GUARD_TTL_SECONDS=900,
        SCRAPYD_DETERMINISTIC_JOBID=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)
'''


# ===========================================================================
# Group A -- the REAL planner (`tasks_jobs.dispatch_job`).
#
# Items 1, 3, 4, 8: every one of them is a claim about how the planner
# DERIVES an identity across deliveries and replans, which a hand-built
# identity could not prove.
# ===========================================================================

_PLANNER = (
    _DOUBLES
    + '''
import requests

from _jobs_fake_session import FakeOrmSession
from app_shared.enums import (
    AccessMethod,
    DispatchIntentState,
    ExtractionMethod,
    MatchPriority,
    MatchStatus,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeProfileMode,
    ScrapeScope,
    ScrapeTargetStatus,
    StrategyMethodProofState,
)
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.dispatch import DispatchIntent
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.models.strategy import DomainStrategyMethod, DomainStrategyProfile
from app_shared.scrapyd.client import ScrapydDispatchClient as RealClient

import app.workers.tasks_jobs as tasks_jobs

transport = RecordingTransport()
fake_redis = FakeRedis()


def client_factory(*, settings=None, intents=None):
    http_session = requests.Session()
    http_session.post = transport.post
    return RealClient(
        settings=settings, redis_client=fake_redis, session=http_session, intents=intents
    )


tasks_jobs.ScrapydDispatchClient = client_factory
# EPA C3: the paid-dispatch sites authorize before they POST. That is a
# real DB transaction (budget locks + breaker/entitlement/domain evidence),
# so these DB-free tests stub it; the gate's own behaviour is proven in
# tests/integration/test_cost_authorization.py. `stub_cost_authorization`
# FAILS if the gate is missing, so this cannot hide a deleted gate.
from _costauth_test_stub import stub_cost_authorization
stub_cost_authorization(tasks_jobs)
tasks_jobs.set_workspace_context = lambda session, workspace_id: None

session = FakeOrmSession()

from contextlib import contextmanager


@contextmanager
def fake_get_session():
    yield session


tasks_jobs.get_session = fake_get_session

now = datetime.now(timezone.utc)
workspace_id = uuid.uuid4()
job_id = uuid.uuid4()
competitor_id = uuid.uuid4()

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
job.cancellation_generation = 0
job.planning_generation = 0
session.seed(job)

competitor = Competitor(workspace_id=workspace_id, name="A", domain="a.example.com")
competitor.id = competitor_id
session.seed(competitor)

matches = []
targets = []
for _ in range(4):
    match = CompetitorProductMatch(
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=competitor_id,
        competitor_url="https://a.example.com/p",
        normalized_competitor_url="https://a.example.com/p",
        url_pattern="a.example.com",
        url_pattern_version=1,
        priority=MatchPriority.NORMAL,
        status=MatchStatus.ACTIVE,
    )
    match.id = uuid.uuid4()
    matches.append(match)
    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=job_id,
        match_id=match.id,
        status=ScrapeTargetStatus.PENDING,
        created_at=now,
    )
    target.id = uuid.uuid4()
    target.strategy_attempt_ordinal = 0
    targets.append(target)
session.seed(*matches)
session.seed(*targets)

strategy_profile = DomainStrategyProfile(
    workspace_id=workspace_id,
    competitor_id=competitor_id,
    domain="a.example.com",
    url_pattern="a.example.com",
)
strategy_profile.id = uuid.uuid4()
session.seed(strategy_profile)


def make_method(access_method, extraction_method, priority):
    method = DomainStrategyMethod(
        workspace_id=workspace_id,
        domain_strategy_profile_id=strategy_profile.id,
        access_method=access_method,
        extraction_method=extraction_method,
        priority=priority,
        method_version=1,
        enabled=True,
        proof_state=StrategyMethodProofState.PROVEN,
        enter_on=[],
        fallback_on=[],
    )
    method.id = uuid.uuid4()
    method.retired_at = None
    method.cooldown_until = None
    method.next_canary_at = None
    session.seed(method)
    return method


# Rung 1: plain HTTP. Rung 2: a headless browser -- a DIFFERENT transport,
# so `mode_for_access_method` resolves it to BROWSER and the batch routes
# to a different Scrapyd project/spider (a different `node_class`).
http_method = make_method(AccessMethod.DIRECT_HTTP, ExtractionMethod.JSON_LD, 1)
browser_method = make_method(AccessMethod.PLAYWRIGHT_DIRECT, ExtractionMethod.JSON_LD, 2)


def intents_for_job():
    return [
        row
        for row in session._rows.get(DispatchIntent, [])
        if row.scrape_job_id == job_id
    ]


def run():
    tasks_jobs.dispatch_job(str(job_id), str(workspace_id))


def reopen(targets_to_reopen, *, clear_cursor):
    """Put targets back in flight so the planner re-plans them.

    `clear_cursor=False` models a plain redelivery (the strategy chain
    has NOT moved, so the generation must be reused); `clear_cursor=True`
    models a committed chain transition (it must advance).
    """
    for target in targets_to_reopen:
        target.dispatched_at = None
        target.status = ScrapeTargetStatus.PENDING
        if clear_cursor:
            target.current_strategy_method_id = None


def switch_to_browser_rung():
    """Commit the chain transition HTTP -> BROWSER for every open target."""
    http_method.enabled = False
    strategy_profile.preferred_method_id = browser_method.id
'''
)


_ITEM_1_CELERY_REPLAY = (
    _PLANNER
    + '''
run()
if transport.post_count != 1:
    fail("FIRST_DELIVERY_POSTS:" + str(transport.post_count))
generation = job.planning_generation
if generation != 1:
    fail("FIRST_PLAN_DID_NOT_ADVANCE_GENERATION:" + str(generation))
first_key = intents_for_job()[0].identity_key

# A duplicate at-least-once Celery delivery of the identical task.
run()
if transport.post_count != 1:
    fail("REPLAY_CAUSED_A_SECOND_POST:" + str(transport.post_count))
if job.planning_generation != generation:
    fail("REPLAY_MINTED_A_NEW_GENERATION:" + str(job.planning_generation))

# ... and a redelivery that also has to RE-PLAN the work (the targets
# were reopened) but under an unmoved strategy cursor: the persisted
# generation is reused verbatim, so the identity is rebuilt identically.
reopen(targets, clear_cursor=False)
run()
if transport.post_count != 1:
    fail("REPLAN_UNDER_SAME_CURSOR_RE_POSTED:" + str(transport.post_count))
rows = intents_for_job()
if len(rows) != 1:
    fail("REPLAY_CREATED_EXTRA_INTENTS:" + str(len(rows)))
if rows[0].identity_key != first_key:
    fail("REPLAY_CHANGED_THE_IDENTITY_KEY")
if rows[0].state != DispatchIntentState.CONFIRMED:
    fail("INTENT_NOT_CONFIRMED:" + str(rows[0].state))
print("OK")
'''
)


_ITEM_3_CROSS_MODE_HANDOFF = (
    _PLANNER
    + '''
# The HTTP attempt reaches the node and is REFUSED (`status != ok`), so
# the client releases the claim and marks the intent FAILED.
transport.behaviour = ["error", "ok"]
try:
    run()
except Exception:
    pass
http_rows = intents_for_job()
if len(http_rows) != 1:
    fail("EXPECTED_ONE_FAILED_INTENT:" + str(len(http_rows)))
if http_rows[0].state != DispatchIntentState.FAILED:
    fail("FAILED_POST_DID_NOT_RECORD_FAILED:" + str(http_rows[0].state))
if http_rows[0].mode != ScrapeProfileMode.HTTP:
    fail("FIRST_RUNG_NOT_HTTP:" + str(http_rows[0].mode))
if fake_redis.store:
    fail("FAILED_POST_LEFT_A_POISONED_KEY:" + str(sorted(fake_redis.store)))
http_key = http_rows[0].identity_key
http_generation = http_rows[0].planning_generation

# Cross-mode handoff: the chain moves to the browser rung and that
# transition is COMMITTED, so a new durable generation is minted.
switch_to_browser_rung()
reopen(targets, clear_cursor=True)
run()

rows = {row.identity_key: row for row in intents_for_job()}
if len(rows) != 2:
    fail("HANDOFF_DID_NOT_MINT_A_SECOND_IDENTITY:" + str(len(rows)))
if http_key not in rows:
    fail("THE_HTTP_INTENT_WAS_LOST")
browser_row = [row for row in rows.values() if row.identity_key != http_key][0]
if browser_row.mode != ScrapeProfileMode.BROWSER:
    fail("HANDOFF_DID_NOT_SWITCH_MODE:" + str(browser_row.mode))
if browser_row.node_class == rows[http_key].node_class:
    fail("HANDOFF_KEPT_THE_HTTP_NODE_CLASS:" + str(browser_row.node_class))
if browser_row.planning_generation <= http_generation:
    fail("HANDOFF_DID_NOT_ADVANCE_GENERATION:" + str(browser_row.planning_generation))
if browser_row.state != DispatchIntentState.CONFIRMED:
    fail("BROWSER_DISPATCH_NOT_CONFIRMED:" + str(browser_row.state))

# Both were genuinely schedulable: the failed HTTP attempt reached the
# node once, and the browser replan reached it again -- the old identity
# never suppressed the new one.
if transport.post_count != 2:
    fail("EXPECTED_TWO_POSTS_GOT:" + str(transport.post_count))
if "scrapers-browser" not in transport.calls[1]["url"]:
    fail("BROWSER_BATCH_WENT_TO_THE_HTTP_POOL:" + transport.calls[1]["url"])
print("OK")
'''
)


_ITEM_4_CHANGING_FALLBACK_SUBSET = (
    _PLANNER
    + '''
# The A1 canary shape (26 stranded targets): a full batch is dispatched,
# part of it finishes, and the SAME recovery window re-plans the
# remainder. Under the pre-B1 positional key `dispatched:{job}:0` the
# smaller subset landed on the very same key as the full batch, was
# answered with the old jobid, and never ran -- until the Redis key
# expired, at which point the key silently changed meaning again.
run()
if transport.post_count != 1:
    fail("FIRST_DISPATCH_POSTS:" + str(transport.post_count))
full = intents_for_job()[0]
full_key = full.identity_key
full_digest = full.match_ids_digest
if len(full.match_ids) != 4:
    fail("FULL_BATCH_NOT_FOUR_TARGETS:" + str(full.match_ids))

# Two of the four came back; the rest are re-planned inside the same
# recovery window, on the same rung, as a strictly smaller subset.
for target in targets[:2]:
    target.status = ScrapeTargetStatus.COMPLETED
reopen(targets[2:], clear_cursor=False)
run()

rows = intents_for_job()
if len(rows) != 2:
    fail("SUBSET_REPLAN_DID_NOT_MINT_ITS_OWN_INTENT:" + str(len(rows)))
subset = [row for row in rows if row.identity_key != full_key][0]
if sorted(subset.match_ids) != sorted(str(target.match_id) for target in targets[2:]):
    fail("SUBSET_MATCH_IDS_WRONG:" + str(subset.match_ids))
if subset.match_ids_digest == full_digest:
    fail("SUBSET_ALIASED_THE_FULL_BATCH_DIGEST:" + subset.match_ids_digest)
if subset.identity_key == full_key:
    fail("SUBSET_ALIASED_THE_FULL_BATCH_KEY:" + subset.identity_key)

# The discriminator is the WORK, not the generation: this replan ran
# under an unmoved cursor, so both intents share a planning generation
# and the keys still differ.
if subset.planning_generation != full.planning_generation:
    fail(
        "SUBSET_REPLAN_MOVED_THE_GENERATION_SO_THE_DIGEST_CLAIM_IS_UNTESTED:"
        + str(subset.planning_generation)
    )

# ... and it was actually POSTed, rather than answered with the full
# batch's jobid. That second POST is the 26 stranded targets running.
if transport.post_count != 2:
    fail("SUBSET_WAS_SUPPRESSED_BY_THE_FULL_BATCH_GUARD:" + str(transport.post_count))
posted = transport.calls[1]["data"]["match_ids"].split(",")
if sorted(posted) != sorted(str(target.match_id) for target in targets[2:]):
    fail("SECOND_POST_CARRIED_THE_WRONG_WORK:" + str(posted))

# Neither key is positional -- `batch_index` is recorded for humans only.
for row in rows:
    if row.batch_index != "0":
        fail("BATCH_INDEX_NOT_RECORDED:" + str(row.batch_index))
if full_key.rsplit(":", 1)[-1] == subset.identity_key.rsplit(":", 1)[-1]:
    fail("KEYS_DIFFER_ONLY_OUTSIDE_THE_DIGEST")
print("OK")
'''
)


_ITEM_8_NODE_FAILURE_REPLAN = (
    _PLANNER
    + '''
# The HTTP node is DEAD: the POST raises rather than answering. The
# client must release the claim and record a durable FAILED -- "tried and
# failed" has to stay distinguishable from "never tried", which is the
# distinction the pre-B1 bare `DELETE` of the Redis key destroyed.
transport.behaviour = ["boom", "ok"]
try:
    run()
except Exception:
    pass
first = intents_for_job()
if len(first) != 1:
    fail("EXPECTED_ONE_INTENT:" + str(len(first)))
if first[0].state != DispatchIntentState.FAILED:
    fail("DEAD_NODE_DID_NOT_RECORD_FAILED:" + str(first[0].state))
if not first[0].error_message:
    fail("FAILED_INTENT_HAS_NO_ERROR_MESSAGE")
if fake_redis.store:
    fail("DEAD_NODE_LEFT_A_CLAIM_BEHIND:" + str(sorted(fake_redis.store)))
dead_key = first[0].identity_key
dead_node_class = first[0].node_class

# Replanned onto a different node class, committed as a new generation.
switch_to_browser_rung()
reopen(targets, clear_cursor=True)
run()

rows = {row.identity_key: row for row in intents_for_job()}
if len(rows) != 2:
    fail("REPLAN_DID_NOT_MINT_A_NEW_IDENTITY:" + str(len(rows)))
if dead_key not in rows:
    fail("THE_FAILED_INTENT_WAS_DELETED_NOT_KEPT")
if rows[dead_key].state != DispatchIntentState.FAILED:
    fail("THE_FAILED_INTENT_WAS_OVERWRITTEN:" + str(rows[dead_key].state))
replan = [row for row in rows.values() if row.identity_key != dead_key][0]
if replan.node_class == dead_node_class:
    fail("REPLAN_STAYED_ON_THE_DEAD_NODE_CLASS:" + str(replan.node_class))
if replan.planning_generation <= rows[dead_key].planning_generation:
    fail("REPLAN_DID_NOT_ADVANCE_GENERATION:" + str(replan.planning_generation))
if replan.state != DispatchIntentState.CONFIRMED:
    fail("REPLAN_NOT_CONFIRMED:" + str(replan.state))
if transport.post_count != 2:
    fail("EXPECTED_TWO_POSTS_GOT:" + str(transport.post_count))

# `node_class` really is what discriminates: hold every other component
# fixed and only the pool changes, and the key still moves.
from app_shared.scrapyd.identity import build_dispatch_identity

common = dict(
    scrape_job_id=str(job_id),
    planning_generation=7,
    strategy_method="css/v1",
    domain="a.example.com",
    mode="HTTP",
    match_ids=[str(m.id) for m in matches],
)
if build_dispatch_identity(node_class="price_monitor:s", **common).key == (
    build_dispatch_identity(node_class="price_monitor_browser:s", **common).key
):
    fail("NODE_CLASS_IS_NOT_PART_OF_THE_IDENTITY")
print("OK")
'''
)


# ===========================================================================
# Group B -- the REAL `ScrapydDispatchClient` + `DispatchIntentStore`.
#
# Items 5, 6, 7, 9, 10: claims about the reconcile -> claim -> POST ->
# commit -> release ordering and about recovery, all of which live below
# the planner.
# ===========================================================================

_CLIENT = (
    _DOUBLES
    + '''
import requests

from _jobs_fake_session import FakeOrmSession
from app_shared.enums import (
    DispatchIntentState,
    ScrapeErrorCode,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeProfileMode,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.jobs.dispatch_intents import DispatchIntentStore
from app_shared.models.dispatch import DispatchIntent
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.scrapyd.client import (
    DispatchIntegrityError,
    ScrapydDispatchClient,
    ScrapydDispatchError,
    StaleCancellationGenerationError,
)
from app_shared.scrapyd.identity import (
    PENDING_SENTINEL,
    build_dispatch_identity,
    get_committed_dispatch,
)
from app_shared.scrapyd.reconcile import (
    InflightVerdict,
    identity_from_intent,
    reconcile_inflight,
)

now = datetime.now(timezone.utc)
workspace_id = uuid.uuid4()
job_id = uuid.uuid4()
session = FakeOrmSession()
fake_redis = FakeRedis()
transport = RecordingTransport()

job = ScrapeJob(
    workspace_id=workspace_id,
    type=ScrapeJobType.MANUAL,
    scope=ScrapeScope.MATCH,
    status=ScrapeJobStatus.RUNNING,
    total_targets=2,
    source=ScrapeJobSource.API,
    created_at=now,
)
job.id = job_id
job.cancellation_generation = 0
job.planning_generation = 1
session.seed(job)

match_a = uuid.uuid4()
match_b = uuid.uuid4()

target_a = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=job_id,
    match_id=match_a,
    status=ScrapeTargetStatus.PENDING,
    created_at=now,
)
target_a.id = uuid.uuid4()
target_b = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=job_id,
    match_id=match_b,
    status=ScrapeTargetStatus.PENDING,
    created_at=now,
)
target_b.id = uuid.uuid4()
session.seed(target_a, target_b)

NODE_CLASS = "price_monitor:generic_price_spider"


def identity_for(match_ids, *, generation=1):
    return build_dispatch_identity(
        scrape_job_id=str(job_id),
        planning_generation=generation,
        strategy_method="DIRECT_HTTP/JSON_LD/v1",
        domain="a.example.com",
        mode=ScrapeProfileMode.HTTP,
        node_class=NODE_CLASS,
        match_ids=match_ids,
    )


def make_store(*, authorized_generation=0):
    return DispatchIntentStore(
        session,
        workspace_id=workspace_id,
        scrape_job_id=job_id,
        authorized_cancellation_generation=authorized_generation,
    )


def make_client(settings=None, intents=None, lister=None):
    http_session = requests.Session()
    http_session.post = transport.post
    return ScrapydDispatchClient(
        settings=settings if settings is not None else make_settings(),
        redis_client=fake_redis,
        session=http_session,
        intents=intents,
        lister=lister,
    )


def schedule(identity, *, client=None, match_ids=None, node_url=None):
    client = client if client is not None else make_client(intents=make_store())
    return client.schedule(
        "price_monitor",
        "generic_price_spider",
        workspace_id=str(workspace_id),
        scrape_job_id=str(job_id),
        match_ids=list(match_ids if match_ids is not None else [match_a, match_b]),
        mode="HTTP",
        batch_index=0,
        node_url=node_url,
        identity=identity,
    )


class FakeLister:
    """A scripted `listjobs.json`. `None` for a node models unreachable."""

    def __init__(self, payloads=None):
        # {node_url: payload-or-None}; a missing node answers "empty".
        self.payloads = payloads or {}
        self.calls = []
        self.default = {"pending": [], "running": [], "finished": []}

    def list_jobs(self, node_url, project=None):
        self.calls.append((node_url, project))
        if node_url in self.payloads:
            return self.payloads[node_url]
        return self.default
'''
)


_ITEM_5_SENTINEL_EXPIRY = (
    _CLIENT
    + '''
identity = identity_for([match_a, match_b])
store = make_store()

# --- 5a: an aged-out guard over a CONFIRMED intent -----------------------
# The TTL says nothing about whether the POST happened; only the durable
# row does. Step 0 must answer from `dispatch_intents` and HEAL the
# guard, never re-POST.
store.plan(identity, match_ids=[match_a, match_b])
schedule(identity, client=make_client(intents=store))
if transport.post_count != 1:
    fail("FIRST_SCHEDULE_POSTS:" + str(transport.post_count))
committed_jobid = store.reconcile(identity).jobid

fake_redis.expire(identity.key)          # the guard's TTL elapses
if fake_redis.get(identity.key) is not None:
    fail("GUARD_DID_NOT_EXPIRE")

again = schedule(identity, client=make_client(intents=make_store()))
if transport.post_count != 1:
    fail("TTL_ELAPSE_ALONE_AUTHORIZED_A_RE_POST:" + str(transport.post_count))
if again != committed_jobid:
    fail("EXPIRED_GUARD_RETURNED_A_DIFFERENT_JOBID:" + str(again))
healed = fake_redis.get(identity.key)
if healed is None or PENDING_SENTINEL in str(healed):
    fail("GUARD_WAS_NOT_HEALED:" + str(healed))
if str(committed_jobid) not in str(healed):
    fail("HEALED_GUARD_LOST_THE_JOBID:" + str(healed))

# --- 5b: a value that proves nothing, replaced only under compare-and-set -
# A pre-B1 bare jobid (or a corrupted value) under the key is not an
# answer about THIS identity. With no durable commitment behind it the
# client re-claims -- but only if the value has not moved.
other = identity_for([match_a], generation=1)
fake_redis.store[other.key] = "legacy-bare-jobid"
before = transport.post_count
schedule(other, match_ids=[match_a], client=make_client(intents=make_store()))
if transport.post_count != before + 1:
    fail("CAS_REPLACE_DID_NOT_PROCEED:" + str(transport.post_count))

# ... and when it DOES move underneath us, the loser backs off instead of
# racing: no second POST.
third = identity_for([match_b], generation=1)


class MovingRedis(FakeRedis):
    """`get` observes a different value each time -- the CAS must fail."""

    def __init__(self):
        super().__init__()
        self.reads = 0

    def get(self, name):
        if name == third.key:
            self.reads += 1
            return "value-" + str(self.reads)
        return super().get(name)


moving = MovingRedis()
moving.store[third.key] = "value-0"
racing_http = requests.Session()
racing_http.post = transport.post
racing = ScrapydDispatchClient(
    settings=make_settings(),
    redis_client=moving,
    session=racing_http,
    intents=make_store(),
)
before = transport.post_count
try:
    racing.schedule(
        "price_monitor",
        "generic_price_spider",
        workspace_id=str(workspace_id),
        scrape_job_id=str(job_id),
        match_ids=[match_b],
        mode="HTTP",
        batch_index=0,
        identity=third,
    )
except ScrapydDispatchError as exc:
    if "already in progress" not in str(exc):
        fail("WRONG_ERROR_ON_LOST_CAS:" + str(exc))
else:
    fail("LOST_CAS_DID_NOT_BACK_OFF")
if transport.post_count != before:
    fail("LOST_CAS_STILL_POSTED:" + str(transport.post_count))

# --- 5c: an aged-out SENTINEL over a POSTED intent (EPA B3b wiring) ------
# The genuinely ambiguous case: a POST may be in flight right now. TTL
# elapse must not be read as authorization; the durable POSTED row is the
# evidence, and `schedule()` ITSELF must now invoke `reconcile_inflight`
# before it is allowed to claim a slot and re-POST -- never a blind re-POST
# just because the sentinel is gone.

# 5c-i: AMBIGUOUS -- `schedule()` refuses to guess. No claim, no POST, and
# the intent is left exactly as POSTED as it was found.
ambiguous_identity = identity_for([match_a, match_b], generation=2)
ambiguous_store = make_store()
ambiguous_store.plan(ambiguous_identity, match_ids=[match_a, match_b])
ambiguous_intent_id = ambiguous_store.record_post(ambiguous_identity)  # POSTED, no jobid yet
fake_redis.store[ambiguous_identity.key] = PENDING_SENTINEL
fake_redis.expire(ambiguous_identity.key)              # the 120s sentinel ages out

ambiguous_row = [
    r for r in session._rows[DispatchIntent] if str(r.id) == ambiguous_intent_id
][0]
if ambiguous_row.state != DispatchIntentState.POSTED:
    fail("RECORD_POST_DID_NOT_PERSIST_POSTED:" + str(ambiguous_row.state))
if ambiguous_row.posted_at is None:
    fail("POSTED_INTENT_HAS_NO_POSTED_AT")
if get_committed_dispatch((fake_redis, make_store()), ambiguous_identity) is not None:
    fail("A_POSTED_INTENT_WAS_TREATED_AS_A_COMMITMENT")

before = transport.post_count
try:
    schedule(
        ambiguous_identity,
        client=make_client(
            intents=make_store(),
            lister=FakeLister({"http://scrapers-1:6800": None}),  # a node is down
        ),
    )
except ScrapydDispatchError as exc:
    if "refusing to guess" not in str(exc):
        fail("WRONG_ERROR_ON_AMBIGUOUS_RECONCILIATION:" + str(exc))
else:
    fail("SCHEDULE_BLINDLY_RE_POSTED_OVER_AN_AMBIGUOUS_RECONCILIATION")
if ambiguous_row.state != DispatchIntentState.POSTED:
    fail("AMBIGUOUS_MUTATED_THE_INTENT:" + str(ambiguous_row.state))
if transport.post_count != before:
    fail("SCHEDULE_POSTED_ON_AMBIGUOUS_RECONCILIATION:" + str(transport.post_count))

# `reconcile_inflight` agrees when called directly, the way a recovery
# sweep would call it.
outcome = reconcile_inflight(
    session,
    ambiguous_intent_id,
    workspace_id=workspace_id,
    settings=make_settings(),
    lister=FakeLister({"http://scrapers-1:6800": None}),
)
if outcome.verdict != InflightVerdict.AMBIGUOUS:
    fail("UNREACHABLE_NODE_WAS_NOT_AMBIGUOUS:" + str(outcome.verdict))
if outcome.may_repost:
    fail("AMBIGUOUS_AUTHORIZED_A_RE_POST")
if transport.post_count != before:
    fail("RECONCILIATION_POSTED:" + str(transport.post_count))

# 5c-ii: the node reports the job (listjobs.json pending-queue args
# correlation, flag off) -- `schedule()` ADOPTS it and issues ZERO posts.
adopt_identity = identity_for([match_a, match_b], generation=3)
adopt_store = make_store()
adopt_store.plan(adopt_identity, match_ids=[match_a, match_b])
adopt_intent_id = adopt_store.record_post(adopt_identity)
fake_redis.store[adopt_identity.key] = PENDING_SENTINEL
fake_redis.expire(adopt_identity.key)
adopt_row = [r for r in session._rows[DispatchIntent] if str(r.id) == adopt_intent_id][0]

adopt_lister = FakeLister(
    {
        "http://scrapers-1:6800": {
            "pending": [
                {
                    "id": "node-minted-5c-ii",
                    "project": "price_monitor",
                    "spider": "generic_price_spider",
                    "args": {
                        "scrape_job_id": str(job_id),
                        "workspace_id": str(workspace_id),
                        "match_ids": str(match_a) + "," + str(match_b),
                        "mode": "HTTP",
                    },
                }
            ],
            "running": [],
            "finished": [],
        }
    }
)
before = transport.post_count
adopted_jobid = schedule(
    adopt_identity,
    client=make_client(intents=make_store(), lister=adopt_lister),
)
if adopted_jobid != "node-minted-5c-ii":
    fail("SCHEDULE_DID_NOT_ADOPT_THE_FOUND_RUN:" + str(adopted_jobid))
if transport.post_count != before:
    fail("SCHEDULE_POSTED_AFTER_ADOPTING_A_FOUND_RUN:" + str(transport.post_count))
if adopt_row.state != DispatchIntentState.CONFIRMED:
    fail("ADOPTED_INTENT_NOT_CONFIRMED:" + str(adopt_row.state))
if adopt_row.scrapyd_job_id != "node-minted-5c-ii":
    fail("ADOPTED_INTENT_HAS_THE_WRONG_JOBID:" + str(adopt_row.scrapyd_job_id))
healed = fake_redis.get(adopt_identity.key)
if healed is None or "node-minted-5c-ii" not in str(healed):
    fail("SCHEDULE_DID_NOT_HEAL_THE_GUARD_AFTER_ADOPTING:" + str(healed))

# 5c-iii: every node answers and none knows the job -- `reconcile_inflight`
# rolls the intent back to PLANNED, and `schedule()`'s ORDINARY path (not
# the reconciler) issues the one and only POST -- exactly like a crash
# BEFORE the POST (item 6).
absent_identity = identity_for([match_a, match_b], generation=4)
absent_store = make_store()
absent_store.plan(absent_identity, match_ids=[match_a, match_b])
absent_intent_id = absent_store.record_post(absent_identity)
fake_redis.store[absent_identity.key] = PENDING_SENTINEL
fake_redis.expire(absent_identity.key)
absent_row = [r for r in session._rows[DispatchIntent] if str(r.id) == absent_intent_id][0]

before = transport.post_count
absent_jobid = schedule(
    absent_identity,
    client=make_client(intents=make_store(), lister=FakeLister()),  # every node: empty
)
if transport.post_count != before + 1:
    fail("SCHEDULE_DID_NOT_POST_EXACTLY_ONCE_AFTER_ABSENCE:" + str(transport.post_count))
if absent_row.state != DispatchIntentState.CONFIRMED:
    fail("ABSENCE_RECOVERY_DID_NOT_CONFIRM:" + str(absent_row.state))
if absent_row.scrapyd_job_id != absent_jobid:
    fail("ABSENCE_RECOVERY_JOBID_MISMATCH:" + str(absent_row.scrapyd_job_id))
print("OK")
'''
)


_ITEM_6_CRASH_BEFORE_POST = (
    _CLIENT
    + '''
# The worker planned the batch, won the Redis claim, and died before the
# network call. Durable state: PLANNED. Redis: a sentinel that will age
# out on its own.
identity = identity_for([match_a, match_b])
store = make_store()
planned = store.plan(identity, match_ids=[match_a, match_b])
fake_redis.set(identity.key, PENDING_SENTINEL, nx=True, ex=120)

if planned.state != DispatchIntentState.PLANNED:
    fail("PLAN_DID_NOT_RECORD_PLANNED:" + str(planned.state))
if planned.posted_at is not None:
    fail("PLANNED_INTENT_HAS_A_POSTED_AT")
if transport.post_count != 0:
    fail("A_CRASHED_WORKER_SOMEHOW_POSTED")

# Recovery asks the reconciler first. A PLANNED intent was never in
# flight, so there is nothing to correlate and the retry is cleared.
outcome = reconcile_inflight(
    session,
    planned.id,
    workspace_id=workspace_id,
    settings=make_settings(),
    lister=FakeLister(),
)
if outcome.verdict != InflightVerdict.NOT_INFLIGHT:
    fail("PLANNED_INTENT_WAS_NOT_NOT_INFLIGHT:" + str(outcome.verdict))
if not outcome.may_repost:
    fail("PLANNED_INTENT_WAS_NOT_CLEARED_TO_PROCEED")

# The sentinel expires; the retry now claims cleanly and proceeds.
fake_redis.expire(identity.key)
jobid = schedule(identity, client=make_client(intents=make_store()))
if transport.post_count != 1:
    fail("RETRY_AFTER_CRASH_DID_NOT_POST_EXACTLY_ONCE:" + str(transport.post_count))
rows = [r for r in session._rows[DispatchIntent] if r.scrape_job_id == job_id]
if len(rows) != 1:
    fail("RETRY_CREATED_A_SECOND_INTENT:" + str(len(rows)))
if rows[0].id != planned.id:
    fail("RETRY_DID_NOT_REUSE_THE_PLANNED_ROW")
if rows[0].state != DispatchIntentState.CONFIRMED:
    fail("RETRY_DID_NOT_CONFIRM:" + str(rows[0].state))
if rows[0].scrapyd_job_id != jobid:
    fail("CONFIRMED_JOBID_MISMATCH:" + str(rows[0].scrapyd_job_id))

# And a redelivery after that is a no-op, as ever.
schedule(identity, client=make_client(intents=make_store()))
if transport.post_count != 1:
    fail("POST_CRASH_RECOVERY_LOST_IDEMPOTENCY:" + str(transport.post_count))
print("OK")
'''
)


# Item 7 is parametrized over the two Step-0 mechanisms plus the failure
# modes each one has.
_ITEM_7_PREAMBLE = (
    _CLIENT
    + '''
def crashed_after_post(match_ids=None, generation=1):
    """Leave exactly what a worker killed mid-POST leaves behind."""
    identity = identity_for(
        match_ids if match_ids is not None else [match_a, match_b],
        generation=generation,
    )
    store = make_store()
    store.plan(identity, match_ids=match_ids or [match_a, match_b])
    intent_id = store.record_post(identity)
    fake_redis.expire(identity.key)   # the sentinel ages out
    row = [r for r in session._rows[DispatchIntent] if str(r.id) == intent_id][0]
    return identity, row


scenario = sys.argv[1] if len(sys.argv) > 1 else ""
'''
)

_ITEM_7_SCENARIOS = (
    _ITEM_7_PREAMBLE
    + '''
if scenario == "deterministic_jobid_finds_the_orphan":
    # SCRAPYD_DETERMINISTIC_JOBID on: the remote jobid IS the intent id,
    # so the orphan is a lookup, in ANY listjobs bucket.
    identity, row = crashed_after_post()
    settings = make_settings(SCRAPYD_DETERMINISTIC_JOBID=True)
    lister = FakeLister(
        {
            "http://scrapers-1:6800": {
                "pending": [],
                # `running` carries no spider args -- only the id, which
                # is exactly why the deterministic id is what makes this
                # bucket searchable at all.
                "running": [
                    {"id": str(row.id), "project": "price_monitor", "spider": "s"}
                ],
                "finished": [],
            }
        }
    )
    outcome = reconcile_inflight(
        session,
        row.id,
        workspace_id=workspace_id,
        settings=settings,
        lister=lister,
        redis=fake_redis,
    )
    if outcome.verdict != InflightVerdict.CONFIRMED:
        fail("ORPHAN_NOT_ADOPTED:" + str(outcome.verdict) + "/" + outcome.detail)
    if outcome.mechanism != "deterministic_jobid":
        fail("WRONG_MECHANISM:" + outcome.mechanism)
    if outcome.scrapyd_job_id != str(row.id):
        fail("ADOPTED_THE_WRONG_JOBID:" + str(outcome.scrapyd_job_id))
    if row.state != DispatchIntentState.CONFIRMED:
        fail("INTENT_NOT_CONFIRMED:" + str(row.state))
    if outcome.may_repost:
        fail("CONFIRMED_AUTHORIZED_A_RE_POST")
    if transport.post_count != 0:
        fail("RECONCILIATION_POSTED:" + str(transport.post_count))
    # The guard is healed, so the next delivery is answered from Redis.
    committed = get_committed_dispatch(fake_redis, identity)
    if committed is None or committed.jobid != str(row.id):
        fail("GUARD_NOT_HEALED_AFTER_ADOPTION")
    # ... and the ordinary dispatch path now no-ops rather than re-POSTing.
    if schedule(identity, client=make_client(intents=make_store())) != str(row.id):
        fail("POST_RECONCILE_SCHEDULE_DID_NOT_NO_OP")
    if transport.post_count != 0:
        fail("POST_RECONCILE_SCHEDULE_RE_POSTED:" + str(transport.post_count))

elif scenario == "deterministic_jobid_proves_absence":
    # Flag on, every node answered, nobody knows the job -> the POST
    # never landed. The intent rolls back to PLANNED and the ordinary
    # path -- not the reconciler -- issues the one and only POST.
    identity, row = crashed_after_post()
    outcome = reconcile_inflight(
        session,
        row.id,
        workspace_id=workspace_id,
        settings=make_settings(SCRAPYD_DETERMINISTIC_JOBID=True),
        lister=FakeLister(),
    )
    if outcome.verdict != InflightVerdict.ABSENT:
        fail("ABSENCE_NOT_ESTABLISHED:" + str(outcome.verdict))
    if not outcome.may_repost:
        fail("ABSENCE_DID_NOT_CLEAR_THE_RETRY")
    if row.state != DispatchIntentState.PLANNED:
        fail("ABSENT_INTENT_NOT_ROLLED_BACK:" + str(row.state))
    if row.posted_at is not None:
        fail("ROLLED_BACK_INTENT_KEPT_POSTED_AT")
    if transport.post_count != 0:
        fail("RECONCILIATION_POSTED:" + str(transport.post_count))
    schedule(identity, client=make_client(intents=make_store()))
    if transport.post_count != 1:
        fail("RECOVERED_DISPATCH_DID_NOT_POST_ONCE:" + str(transport.post_count))

elif scenario == "listjobs_args_correlate_a_pending_orphan":
    # Flag OFF (today's default). Scrapyd 1.6.0 exposes spider `args` on
    # PENDING entries, so a job still in the queue is correlatable by
    # (scrape_job_id, match_ids) and is adopted with the NODE's jobid.
    identity, row = crashed_after_post()
    lister = FakeLister(
        {
            "http://scrapers-2:6800": {
                "pending": [
                    {
                        "id": "node-minted-abc123",
                        "project": "price_monitor",
                        "spider": "generic_price_spider",
                        "version": None,
                        "settings": {},
                        "args": {
                            "scrape_job_id": str(job_id),
                            "workspace_id": str(workspace_id),
                            "match_ids": str(match_a) + "," + str(match_b),
                            "mode": "HTTP",
                        },
                    }
                ],
                "running": [],
                "finished": [],
            }
        }
    )
    outcome = reconcile_inflight(
        session,
        row.id,
        workspace_id=workspace_id,
        settings=make_settings(SCRAPYD_DETERMINISTIC_JOBID=False),
        lister=lister,
    )
    if outcome.verdict != InflightVerdict.CONFIRMED:
        fail("PENDING_ORPHAN_NOT_ADOPTED:" + str(outcome.verdict))
    if outcome.mechanism != "listjobs_args":
        fail("WRONG_MECHANISM:" + outcome.mechanism)
    if outcome.scrapyd_job_id != "node-minted-abc123":
        fail("ADOPTED_THE_WRONG_JOBID:" + str(outcome.scrapyd_job_id))
    if row.scrapyd_job_id != "node-minted-abc123":
        fail("INTENT_DID_NOT_RECORD_THE_NODE_JOBID")
    if transport.post_count != 0:
        fail("RECONCILIATION_POSTED:" + str(transport.post_count))
    # The whole pool is asked -- the intent records a node CLASS, never a
    # node URL, so the run may be on any node in it.
    if len(lister.calls) < 2:
        fail("ONLY_ONE_NODE_WAS_ASKED:" + str(lister.calls))

elif scenario == "listjobs_args_cannot_see_a_running_orphan":
    # THE Step-0 limitation, made executable: with the flag off, a job
    # that has left the pending queue carries no args in listjobs.json,
    # so it cannot be correlated. AMBIGUOUS -- never a blind re-POST.
    identity, row = crashed_after_post()
    lister = FakeLister(
        {
            "http://scrapers-1:6800": {
                "pending": [],
                "running": [
                    {
                        "id": "node-minted-running",
                        "project": "price_monitor",
                        "spider": "generic_price_spider",
                        "pid": 42,
                        "start_time": "2026-08-25 09:00:00",
                    }
                ],
                "finished": [],
            }
        }
    )
    outcome = reconcile_inflight(
        session,
        row.id,
        workspace_id=workspace_id,
        settings=make_settings(SCRAPYD_DETERMINISTIC_JOBID=False),
        lister=lister,
    )
    if outcome.verdict != InflightVerdict.AMBIGUOUS:
        fail("RUNNING_ORPHAN_WAS_NOT_AMBIGUOUS:" + str(outcome.verdict))
    if outcome.may_repost:
        fail("AMBIGUOUS_AUTHORIZED_A_RE_POST")
    if row.state != DispatchIntentState.POSTED:
        fail("AMBIGUOUS_MUTATED_THE_INTENT:" + str(row.state))
    if "SCRAPYD_DETERMINISTIC_JOBID" not in outcome.detail:
        fail("AMBIGUOUS_DID_NOT_EXPLAIN_THE_REMEDY:" + outcome.detail)
    if transport.post_count != 0:
        fail("RECONCILIATION_POSTED:" + str(transport.post_count))

    # The SAME orphan, with the flag on, IS resolvable -- the two
    # mechanisms are not interchangeable, and this is the difference.
    lister_on = FakeLister(
        {
            "http://scrapers-1:6800": {
                "pending": [],
                "running": [{"id": str(row.id), "project": "price_monitor"}],
                "finished": [],
            }
        }
    )
    outcome_on = reconcile_inflight(
        session,
        row.id,
        workspace_id=workspace_id,
        settings=make_settings(SCRAPYD_DETERMINISTIC_JOBID=True),
        lister=lister_on,
    )
    if outcome_on.verdict != InflightVerdict.CONFIRMED:
        fail("FLAG_ON_DID_NOT_RESOLVE_THE_SAME_ORPHAN:" + str(outcome_on.verdict))

elif scenario == "browser_intents_are_reconciled_on_the_browser_pool":
    # `node_class` selects the pool to search. A browser batch must not
    # be looked for on the HTTP nodes (it would read as absent there).
    identity = build_dispatch_identity(
        scrape_job_id=str(job_id),
        planning_generation=3,
        strategy_method="PLAYWRIGHT_DIRECT/JSON_LD/v1",
        domain="a.example.com",
        mode=ScrapeProfileMode.BROWSER,
        node_class="price_monitor_browser:generic_browser_price_spider",
        match_ids=[match_a],
    )
    store = make_store()
    store.plan(identity, match_ids=[match_a])
    intent_id = store.record_post(identity)
    row = [r for r in session._rows[DispatchIntent] if str(r.id) == intent_id][0]
    lister = FakeLister()
    reconcile_inflight(
        session,
        row.id,
        workspace_id=workspace_id,
        settings=make_settings(SCRAPYD_DETERMINISTIC_JOBID=True),
        lister=lister,
    )
    asked = {node for node, _project in lister.calls}
    if asked != {"http://scrapers-browser-1:6800"}:
        fail("BROWSER_INTENT_SEARCHED_THE_WRONG_POOL:" + str(asked))
    projects = {project for _node, project in lister.calls}
    if projects != {"price_monitor_browser"}:
        fail("WRONG_PROJECT_FILTER:" + str(projects))

elif scenario == "a_row_that_cannot_reproduce_its_key_is_refused":
    # Reconciliation names the work from the row's own columns. If those
    # no longer hash to the stored key the row was tampered with, and
    # reconciling against a name we cannot reproduce would be worse than
    # refusing.
    identity, row = crashed_after_post()
    if identity_from_intent(row).key != row.identity_key:
        fail("A_HEALTHY_ROW_DID_NOT_ROUND_TRIP")
    row.domain = "somewhere-else.example.com"
    try:
        reconcile_inflight(
            session,
            row.id,
            workspace_id=workspace_id,
            settings=make_settings(),
            lister=FakeLister(),
        )
    except ValueError as exc:
        if "identity key" not in str(exc):
            fail("WRONG_ERROR:" + str(exc))
    else:
        fail("TAMPERED_ROW_WAS_RECONCILED_ANYWAY")

elif scenario == "another_workspace_cannot_reconcile_this_intent":
    identity, row = crashed_after_post()
    try:
        reconcile_inflight(
            session,
            row.id,
            workspace_id=uuid.uuid4(),
            settings=make_settings(),
            lister=FakeLister(),
        )
    except LookupError:
        pass
    else:
        fail("CROSS_WORKSPACE_RECONCILIATION_SUCCEEDED")
    if row.state != DispatchIntentState.POSTED:
        fail("CROSS_WORKSPACE_CALL_MUTATED_THE_ROW:" + str(row.state))

else:
    fail("UNKNOWN_SCENARIO:" + scenario)

print("OK")
'''
)


_ITEM_9_RATE_LIMIT_DEFERRAL = (
    _CLIENT
    + '''
from app.workers.tasks_dispatch import DispatchedBatch, stamp_targets_dispatched

# The full batch is dispatched and stamped.
full = identity_for([match_a, match_b])
store = make_store()
store.plan(full, match_ids=[match_a, match_b])
schedule(full, client=make_client(intents=store))
stamp_targets_dispatched(
    session,
    fake_redis,
    batch=DispatchedBatch(
        workspace_id=workspace_id,
        scrape_job_id=job_id,
        identity=full,
        targets=[target_a, target_b],
    ),
)
if target_b.dispatched_at is None:
    fail("FULL_BATCH_WAS_NOT_STAMPED")

# `target_b` is handed back by the requeue cap (SPEC-11): DEFERRED, with
# a rate-limit error code and no dispatch stamp.
target_b.status = ScrapeTargetStatus.DEFERRED
target_b.error_code = ScrapeErrorCode.RATE_LIMITED
target_b.dispatched_at = None
target_b.dispatch_intent_id = None

# A recovery window overlaps and re-plans JUST that target. The full
# batch's committed guard/intent describes different work, so it is not
# proof about this batch and stamping must refuse.
subset = identity_for([match_b])
subset_batch = DispatchedBatch(
    workspace_id=workspace_id,
    scrape_job_id=job_id,
    identity=subset,
    targets=[target_b],
)
posts_before = transport.post_count
try:
    stamp_targets_dispatched(session, fake_redis, batch=subset_batch)
except DispatchIntegrityError as exc:
    if subset.key not in str(exc):
        fail("ERROR_DID_NOT_NAME_THE_IDENTITY:" + str(exc))
else:
    fail("DEFERRED_TARGET_WAS_RESTAMPED_WITHOUT_A_GUARD")

if target_b.dispatched_at is not None:
    fail("REFUSED_STAMP_STILL_WROTE_DISPATCHED_AT")
if target_b.status != ScrapeTargetStatus.DEFERRED:
    fail("REFUSED_STAMP_MUTATED_THE_STATUS:" + str(target_b.status))
if transport.post_count != posts_before:
    fail("STAMPING_POSTED_SOMETHING")

# Leaving it unstamped is the point: `dispatched_at IS NULL` is exactly
# what `redispatch_pending_jobs` looks for, so the target is still
# reachable rather than silently lost.
if target_a.dispatched_at is None:
    fail("THE_OTHER_TARGETS_STAMP_WAS_ROLLED_BACK")

# Once the subset is genuinely dispatched, the same call succeeds and the
# DEFERRED handback flips back to an ordinary in-flight PENDING row.
subset_store = make_store()
subset_store.plan(subset, match_ids=[match_b])
schedule(subset, client=make_client(intents=subset_store), match_ids=[match_b])
if transport.post_count != posts_before + 1:
    fail("SUBSET_WAS_SUPPRESSED_BY_THE_FULL_BATCH:" + str(transport.post_count))
stamp_targets_dispatched(session, fake_redis, batch=subset_batch)
if target_b.status != ScrapeTargetStatus.PENDING:
    fail("DEFERRED_DID_NOT_FLIP_BACK_TO_PENDING:" + str(target_b.status))
if target_b.error_code is not None:
    fail("RATE_LIMIT_ERROR_CODE_NOT_CLEARED")
if target_b.dispatched_at is None:
    fail("SUBSET_WAS_NOT_STAMPED_AFTER_A_REAL_DISPATCH")
subset_row = subset_store.reconcile(subset)
if str(target_b.dispatch_intent_id) != str(subset_row.intent_id):
    fail("STAMPED_AGAINST_THE_WRONG_INTENT")
print("OK")
'''
)


_ITEM_10_CANCELLATION_FENCE = (
    _CLIENT
    + '''
# A2's fence. The intent records the `cancellation_generation` its work
# was authorized under; the job moves past it when a human cancels.
identity = identity_for([match_a, match_b])
store = make_store(authorized_generation=0)
planned = store.plan(identity, match_ids=[match_a, match_b])
if planned.cancellation_generation_at_creation != 0:
    fail("FENCE_NOT_CAPTURED_AT_PLAN_TIME")

job.cancellation_generation = 1          # ... the cancellation lands

try:
    schedule(identity, client=make_client(intents=make_store()))
except StaleCancellationGenerationError as exc:
    if "generation" not in str(exc):
        fail("WRONG_ERROR_TEXT:" + str(exc))
else:
    fail("STALE_GENERATION_WAS_DISPATCHED")

# Refused BEFORE anything was claimed or POSTed -- the whole point is to
# spend nothing, not to discard results afterwards.
if transport.post_count != 0:
    fail("STALE_DISPATCH_REACHED_THE_NODE:" + str(transport.post_count))
if identity.key in fake_redis.store:
    fail("STALE_DISPATCH_CLAIMED_A_SLOT")
if planned.state != DispatchIntentState.PLANNED:
    fail("STALE_DISPATCH_ADVANCED_THE_INTENT:" + str(planned.state))

# A CANCELLED job is refused even when the generations happen to agree
# (the belt-and-braces half of the same fence).
job.cancellation_generation = 0
job.status = ScrapeJobStatus.CANCELLED
try:
    schedule(identity, client=make_client(intents=make_store()))
except StaleCancellationGenerationError:
    pass
else:
    fail("CANCELLED_JOB_WAS_DISPATCHED")
if transport.post_count != 0:
    fail("CANCELLED_JOB_REACHED_THE_NODE:" + str(transport.post_count))

# Work authorized UNDER the new generation is fine -- the fence stops
# stale work, not all work.
job.status = ScrapeJobStatus.RUNNING
job.cancellation_generation = 1
fresh = identity_for([match_a, match_b], generation=9)
fresh_store = make_store(authorized_generation=1)
fresh_store.plan(fresh, match_ids=[match_a, match_b])
schedule(fresh, client=make_client(intents=fresh_store))
if transport.post_count != 1:
    fail("FRESHLY_AUTHORIZED_WORK_WAS_REFUSED:" + str(transport.post_count))

# And a stale intent stays refused even after reconciliation is asked --
# `reconcile` is where the fence lives, so it raises rather than
# answering "not committed" and inviting a POST.
try:
    make_store(authorized_generation=0).reconcile(identity)
except StaleCancellationGenerationError:
    pass
else:
    fail("RECONCILE_DID_NOT_ENFORCE_THE_FENCE")
print("OK")
'''
)


# ===========================================================================
# Group C -- item 2, against a REAL Postgres.
# ===========================================================================

_ITEM_2_CONCURRENT_DISPATCH = (
    _DOUBLES
    + '''
import os
import threading

import requests
from sqlalchemy import ForeignKeyConstraint, MetaData, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_shared.enums import (
    DispatchIntentState,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeProfileMode,
    ScrapeScope,
)
from app_shared.jobs.dispatch_intents import DispatchIntentStore
from app_shared.models.dispatch import DispatchIntent
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.scrapyd.client import ScrapydDispatchClient, ScrapydDispatchError
from app_shared.scrapyd.identity import build_dispatch_identity

dsn = os.environ["B3_SCRATCH_DATABASE_URL"]

# Only the three tables this scenario touches, built from the ORM models
# with foreign keys stripped -- the point is the UNIQUE constraint on
# `identity_key` and real transaction isolation, not the whole schema.
meta = MetaData()
for model in (ScrapeJob, ScrapeJobTarget, DispatchIntent):
    table = model.__table__.to_metadata(meta)
    for constraint in list(table.constraints):
        if isinstance(constraint, ForeignKeyConstraint):
            table.constraints.discard(constraint)
    for column in table.columns:
        for fk in list(column.foreign_keys):
            column.foreign_keys.discard(fk)
            table.foreign_keys.discard(fk)

engine = create_engine(dsn, pool_size=6, max_overflow=6)
meta.drop_all(engine)
meta.create_all(engine)

workspace_id = uuid.uuid4()
job_id = uuid.uuid4()
match_a = uuid.uuid4()
match_b = uuid.uuid4()
now = datetime.now(timezone.utc)

with Session(engine) as setup:
    job = ScrapeJob(
        workspace_id=workspace_id,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.MATCH,
        status=ScrapeJobStatus.RUNNING,
        total_targets=2,
        source=ScrapeJobSource.API,
        created_at=now,
    )
    job.id = job_id
    job.cancellation_generation = 0
    job.planning_generation = 1
    setup.add(job)
    setup.commit()

identity = build_dispatch_identity(
    scrape_job_id=str(job_id),
    planning_generation=1,
    strategy_method="DIRECT_HTTP/JSON_LD/v1",
    domain="a.example.com",
    mode=ScrapeProfileMode.HTTP,
    node_class="price_monitor:generic_price_spider",
    match_ids=[match_a, match_b],
)

# --- 2a: two planners racing to CREATE the intent ------------------------
# `plan()` is a get-or-create keyed on `identity_key`, and
# `uq_dispatch_intents_identity_key` is what makes "one intent per
# canonical identity" a DATABASE guarantee rather than an application
# convention. Two real transactions can resolve the race either way --
# the loser's own `_load` may already see the winner's committed row
# (READ COMMITTED), or its INSERT may reach the unique index and be
# refused. Both are correct; what must NEVER happen is two rows, two
# ids, or an unhandled error. Pinning one of the two orderings would be
# testing the scheduler, not the invariant.

start = threading.Barrier(2)
plan_results = {}


def plan_worker(name):
    with Session(engine) as db:
        store = DispatchIntentStore(
            db, workspace_id=workspace_id, scrape_job_id=job_id
        )
        start.wait(timeout=30)
        try:
            intent = store.plan(identity, match_ids=[match_a, match_b])
            intent_id = str(intent.id)
            db.commit()
            plan_results[name] = ("committed", intent_id)
        except IntegrityError:
            db.rollback()
            plan_results[name] = ("refused", None)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            db.rollback()
            plan_results[name] = ("error", type(exc).__name__ + ":" + str(exc))


threads = [threading.Thread(target=plan_worker, args=(n,)) for n in ("p1", "p2")]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=60)

if len(plan_results) != 2:
    fail("A_PLANNER_THREAD_NEVER_FINISHED:" + str(plan_results))
kinds = sorted(kind for kind, _detail in plan_results.values())
if kinds not in (["committed", "committed"], ["committed", "refused"]):
    fail("CONCURRENT_PLAN_OUTCOMES:" + str(plan_results))
agreed = {detail for kind, detail in plan_results.values() if kind == "committed"}
if len(agreed) != 1:
    fail("PLANNERS_DISAGREED_ABOUT_THE_INTENT_ID:" + str(agreed))

with Session(engine) as check:
    rows = check.query(DispatchIntent).filter_by(scrape_job_id=job_id).all()
    if len(rows) != 1:
        fail("MORE_THAN_ONE_INTENT_SURVIVED:" + str(len(rows)))
    if rows[0].state != DispatchIntentState.PLANNED:
        fail("SURVIVING_INTENT_NOT_PLANNED:" + str(rows[0].state))
    surviving_key = rows[0].identity_key

# ... and the constraint that made the above safe is real, deterministically:
# an INSERT that bypasses the get-or-create is refused by the database.
with Session(engine) as duplicate:
    duplicate.add(
        DispatchIntent(
            workspace_id=workspace_id,
            scrape_job_id=job_id,
            planning_generation=identity.planning_generation,
            strategy_method=identity.strategy_method,
            domain=identity.domain,
            mode=ScrapeProfileMode.HTTP,
            node_class=identity.node_class,
            match_ids_digest=identity.work_digest,
            match_ids=[str(match_a), str(match_b)],
            identity_key=surviving_key,
            identity_payload=identity.canonical_payload,
            state=DispatchIntentState.PLANNED,
            cancellation_generation_at_creation=0,
        )
    )
    try:
        duplicate.commit()
    except IntegrityError as exc:
        if "uq_dispatch_intents_identity_key" not in str(exc):
            fail("REFUSED_BY_THE_WRONG_CONSTRAINT:" + str(exc)[:200])
        duplicate.rollback()
    else:
        fail("THE_DATABASE_ACCEPTED_A_SECOND_INTENT_FOR_ONE_IDENTITY")

# --- 2b: two dispatchers racing to POST the same identity ----------------
# The Redis claim (`SET NX`) is what serializes this in production. The
# double below reproduces its atomicity with a lock, so the assertion is
# about the CLIENT's ordering rather than about the fake.

transport = RecordingTransport()
lock = threading.Lock()


class LockedRedis(FakeRedis):
    def set(self, name, value, *, nx=False, ex=None):
        with lock:
            return super().set(name, value, nx=nx, ex=ex)

    def get(self, name):
        with lock:
            return super().get(name)

    def delete(self, *names):
        with lock:
            return super().delete(*names)


class SlowTransport(RecordingTransport):
    """Widen the POST window so the loser is guaranteed to arrive during it."""

    def post(self, url, *, data, auth, timeout):
        import time

        time.sleep(0.25)
        return super().post(url, data=data, auth=auth, timeout=timeout)


shared_redis = LockedRedis()
slow = SlowTransport()
post_start = threading.Barrier(2)
dispatch_results = {}


def dispatch_worker(name):
    with Session(engine) as db:
        http = requests.Session()
        http.post = slow.post
        client = ScrapydDispatchClient(
            settings=make_settings(),
            redis_client=shared_redis,
            session=http,
            intents=DispatchIntentStore(
                db, workspace_id=workspace_id, scrape_job_id=job_id
            ),
        )
        post_start.wait(timeout=30)
        try:
            jobid = client.schedule(
                "price_monitor",
                "generic_price_spider",
                workspace_id=str(workspace_id),
                scrape_job_id=str(job_id),
                match_ids=[match_a, match_b],
                mode="HTTP",
                batch_index=0,
                identity=identity,
            )
            db.commit()
            dispatch_results[name] = ("ok", jobid)
        except ScrapydDispatchError as exc:
            db.rollback()
            dispatch_results[name] = ("refused", str(exc))
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            db.rollback()
            dispatch_results[name] = ("error", type(exc).__name__ + ":" + str(exc))


threads = [threading.Thread(target=dispatch_worker, args=(n,)) for n in ("d1", "d2")]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=60)

if slow.post_count != 1:
    fail("SIMULTANEOUS_DISPATCH_DOUBLE_POSTED:" + str(slow.post_count))

kinds = sorted(kind for kind, _detail in dispatch_results.values())
if kinds == ["ok", "ok"]:
    # Both may legitimately "succeed" -- the loser as a NO-OP returning
    # the winner's already-committed jobid, never as a second POST.
    jobids = {detail for _kind, detail in dispatch_results.values()}
    if len(jobids) != 1:
        fail("TWO_DIFFERENT_JOBIDS_FOR_ONE_IDENTITY:" + str(jobids))
elif kinds == ["ok", "refused"]:
    refused = [d for k, d in dispatch_results.values() if k == "refused"][0]
    if "already in progress" not in refused:
        fail("LOSER_GOT_THE_WRONG_ERROR:" + refused)
else:
    fail("UNEXPECTED_DISPATCH_OUTCOMES:" + str(dispatch_results))

with Session(engine) as check:
    rows = check.query(DispatchIntent).filter_by(scrape_job_id=job_id).all()
    if len(rows) != 1:
        fail("DISPATCH_RACE_CREATED_EXTRA_INTENTS:" + str(len(rows)))
    if rows[0].state != DispatchIntentState.CONFIRMED:
        fail("WINNING_INTENT_NOT_CONFIRMED:" + str(rows[0].state))
    if not rows[0].scrapyd_job_id:
        fail("WINNING_INTENT_HAS_NO_SCRAPYD_JOB_ID")

meta.drop_all(engine)
engine.dispose()
print("OK")
'''
)


# ===========================================================================
# The matrix.
# ===========================================================================


def test_item1_celery_replay_produces_one_post() -> None:
    """1. A duplicate Celery delivery reuses the persisted
    `planning_generation`, rebuilds the identical identity, and POSTs once."""
    _run(_ITEM_1_CELERY_REPLAY)


@pytest.mark.skipif(
    not _scratch_db_reachable(),
    reason=(
        f"needs an isolated scratch Postgres at {_SCRATCH_DSN.rsplit('@', 1)[-1]} "
        "(override with B3_SCRATCH_DATABASE_URL) — this is the one item whose "
        "claim is about the uq_dispatch_intents_identity_key constraint and real "
        "transaction isolation, which no in-memory fake can express"
    ),
)
def test_item2_simultaneous_dispatches_produce_one_post() -> None:
    """2. Two threads dispatching the same identity: exactly one
    `schedule.json` POST is issued — the loser either no-ops on the winner's
    jobid or is told the dispatch is in flight — and the database refuses a
    second `dispatch_intents` row for that identity."""
    _run(_ITEM_2_CONCURRENT_DISPATCH, extra_env={"B3_SCRATCH_DATABASE_URL": _SCRATCH_DSN})


def test_item3_cross_mode_handoff_mints_a_new_generation() -> None:
    """3. HTTP fails at the node -> the BROWSER rung is committed as a new
    durable generation: distinct identity keys, distinct node classes, and
    both are genuinely schedulable."""
    _run(_ITEM_3_CROSS_MODE_HANDOFF)


def test_item4_changing_fallback_subset_never_aliases_the_full_batch() -> None:
    """4. The A1 regression: a smaller subset replanned inside one recovery
    window gets its own key (the work digest moved, the generation did not)
    and is actually POSTed rather than answered with the full batch's jobid."""
    _run(_ITEM_4_CHANGING_FALLBACK_SUBSET)


def test_item5_sentinel_expiry_never_authorizes_a_re_post() -> None:
    """5. An aged-out guard over a CONFIRMED intent heals rather than
    re-POSTs; a value that proves nothing is replaced only under
    compare-and-set (and the loser of that race backs off); an aged-out
    sentinel over a POSTED intent is reconciled by `schedule()` itself
    (EPA B3b) -- AMBIGUOUS blocks with no POST, a found run is adopted
    with no POST, and established absence lets the ordinary path POST
    exactly once."""
    _run(_ITEM_5_SENTINEL_EXPIRY)


def test_item6_crash_before_post_retries_cleanly() -> None:
    """6. Claim-then-die leaves a PLANNED intent; once the sentinel expires
    the retry reconciles to "never in flight", reuses the same row, and POSTs
    exactly once."""
    _run(_ITEM_6_CRASH_BEFORE_POST)


@pytest.mark.parametrize(
    "scenario",
    [
        "deterministic_jobid_finds_the_orphan",
        "deterministic_jobid_proves_absence",
        "listjobs_args_correlate_a_pending_orphan",
        "listjobs_args_cannot_see_a_running_orphan",
        "browser_intents_are_reconciled_on_the_browser_pool",
        "a_row_that_cannot_reproduce_its_key_is_refused",
        "another_workspace_cannot_reconcile_this_intent",
    ],
)
def test_item7_crash_after_post_is_reconciled_not_re_posted(scenario: str) -> None:
    """7. `reconcile_inflight` resolves a POSTED-but-unconfirmed intent through
    BOTH Step-0 mechanisms — the deterministic client-supplied jobid and
    listjobs.json spider-arg correlation — and refuses to guess when neither
    can answer. It never issues a POST."""
    env = {**os.environ, **_ENV}
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _ITEM_7_SCENARIOS, scenario],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr[-4000:]!r}"
    )
    assert result.stdout.strip().splitlines()[-1] == "OK", result.stdout


def test_item8_node_failure_replan_mints_a_new_node_class_identity() -> None:
    """8. A dead node leaves a durable FAILED intent (kept, not deleted) and
    the replan onto another `node_class` is a new generation with a new
    identity."""
    _run(_ITEM_8_NODE_FAILURE_REPLAN)


def test_item9_rate_limit_deferral_cannot_be_restamped() -> None:
    """9. A DEFERRED rate-limit handback re-planned in an overlapping recovery
    window is refused by `stamp_targets_dispatched` until a guard/intent for
    that exact subset exists — and stays `dispatched_at IS NULL` meanwhile, so
    the redispatch sweep can still find it."""
    _run(_ITEM_9_RATE_LIMIT_DEFERRAL)


def test_item10_stale_cancellation_generation_is_refused() -> None:
    """10. A2's fence: an intent carrying a superseded
    `cancellation_generation_at_creation` is refused before anything is
    claimed or POSTed, and a CANCELLED job is refused regardless."""
    _run(_ITEM_10_CANCELLATION_FENCE)
