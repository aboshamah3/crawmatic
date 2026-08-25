"""Subprocess harness: the REAL planner path under replay/retry/replan (EPA B1).

Run as ``python -c "<this file's text>" <scenario>`` by
``tests/unit/scrapyd/test_dispatch_identity.py``. Not a ``test_*.py``
module, so pytest's default collection skips it — it is a support script,
executed in a fresh interpreter for the two reasons
``tests/unit/test_jobs_dispatch_task.py`` documents: ``apps/api`` and
``apps/workers`` each ship a top-level ``app`` package, and
``celery_app.py`` calls ``get_settings()`` at import time.

Everything below runs against ``FakeOrmSession`` + a fake Redis + a
stubbed HTTP transport, wired through the REAL
``app.workers.tasks_jobs.dispatch_job``, the REAL
``ScrapydDispatchClient`` and the REAL ``DispatchIntentStore``. The point
is that "the planning generation is durable" is a claim about the
planner, so a hand-built identity could not prove it.
"""

import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

import requests  # noqa: E402

from _jobs_fake_session import FakeOrmSession  # noqa: E402
from app_shared.enums import (  # noqa: E402
    AccessMethod,
    DispatchIntentState,
    ExtractionMethod,
    MatchPriority,
    MatchStatus,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
    StrategyMethodProofState,
)
from app_shared.jobs.cancellation import iter_known_scrapyd_job_ids  # noqa: E402
from app_shared.models.competitors_matches import (  # noqa: E402
    Competitor,
    CompetitorProductMatch,
)
from app_shared.models.dispatch import DispatchIntent  # noqa: E402
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget  # noqa: E402
from app_shared.models.strategy import (  # noqa: E402
    DomainStrategyMethod,
    DomainStrategyProfile,
)
from app_shared.scrapyd.client import ScrapydDispatchClient as RealClient  # noqa: E402

import app.workers.tasks_jobs as tasks_jobs  # noqa: E402


def fail(message):
    print(message)
    sys.exit(1)


# --- fakes -----------------------------------------------------------------


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
fake_redis = FakeRedis()


def fake_post(url, *, data, auth, timeout):
    calls.append({"url": url, "data": dict(data), "auth": auth})
    return FakeResponse(200, {"status": "ok", "jobid": "job-" + str(len(calls))})


def client_factory(*, settings=None, intents=None):
    http_session = requests.Session()
    http_session.post = fake_post
    return RealClient(
        settings=settings,
        redis_client=fake_redis,
        session=http_session,
        intents=intents,
    )


tasks_jobs.ScrapydDispatchClient = client_factory
tasks_jobs.set_workspace_context = lambda session, workspace_id: None

fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


tasks_jobs.get_session = fake_get_session


# --- fixture world ---------------------------------------------------------
#
# One job, one competitor domain, two matches, and a real strategy
# profile with one runnable method -- the profile is what makes the
# planner ADVANCE a cursor, which is the only thing that may advance the
# planning generation.

now = datetime.now(timezone.utc)
workspace_id = uuid.uuid4()
job_id = uuid.uuid4()
competitor_id = uuid.uuid4()

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
job.cancellation_generation = 0
job.planning_generation = 0
fake_session.seed(job)

competitor = Competitor(workspace_id=workspace_id, name="A", domain="a.example.com")
competitor.id = competitor_id
fake_session.seed(competitor)

targets = []
matches = []
for _ in range(2):
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
fake_session.seed(*matches)
fake_session.seed(*targets)

strategy_profile = DomainStrategyProfile(
    workspace_id=workspace_id,
    competitor_id=competitor_id,
    domain="a.example.com",
    url_pattern="a.example.com",
)
strategy_profile.id = uuid.uuid4()
fake_session.seed(strategy_profile)

method = DomainStrategyMethod(
    workspace_id=workspace_id,
    domain_strategy_profile_id=strategy_profile.id,
    access_method=AccessMethod.DIRECT_HTTP,
    extraction_method=ExtractionMethod.JSON_LD,
    priority=1,
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
fake_session.seed(method)


def intents_for_job():
    return [
        row
        for row in fake_session._rows.get(DispatchIntent, [])
        if row.scrape_job_id == job_id
    ]


def reset_targets_for_replan(clear_cursor):
    """Undo the dispatch stamps so the planner re-plans the same targets.

    ``clear_cursor=False`` models a plain task retry: the work is
    re-planned but the strategy chain has not moved, so the generation
    must NOT advance. ``clear_cursor=True`` models a committed
    replan/fallback: the chain moves, so it MUST.
    """
    for target in targets:
        target.dispatched_at = None
        target.status = ScrapeTargetStatus.PENDING
        if clear_cursor:
            target.current_strategy_method_id = None


def run():
    tasks_jobs.dispatch_job(str(job_id), str(workspace_id))


scenario = sys.argv[1]

if scenario == "celery_replay_reuses_persisted_generation":
    run()
    if len(calls) != 1:
        fail("EXPECTED_ONE_POST_GOT:" + str(len(calls)))
    generation_after_first = job.planning_generation
    if generation_after_first != 1:
        fail("FIRST_PLAN_DID_NOT_ADVANCE_GENERATION:" + str(generation_after_first))

    # A duplicate Celery delivery of the very same task.
    run()
    if job.planning_generation != generation_after_first:
        fail("REPLAY_MINTED_A_NEW_GENERATION:" + str(job.planning_generation))
    if len(calls) != 1:
        fail("REPLAY_CAUSED_A_SECOND_POST:" + str(len(calls)))
    if len(intents_for_job()) != 1:
        fail("REPLAY_CREATED_A_SECOND_INTENT:" + str(len(intents_for_job())))

elif scenario == "task_retry_does_not_mint_a_new_generation":
    run()
    if len(calls) != 1:
        fail("EXPECTED_ONE_POST_GOT:" + str(len(calls)))
    first_generation = job.planning_generation
    first_key = intents_for_job()[0].identity_key

    # A retry that re-plans the identical work: the strategy chain has
    # NOT moved, so the generation is reused verbatim, the identity is
    # rebuilt identically, and the durable intent answers the re-POST.
    reset_targets_for_replan(clear_cursor=False)
    run()

    if job.planning_generation != first_generation:
        fail("RETRY_MINTED_A_NEW_GENERATION:" + str(job.planning_generation))
    if len(calls) != 1:
        fail("RETRY_CAUSED_A_SECOND_POST:" + str(len(calls)))
    intents = intents_for_job()
    if len(intents) != 1:
        fail("RETRY_CREATED_A_SECOND_INTENT:" + str(len(intents)))
    if intents[0].identity_key != first_key:
        fail("RETRY_CHANGED_THE_IDENTITY_KEY")

elif scenario == "committed_replan_advances_the_generation":
    run()
    first_generation = job.planning_generation
    first_key = intents_for_job()[0].identity_key

    # A committed strategy-chain transition: this IS new work.
    reset_targets_for_replan(clear_cursor=True)
    run()

    if job.planning_generation != first_generation + 1:
        fail("REPLAN_DID_NOT_ADVANCE_GENERATION:" + str(job.planning_generation))
    if len(calls) != 2:
        fail("REPLAN_WAS_SUPPRESSED_BY_THE_OLD_GUARD:" + str(len(calls)))
    keys = {row.identity_key for row in intents_for_job()}
    if len(keys) != 2:
        fail("REPLAN_ALIASED_THE_PREVIOUS_IDENTITY:" + str(keys))
    if first_key not in keys:
        fail("THE_ORIGINAL_INTENT_WAS_LOST")

elif scenario == "intents_are_persisted_by_the_planner":
    run()
    intents = intents_for_job()
    if len(intents) != 1:
        fail("EXPECTED_ONE_INTENT_GOT:" + str(len(intents)))
    intent = intents[0]
    if intent.state != DispatchIntentState.CONFIRMED:
        fail("INTENT_NOT_CONFIRMED:" + str(intent.state))
    if intent.scrapyd_job_id != "job-1":
        fail("INTENT_MISSING_SCRAPYD_JOB_ID:" + str(intent.scrapyd_job_id))
    if intent.workspace_id != workspace_id:
        fail("INTENT_NOT_WORKSPACE_SCOPED")
    if intent.planning_generation != job.planning_generation:
        fail("INTENT_GENERATION_DRIFTED:" + str(intent.planning_generation))
    if intent.cancellation_generation_at_creation != 0:
        fail("INTENT_FENCE_NOT_CAPTURED")
    if intent.domain != "a.example.com":
        fail("INTENT_DOMAIN_WRONG:" + str(intent.domain))
    if intent.strategy_method == "default":
        fail("INTENT_STRATEGY_METHOD_NOT_RESOLVED")
    if sorted(intent.match_ids) != sorted(str(m.id) for m in matches):
        fail("INTENT_MATCH_IDS_WRONG:" + str(intent.match_ids))
    # `batch_index` is recorded for traceability but must NOT be in the key.
    if intent.batch_index != "0":
        fail("INTENT_BATCH_INDEX_NOT_RECORDED:" + str(intent.batch_index))
    if ":0" in intent.identity_key.rsplit(":", 1)[-1]:
        fail("IDENTITY_KEY_LOOKS_POSITIONAL:" + intent.identity_key)

    # A2's step 2 is live: cancellation now finds the real Scrapyd ids.
    known = list(iter_known_scrapyd_job_ids(fake_session, job_id))
    if known != ["job-1"]:
        fail("ITER_KNOWN_SCRAPYD_JOB_IDS_DID_NOT_READ_DISPATCH_INTENTS:" + str(known))

else:
    fail("UNKNOWN_SCENARIO:" + scenario)

print("OK")
sys.exit(0)
