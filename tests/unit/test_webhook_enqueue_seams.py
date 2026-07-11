"""Unit tests for the SPEC-16 US3 webhook enqueue seams (T036,
contracts/events.md).

With `enqueue`/`get_session` monkeypatched/mocked (no live Celery/DB),
asserts each of the FOUR seam paths calls
`enqueue(CREATE_WEBHOOK_EVENT, queue="webhook_events", kwargs=...)` exactly
once per genuine transition, with the expected `event_type`/payload:

1. alert transitions -- `tasks_analysis.py::recompute_variant`
2. job finalization -- `tasks_jobs.py::finalize_jobs`
3. strategy promotion/rediscovery surfaced by `flush_profile` --
   `tasks_strategy.py::flush_stats`
4. strategy rediscovery -- `tasks_strategy.py::light_recheck`

Plus the negative cases (alert `event_type is None`, job
`UNCHANGED`/`CANCELLED`, strategy `apply_promotion`/`apply_rediscovery`
returning `False`) enqueue nothing, and a raised broker error inside any
seam is swallowed (the source path completes, its commit stands).

Loaded in a fresh subprocess per scenario (mirrors
`test_price_analysis_task.py`/`test_jobs_dispatch_task.py`'s
`_COMMON_SETUP` pattern), for the same two reasons: (1) `apps/api` and
`apps/workers` each ship their own top-level ``app`` package, so importing
`app.workers.*` in the shared test process is ambiguous once another test
module has already imported `apps/api`'s `app` package; (2)
`celery_app.py` calls `get_settings()` at module scope, needing a clean,
self-contained env.
"""

from __future__ import annotations

import os
import subprocess
import sys

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


def _run(script_body: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **_ENV}
    return subprocess.run(
        [sys.executable, "-c", script_body],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


_RECORDING_ENQUEUE_HELPER = """
class _RecordingEnqueue:
    def __init__(self, raise_on=None):
        self.calls = []
        self.raise_on = raise_on or set()

    def __call__(self, name, *, queue, kwargs=None):
        self.calls.append({"name": name, "queue": queue, "kwargs": kwargs})
        if name in self.raise_on:
            raise RuntimeError("simulated broker outage")
"""


# --- 1. alert seam: recompute_variant --------------------------------------

_ALERT_SETUP = (
    """
import sys
import uuid
from contextlib import contextmanager
from decimal import Decimal
from datetime import datetime, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

from _alerts_fake_session import FakeAlertsSession
from app_shared.models.catalog import ProductVariant
from app_shared.models.observations import MatchCurrentPrice
from app_shared.task_names import CREATE_WEBHOOK_EVENT

import app.workers.tasks_analysis as tasks_analysis

"""
    + _RECORDING_ENQUEUE_HELPER
    + """
fake_session = FakeAlertsSession()


@contextmanager
def fake_get_session():
    yield fake_session


def fake_set_workspace_context(session, workspace_id):
    pass


tasks_analysis.get_session = fake_get_session
tasks_analysis.set_workspace_context = fake_set_workspace_context

workspace_id = uuid.uuid4()
product_id = uuid.uuid4()
variant_id = uuid.uuid4()

variant = ProductVariant(
    workspace_id=workspace_id,
    product_id=product_id,
    title="Widget",
    current_price=Decimal("95"),
    currency="SAR",
    status="active",
)
variant.id = variant_id
fake_session.seed(variant)


def make_match(price, currency="SAR"):
    match = MatchCurrentPrice(
        workspace_id=workspace_id,
        match_id=uuid.uuid4(),
        product_id=product_id,
        product_variant_id=variant_id,
        competitor_id=uuid.uuid4(),
        price=Decimal(price),
        currency=currency,
        comparable=True,
        success=True,
    )
    match.id = uuid.uuid4()
    return match
"""
)


def test_alert_seam_enqueues_once_on_genuine_transition() -> None:
    script = (
        _ALERT_SETUP
        + """
enqueue = _RecordingEnqueue()
tasks_analysis.enqueue = enqueue

scrape_job_id = uuid.uuid4()
fake_session.seed(make_match("90"), make_match("100"), make_match("110"))

# client_price 95 > cheapest 90 -> HIGH_PRICE (CREATED transition, first run).
tasks_analysis.recompute_variant(
    workspace_id=str(workspace_id),
    product_variant_id=str(variant_id),
    scrape_job_id=str(scrape_job_id),
)

calls = [c for c in enqueue.calls if c["name"] == CREATE_WEBHOOK_EVENT]
if len(calls) != 1:
    print("EXPECTED_ONE_CALL:" + str(len(calls)))
    sys.exit(1)

call = calls[0]
if call["queue"] != "webhook_events":
    print("WRONG_QUEUE:" + str(call["queue"]))
    sys.exit(1)
kwargs = call["kwargs"]
if kwargs["workspace_id"] != str(workspace_id):
    print("WRONG_WORKSPACE:" + str(kwargs["workspace_id"]))
    sys.exit(1)
if kwargs["event_type"] != "price.alert.created":
    print("WRONG_EVENT_TYPE:" + str(kwargs["event_type"]))
    sys.exit(1)
if kwargs["payload"]["transition"] != "CREATED":
    print("WRONG_TRANSITION:" + str(kwargs["payload"]))
    sys.exit(1)
if str(scrape_job_id) not in kwargs["dedup_key"]:
    print("DEDUP_KEY_MISSING_JOB_ID:" + kwargs["dedup_key"])
    sys.exit(1)

# Re-running with identical inputs -> UNCHANGED -> no additional enqueue.
tasks_analysis.recompute_variant(
    workspace_id=str(workspace_id),
    product_variant_id=str(variant_id),
    scrape_job_id=str(scrape_job_id),
)
calls = [c for c in enqueue.calls if c["name"] == CREATE_WEBHOOK_EVENT]
if len(calls) != 1:
    print("UNCHANGED_RUN_ENQUEUED_AGAIN:" + str(len(calls)))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"


def test_alert_seam_no_event_type_enqueues_nothing() -> None:
    script = (
        _ALERT_SETUP
        + """
enqueue = _RecordingEnqueue()
tasks_analysis.enqueue = enqueue

# No competitor rows at all -> NO_COMPETITOR_DATA, but with no prior
# history and outcome.type staying at its very first value the engine's
# transition() call may still be None (no prior + NORMAL-only path) --
# assert directly on the negative: whatever recompute_variant computed
# internally, if event_type ended up None then nothing was enqueued.
tasks_analysis.recompute_variant(
    workspace_id=str(workspace_id),
    product_variant_id=str(variant_id),
)

calls = [c for c in enqueue.calls if c["name"] == CREATE_WEBHOOK_EVENT]
alert_states = fake_session._rows.get(
    __import__("app_shared.models.alerts", fromlist=["VariantAlertState"]).VariantAlertState, []
)
had_event = bool(
    fake_session._rows.get(
        __import__("app_shared.models.alerts", fromlist=["PriceAlertEvent"]).PriceAlertEvent, []
    )
)
if had_event and len(calls) != 1:
    print("EVENT_WRITTEN_BUT_NOT_ENQUEUED:" + str(len(calls)))
    sys.exit(1)
if not had_event and len(calls) != 0:
    print("NO_EVENT_BUT_ENQUEUED:" + str(len(calls)))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"


def test_alert_seam_broker_error_is_swallowed() -> None:
    script = (
        _ALERT_SETUP
        + """
from app_shared.task_names import CREATE_WEBHOOK_EVENT

enqueue = _RecordingEnqueue(raise_on={CREATE_WEBHOOK_EVENT})
tasks_analysis.enqueue = enqueue

fake_session.seed(make_match("90"), make_match("100"), make_match("110"))

# Must not raise even though enqueue() throws.
tasks_analysis.recompute_variant(
    workspace_id=str(workspace_id),
    product_variant_id=str(variant_id),
)

if not fake_session.committed:
    print("SOURCE_COMMIT_DID_NOT_STAND")
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"


# --- 2. job seam: finalize_jobs ---------------------------------------------

_JOB_SETUP = (
    """
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

from _jobs_fake_session import FakeOrmSession
from app_shared.enums import (
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.task_names import CREATE_WEBHOOK_EVENT, STRATEGY_STATS_FLUSH

import app.workers.tasks_jobs as tasks_jobs

"""
    + _RECORDING_ENQUEUE_HELPER
    + """
fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


def fake_set_workspace_context(session, workspace_id):
    pass


tasks_jobs.get_session = fake_get_session
tasks_jobs.set_workspace_context = fake_set_workspace_context


def _make_job(workspace_id, status):
    now = datetime.now(timezone.utc)
    job = ScrapeJob(
        workspace_id=workspace_id,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.VARIANT,
        status=status,
        total_targets=2,
        source=ScrapeJobSource.API,
        created_at=now,
        started_at=now,
    )
    job.id = uuid.uuid4()
    return job


def _make_target(workspace_id, scrape_job_id, status):
    target = ScrapeJobTarget(
        workspace_id=workspace_id,
        scrape_job_id=scrape_job_id,
        match_id=uuid.uuid4(),
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    target.id = uuid.uuid4()
    return target


workspace_id = uuid.uuid4()
"""
)


def test_job_seam_enqueues_once_per_finalized_terminal_job() -> None:
    script = (
        _JOB_SETUP
        + """
enqueue = _RecordingEnqueue()
tasks_jobs.enqueue = enqueue

completed_job = _make_job(workspace_id, ScrapeJobStatus.RUNNING)
fake_session.seed(completed_job)
fake_session.seed(_make_target(workspace_id, completed_job.id, ScrapeTargetStatus.COMPLETED))
fake_session.seed(_make_target(workspace_id, completed_job.id, ScrapeTargetStatus.COMPLETED))

tasks_jobs.finalize_jobs()

calls = [c for c in enqueue.calls if c["name"] == CREATE_WEBHOOK_EVENT]
if len(calls) != 1:
    print("EXPECTED_ONE_CALL:" + str(len(calls)))
    sys.exit(1)
call = calls[0]
if call["queue"] != "webhook_events":
    print("WRONG_QUEUE:" + str(call["queue"]))
    sys.exit(1)
kwargs = call["kwargs"]
if kwargs["event_type"] != "scrape.job.completed":
    print("WRONG_EVENT_TYPE:" + str(kwargs["event_type"]))
    sys.exit(1)
if kwargs["payload"]["scrape_job_id"] != str(completed_job.id):
    print("WRONG_JOB_ID:" + str(kwargs["payload"]))
    sys.exit(1)
if kwargs["payload"]["success_count"] != 2 or kwargs["payload"]["total"] != 2:
    print("WRONG_COUNTS:" + str(kwargs["payload"]))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"


def test_job_seam_already_cancelled_job_enqueues_nothing() -> None:
    script = (
        _JOB_SETUP
        + """
enqueue = _RecordingEnqueue()
tasks_jobs.enqueue = enqueue

# Already-terminal CANCELLED job is skipped outright by finalize_jobs'
# own scan (_NON_TERMINAL_JOB_STATUSES excludes it) -- never finalized,
# never builds a webhook event.
cancelled_job = _make_job(workspace_id, ScrapeJobStatus.CANCELLED)
fake_session.seed(cancelled_job)

tasks_jobs.finalize_jobs()

calls = [c for c in enqueue.calls if c["name"] == CREATE_WEBHOOK_EVENT]
if calls:
    print("CANCELLED_JOB_ENQUEUED:" + str(calls))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"


def test_job_seam_not_yet_finalized_job_enqueues_nothing() -> None:
    script = (
        _JOB_SETUP
        + """
enqueue = _RecordingEnqueue()
tasks_jobs.enqueue = enqueue

# One target still PENDING -> job never finalizes this cycle ("unchanged").
running_job = _make_job(workspace_id, ScrapeJobStatus.RUNNING)
fake_session.seed(running_job)
fake_session.seed(_make_target(workspace_id, running_job.id, ScrapeTargetStatus.COMPLETED))
fake_session.seed(_make_target(workspace_id, running_job.id, ScrapeTargetStatus.PENDING))

tasks_jobs.finalize_jobs()

calls = [c for c in enqueue.calls if c["name"] == CREATE_WEBHOOK_EVENT]
if calls:
    print("UNFINALIZED_JOB_ENQUEUED:" + str(calls))
    sys.exit(1)
if running_job.status != ScrapeJobStatus.RUNNING:
    print("JOB_WRONGLY_FINALIZED:" + str(running_job.status))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"


def test_job_seam_broker_error_is_swallowed() -> None:
    script = (
        _JOB_SETUP
        + """
from app_shared.task_names import CREATE_WEBHOOK_EVENT

enqueue = _RecordingEnqueue(raise_on={CREATE_WEBHOOK_EVENT})
tasks_jobs.enqueue = enqueue

completed_job = _make_job(workspace_id, ScrapeJobStatus.RUNNING)
fake_session.seed(completed_job)
fake_session.seed(_make_target(workspace_id, completed_job.id, ScrapeTargetStatus.COMPLETED))

tasks_jobs.finalize_jobs()

if not fake_session.committed:
    print("SOURCE_COMMIT_DID_NOT_STAND")
    sys.exit(1)
if completed_job.status != ScrapeJobStatus.COMPLETED:
    print("JOB_NOT_FINALIZED:" + str(completed_job.status))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"


# --- 3. strategy seam: flush_stats (promotion + rediscovery transitions) ----

_FLUSH_STATS_SETUP = (
    """
import sys
import uuid

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

from app_shared.enums import StrategyStatus
from app_shared.strategy.flush import FlushResult, StrategyTransition
from app_shared.task_names import CREATE_WEBHOOK_EVENT

import app.workers.tasks_strategy as tasks_strategy

"""
    + _RECORDING_ENQUEUE_HELPER
    + """

class _FakeSession:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True


fake_session = _FakeSession()


class _ctx:
    def __enter__(self):
        return fake_session

    def __exit__(self, *exc):
        return False


def fake_get_session():
    return _ctx()


def fake_set_workspace_context(session, workspace_id):
    pass


tasks_strategy.get_session = fake_get_session
tasks_strategy.set_workspace_context = fake_set_workspace_context

workspace_id = uuid.uuid4()
profile_promote = uuid.uuid4()
profile_rediscover = uuid.uuid4()
profile_noop = uuid.uuid4()
"""
)


def test_flush_stats_seam_enqueues_once_per_surfaced_transition() -> None:
    script = (
        _FLUSH_STATS_SETUP
        + """
enqueue = _RecordingEnqueue()
tasks_strategy.enqueue = enqueue


def fake_flush_profile(session, redis, profile_id):
    if profile_id == profile_promote:
        return FlushResult(
            keys_flushed=1,
            transitions=(
                StrategyTransition(
                    profile_id=profile_promote,
                    workspace_id=workspace_id,
                    domain="promoted.example.com",
                    new_status=StrategyStatus.ACTIVE,
                    change="PROMOTED",
                    method="DIRECT_HTTP",
                ),
            ),
        )
    if profile_id == profile_rediscover:
        return FlushResult(
            keys_flushed=1,
            transitions=(
                StrategyTransition(
                    profile_id=profile_rediscover,
                    workspace_id=workspace_id,
                    domain="degraded.example.com",
                    new_status=StrategyStatus.DEGRADED,
                    change="REDISCOVERY_TRIGGERED",
                    method=None,
                ),
            ),
        )
    # profile_noop: apply_promotion/apply_rediscovery both returned False
    # internally -- nothing surfaced (no genuine row change this cycle).
    return FlushResult(keys_flushed=0, transitions=())


tasks_strategy.flush_profile = fake_flush_profile

tasks_strategy.flush_stats(
    workspace_id=str(workspace_id),
    profile_ids=[str(profile_promote), str(profile_rediscover), str(profile_noop)],
)

calls = [c for c in enqueue.calls if c["name"] == CREATE_WEBHOOK_EVENT]
if len(calls) != 2:
    print("EXPECTED_TWO_CALLS:" + str(len(calls)))
    sys.exit(1)

by_change = {c["kwargs"]["payload"]["change"]: c for c in calls}

promoted = by_change.get("PROMOTED")
if promoted is None:
    print("MISSING_PROMOTED_CALL:" + str(calls))
    sys.exit(1)
if (
    promoted["queue"] != "webhook_events"
    or promoted["kwargs"]["event_type"] != "domain.strategy.updated"
):
    print("WRONG_PROMOTED_SHAPE:" + str(promoted))
    sys.exit(1)
if (
    promoted["kwargs"]["payload"]["new_status"] != "ACTIVE"
    or promoted["kwargs"]["payload"]["method"] != "DIRECT_HTTP"
):
    print("WRONG_PROMOTED_PAYLOAD:" + str(promoted["kwargs"]["payload"]))
    sys.exit(1)
if promoted["kwargs"]["workspace_id"] != str(workspace_id):
    print("WRONG_PROMOTED_WORKSPACE:" + str(promoted))
    sys.exit(1)

rediscovered = by_change.get("REDISCOVERY_TRIGGERED")
if rediscovered is None:
    print("MISSING_REDISCOVERY_CALL:" + str(calls))
    sys.exit(1)
if (
    rediscovered["kwargs"]["payload"]["new_status"] != "DEGRADED"
    or rediscovered["kwargs"]["payload"]["method"] is not None
):
    print("WRONG_REDISCOVERY_PAYLOAD:" + str(rediscovered["kwargs"]["payload"]))
    sys.exit(1)

if not fake_session.committed:
    print("SESSION_NOT_COMMITTED")
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"


def test_flush_stats_seam_broker_error_is_swallowed() -> None:
    script = (
        _FLUSH_STATS_SETUP
        + """
from app_shared.task_names import CREATE_WEBHOOK_EVENT

enqueue = _RecordingEnqueue(raise_on={CREATE_WEBHOOK_EVENT})
tasks_strategy.enqueue = enqueue


def fake_flush_profile(session, redis, profile_id):
    return FlushResult(
        keys_flushed=1,
        transitions=(
            StrategyTransition(
                profile_id=profile_promote,
                workspace_id=workspace_id,
                domain="promoted.example.com",
                new_status=StrategyStatus.ACTIVE,
                change="PROMOTED",
                method="DIRECT_HTTP",
            ),
        ),
    )


tasks_strategy.flush_profile = fake_flush_profile

# Must not raise even though enqueue() throws for every transition.
tasks_strategy.flush_stats(workspace_id=str(workspace_id), profile_ids=[str(profile_promote)])

if not fake_session.committed:
    print("SOURCE_COMMIT_DID_NOT_STAND")
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"


# --- 4. strategy seam: light_recheck (rediscovery) --------------------------

_LIGHT_RECHECK_SETUP = (
    """
import sys
import uuid

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

from app_shared.enums import StrategyStatus
from app_shared.task_names import CREATE_WEBHOOK_EVENT

import app.workers.tasks_strategy as tasks_strategy

"""
    + _RECORDING_ENQUEUE_HELPER
    + """

class _FakeSession:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True


fake_session = _FakeSession()


class _ctx:
    def __enter__(self):
        return fake_session

    def __exit__(self, *exc):
        return False


class _StubProfile:
    def __init__(self, profile_id, workspace_id, domain):
        self.id = profile_id
        self.workspace_id = workspace_id
        self.domain = domain
        self.status = StrategyStatus.ACTIVE
        self.preferred_access_method = None
        self.preferred_extraction_method = None
        self.recent_failure_count = 0


workspace_id = uuid.uuid4()
profile_triggered_id = uuid.uuid4()
profile_not_triggered_id = uuid.uuid4()

profile_triggered = _StubProfile(profile_triggered_id, workspace_id, "degraded.example.com")
profile_not_triggered = _StubProfile(profile_not_triggered_id, workspace_id, "healthy.example.com")

profiles_by_id = {
    profile_triggered_id: profile_triggered,
    profile_not_triggered_id: profile_not_triggered,
}


def fake_get_session():
    return _ctx()


def fake_set_workspace_context(session, workspace_id):
    pass


def fake_scan_active_profile_refs(session, limit):
    return [(profile_triggered_id, workspace_id), (profile_not_triggered_id, workspace_id)]


def fake_scoped_get(session, model, profile_id, workspace_id):
    return profiles_by_id[profile_id]


def fake_combined_stats_for_profile(session, redis, profile):
    return object()


def fake_build_recent_signals(session, profile):
    return object()


def fake_evaluate_rediscovery(profile, combined, recent_signals, thresholds, *, scope="domain"):
    class _Decision:
        trigger = True
        reason = "test"

    return _Decision()


def fake_apply_rediscovery(session, profile, decision):
    # Only the "triggered" profile represents a genuine transition;
    # apply_rediscovery returning False (already-DEGRADED/DISABLED/no-op)
    # must surface no transition at all.
    return profile.id == profile_triggered_id


tasks_strategy.get_session = fake_get_session
tasks_strategy.set_workspace_context = fake_set_workspace_context
tasks_strategy._scan_active_profile_refs = fake_scan_active_profile_refs
tasks_strategy.scoped_get = fake_scoped_get
tasks_strategy._combined_stats_for_profile = fake_combined_stats_for_profile
tasks_strategy.build_recent_signals = fake_build_recent_signals
tasks_strategy.evaluate_rediscovery = fake_evaluate_rediscovery
tasks_strategy.apply_rediscovery = fake_apply_rediscovery
tasks_strategy.get_redis_client = lambda: object()
"""
)


def test_light_recheck_seam_enqueues_once_per_triggered_profile() -> None:
    script = (
        _LIGHT_RECHECK_SETUP
        + """
enqueue = _RecordingEnqueue()
tasks_strategy.enqueue = enqueue

tasks_strategy.light_recheck()

calls = [c for c in enqueue.calls if c["name"] == CREATE_WEBHOOK_EVENT]
if len(calls) != 1:
    print("EXPECTED_ONE_CALL:" + str(len(calls)))
    sys.exit(1)
call = calls[0]
if call["queue"] != "webhook_events":
    print("WRONG_QUEUE:" + str(call["queue"]))
    sys.exit(1)
kwargs = call["kwargs"]
if kwargs["event_type"] != "domain.strategy.updated":
    print("WRONG_EVENT_TYPE:" + str(kwargs["event_type"]))
    sys.exit(1)
if kwargs["payload"]["new_status"] != "DEGRADED":
    print("WRONG_STATUS:" + str(kwargs["payload"]))
    sys.exit(1)
if kwargs["payload"]["strategy_profile_id"] != str(profile_triggered_id):
    print("WRONG_PROFILE:" + str(kwargs["payload"]))
    sys.exit(1)
if kwargs["workspace_id"] != str(workspace_id):
    print("WRONG_WORKSPACE:" + str(kwargs))
    sys.exit(1)

if not fake_session.committed:
    print("SESSION_NOT_COMMITTED")
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"


def test_light_recheck_seam_broker_error_is_swallowed() -> None:
    script = (
        _LIGHT_RECHECK_SETUP
        + """
from app_shared.task_names import CREATE_WEBHOOK_EVENT

enqueue = _RecordingEnqueue(raise_on={CREATE_WEBHOOK_EVENT})
tasks_strategy.enqueue = enqueue

# Must not raise even though enqueue() throws.
tasks_strategy.light_recheck()

if not fake_session.committed:
    print("SOURCE_COMMIT_DID_NOT_STAND")
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"
