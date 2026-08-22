"""Worker-loss replay: idempotent, no contradictory state (audit §12).

Audit §12 promotion criterion: *"Worker-loss test demonstrates idempotent
replay with no lost or contradictory state."* Enabling `task_acks_late` +
`task_reject_on_worker_lost` (and publishing the outbox at-least-once)
means redelivery is now a normal, expected event rather than an
impossible one — so every task must survive being killed part-way through
and run again from the top.

Each test below kills a task at the most dangerous point available to it
and then replays the *same* delivery, asserting the second run produces
exactly one logical effect and never contradicts the first:

1. `create_webhook_event` — killed after its INSERT, replayed: the
   deterministic primary key + `ON CONFLICT DO NOTHING` collapse the
   replay to one row. This is the task the audit's "duplicate-charge
   incident" warning applies to most directly, and it is why the DB (not
   the best-effort Redis dedup) is the guard.
2. `finalize_jobs` — killed after its commit, replayed: the job is
   already terminal, so the replay finalizes nothing again and records no
   second event/flush message.
3. `run_discovery` — killed mid-probe, replayed: the run is no longer
   PENDING, so the replay refuses to re-probe. This one costs real proxy
   money per replay, which is exactly the "fix becomes a duplicate-charge
   incident" failure mode.

Loaded in fresh subprocesses (mirrors `test_webhook_enqueue_seams.py`)
because `apps/api`/`apps/workers` each ship a top-level `app` package and
`celery_app.py` calls `get_settings()` at module scope.
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


def _run(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", body],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **_ENV},
    )


def _assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")


# --- 1. create_webhook_event ------------------------------------------------

_WEBHOOK_SETUP = """
import sys
import uuid
from contextlib import contextmanager

sys.path.insert(0, "apps/workers")

from sqlalchemy.dialects import postgresql

import app.workers.tasks_webhooks as tasks_webhooks


class _Store:
    \"\"\"Models `webhook_events` as a dict keyed by its real composite
    PRIMARY KEY (id, created_at) -- the arbiter the task's ON CONFLICT
    DO NOTHING infers. Partition routing is by created_at, which the key
    already pins, so this is a faithful model of the guard.\"\"\"

    def __init__(self):
        self.rows = {}
        self.insert_attempts = 0
        self.conflicts = 0


store = _Store()


class _FakeSession:
    def __init__(self, store):
        self._store = store
        self.committed = False

    def execute(self, statement):
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        params = dict(compiled.params)
        assert sql.startswith("INSERT INTO webhook_events"), sql
        assert "ON CONFLICT" in sql and "DO NOTHING" in sql, sql
        self._store.insert_attempts += 1
        key = (params["id"], params["created_at"])
        if key in self._store.rows:
            self._store.conflicts += 1
            return None
        self._store.rows[key] = params
        return None

    def commit(self):
        self.committed = True


@contextmanager
def fake_get_session():
    yield _FakeSession(store)


tasks_webhooks.get_session = fake_get_session
tasks_webhooks.set_workspace_context = lambda session, workspace_id: None
# Redis dedup is best-effort and fails open by design -- force it open so
# the DB guard is what is actually under test.
tasks_webhooks._claim_dedup_key = lambda dedup_key: True

workspace_id = uuid.uuid4()
outbox_message_id = uuid.uuid4()
occurred_at = "2026-08-15T12:00:00+00:00"


def deliver():
    tasks_webhooks.create_webhook_event(
        workspace_id=str(workspace_id),
        event_type="scrape.job.completed",
        payload={"scrape_job_id": "j"},
        dedup_key="job:j:COMPLETED",
        event_id=str(outbox_message_id),
        occurred_at=occurred_at,
    )
"""


def test_webhook_event_replay_after_worker_loss_writes_exactly_one_row() -> None:
    _assert_ok(
        _run(
            _WEBHOOK_SETUP
            + """
# Delivery 1: the worker inserts the row, then is SIGKILLed before the
# broker is acked -- modelled by simply not acking and delivering again.
deliver()
if len(store.rows) != 1:
    print("FIRST_DELIVERY_DID_NOT_WRITE_ONE_ROW:" + str(len(store.rows)))
    sys.exit(1)

# Delivery 2 and 3: the same message, redelivered after the visibility
# timeout / worker-loss rejection.
deliver()
deliver()

if len(store.rows) != 1:
    print("REPLAY_DUPLICATED_THE_EVENT:" + str(len(store.rows)))
    sys.exit(1)
if store.insert_attempts != 3:
    print("EXPECTED_THREE_ATTEMPTS:" + str(store.insert_attempts))
    sys.exit(1)
if store.conflicts != 2:
    print("EXPECTED_TWO_CONFLICTS:" + str(store.conflicts))
    sys.exit(1)
print("OK")
"""
        )
    )


def test_webhook_event_replay_is_not_dependent_on_redis() -> None:
    """The DB guard must hold with the Redis dedup completely unavailable.

    Redis dedup fails open (its own docstring says so), so if it were the
    only guard, a Redis outage during a worker-loss storm would multiply
    every event.
    """
    _assert_ok(
        _run(
            _WEBHOOK_SETUP
            + """
def _redis_is_down(dedup_key):
    return True  # fail-open behaviour of the real helper


tasks_webhooks._claim_dedup_key = _redis_is_down

deliver()
deliver()

if len(store.rows) != 1:
    print("REDIS_DOWN_REPLAY_DUPLICATED:" + str(len(store.rows)))
    sys.exit(1)
print("OK")
"""
        )
    )


def test_webhook_event_without_a_deterministic_id_keeps_legacy_behaviour() -> None:
    """Legacy/direct callers (no `event_id`) still work — they simply get
    the old fresh-uuid semantics, where duplicates were always tolerated."""
    _assert_ok(
        _run(
            _WEBHOOK_SETUP
            + """
tasks_webhooks.create_webhook_event(
    workspace_id=str(workspace_id),
    event_type="scrape.job.completed",
    payload={"scrape_job_id": "j"},
)
tasks_webhooks.create_webhook_event(
    workspace_id=str(workspace_id),
    event_type="scrape.job.completed",
    payload={"scrape_job_id": "j"},
)

if len(store.rows) != 2:
    print("LEGACY_PATH_SHOULD_NOT_COLLAPSE:" + str(len(store.rows)))
    sys.exit(1)
print("OK")
"""
        )
    )


def test_producers_stamp_the_outbox_message_id_into_the_webhook_kwargs() -> None:
    """End-to-end plumbing check for the idempotency key.

    The dispatcher is generic — it publishes `payload` verbatim and knows
    nothing about any task's idempotency scheme. So the *producer* must
    mint the outbox row id and put it inside the kwargs as `event_id`
    (plus a stable `occurred_at`). If this plumbing broke, replays would
    silently start duplicating `webhook_events` rows again, because the
    consumer would fall back to a fresh uuid on every delivery.
    """
    _assert_ok(
        _run(
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
from app_shared.task_names import CREATE_WEBHOOK_EVENT

import app.workers.tasks_jobs as tasks_jobs

fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


recorded = []


def fake_write_outbox_message(session, *, workspace_id, task_name, queue,
                              kwargs=None, dedup_key=None, now=None,
                              message_id=None):
    recorded.append({"task_name": task_name, "kwargs": kwargs, "message_id": message_id})


tasks_jobs.get_session = fake_get_session
# The cross-tenant `_scan_job_refs` sweep runs on the BYPASSRLS system
# session (mushtryati F-1) -- point it at the same fake, so the scan
# still reads the seeded rows and no real engine is ever constructed.
tasks_jobs.get_system_session = fake_get_session
tasks_jobs.set_workspace_context = lambda session, workspace_id: None
tasks_jobs.write_outbox_message = fake_write_outbox_message

workspace_id = uuid.uuid4()
now = datetime(2026, 1, 1, tzinfo=timezone.utc)

job = ScrapeJob(
    workspace_id=workspace_id,
    scope=ScrapeScope.VARIANT,
    type=ScrapeJobType.MANUAL,
    source=ScrapeJobSource.API,
    status=ScrapeJobStatus.RUNNING,
    total_targets=1,
    created_at=now,
    started_at=now,
)
job.id = uuid.uuid4()
target = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=job.id,
    match_id=uuid.uuid4(),
    status=ScrapeTargetStatus.COMPLETED,
    created_at=now,
)
target.id = uuid.uuid4()
fake_session.seed(job, target)

tasks_jobs.finalize_jobs()

webhook = [r for r in recorded if r["task_name"] == CREATE_WEBHOOK_EVENT]
if len(webhook) != 1:
    print("EXPECTED_ONE_WEBHOOK_MESSAGE:" + str(len(webhook)))
    sys.exit(1)

entry = webhook[0]
if entry["message_id"] is None:
    print("NO_MESSAGE_ID_SUPPLIED")
    sys.exit(1)
if entry["kwargs"].get("event_id") != str(entry["message_id"]):
    print("EVENT_ID_NOT_THE_MESSAGE_ID:" + str(entry["kwargs"].get("event_id")))
    sys.exit(1)
if not entry["kwargs"].get("occurred_at"):
    print("NO_OCCURRED_AT")
    sys.exit(1)
# `occurred_at` must be parseable back into the aware datetime the
# consumer will use as the partition/PK column.
parsed = datetime.fromisoformat(entry["kwargs"]["occurred_at"])
if parsed.tzinfo is None:
    print("OCCURRED_AT_IS_NAIVE")
    sys.exit(1)
print("OK")
"""
        )
    )


# --- 2. finalize_jobs -------------------------------------------------------


def test_finalize_jobs_replay_after_worker_loss_is_a_no_op() -> None:
    _assert_ok(
        _run(
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

import app.workers.tasks_jobs as tasks_jobs

fake_session = FakeOrmSession()


@contextmanager
def fake_get_session():
    yield fake_session


messages = []


def fake_write_outbox_message(session, *, workspace_id, task_name, queue,
                              kwargs=None, dedup_key=None, now=None,
                              message_id=None):
    messages.append({"task_name": task_name, "dedup_key": dedup_key})


tasks_jobs.get_session = fake_get_session
# The cross-tenant `_scan_job_refs` sweep runs on the BYPASSRLS system
# session (mushtryati F-1) -- point it at the same fake, so the scan
# still reads the seeded rows and no real engine is ever constructed.
tasks_jobs.get_system_session = fake_get_session
tasks_jobs.set_workspace_context = lambda session, workspace_id: None
tasks_jobs.write_outbox_message = fake_write_outbox_message

workspace_id = uuid.uuid4()
now = datetime(2026, 1, 1, tzinfo=timezone.utc)

job = ScrapeJob(
    workspace_id=workspace_id,
    scope=ScrapeScope.VARIANT,
    type=ScrapeJobType.MANUAL,
    source=ScrapeJobSource.API,
    status=ScrapeJobStatus.RUNNING,
    total_targets=1,
    created_at=now,
    started_at=now,
)
job.id = uuid.uuid4()
target = ScrapeJobTarget(
    workspace_id=workspace_id,
    scrape_job_id=job.id,
    match_id=uuid.uuid4(),
    status=ScrapeTargetStatus.COMPLETED,
    created_at=now,
)
target.id = uuid.uuid4()
fake_session.seed(job, target)

# Delivery 1: finalizes the job and records its follow-up messages, then
# the worker dies before acking.
tasks_jobs.finalize_jobs()
first_status = job.status
first_completed_at = job.completed_at
first_messages = list(messages)

if first_status != ScrapeJobStatus.COMPLETED:
    print("NOT_FINALIZED:" + str(first_status))
    sys.exit(1)
if not first_messages:
    print("NO_FOLLOWUP_RECORDED")
    sys.exit(1)

# Delivery 2: the same task, redelivered.
tasks_jobs.finalize_jobs()

if job.status != first_status:
    print("REPLAY_CHANGED_STATUS:" + str(job.status))
    sys.exit(1)
if job.completed_at != first_completed_at:
    print("REPLAY_RESTAMPED_COMPLETED_AT")
    sys.exit(1)
if messages != first_messages:
    print("REPLAY_RECORDED_EXTRA_MESSAGES:" + str(messages))
    sys.exit(1)
print("OK")
"""
        )
    )


# --- 3. run_discovery (paid work) -------------------------------------------

_DISCOVERY_SETUP = """
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, "apps/workers")

from app_shared.enums import DiscoveryRunStatus
from app_shared.models.strategy import StrategyDiscoveryRun

import app.workers.tasks_strategy as tasks_strategy

workspace_id = uuid.uuid4()
competitor_id = uuid.uuid4()

run = StrategyDiscoveryRun(
    workspace_id=workspace_id,
    competitor_id=competitor_id,
    domain="example.com",
    url_pattern="/p/{id}",
    sample_size=3,
    status=DiscoveryRunStatus.RUNNING,
    created_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
)
run.id = uuid.uuid4()


class _FakeSession:
    def commit(self):
        pass

    def flush(self):
        pass


@contextmanager
def fake_get_session():
    yield _FakeSession()


probes = []


def exploding_probe(*args, **kwargs):
    probes.append(1)
    raise AssertionError("replay must not re-probe -- every probe costs money")


tasks_strategy.get_session = fake_get_session
tasks_strategy.set_workspace_context = lambda session, workspace_id: None
tasks_strategy._probe_sample = exploding_probe
"""


def test_discovery_replay_of_a_claimed_run_does_not_re_probe() -> None:
    """The expensive one: replaying a discovery must not buy a second
    sample of proxied fetches."""
    _assert_ok(
        _run(
            _DISCOVERY_SETUP
            + """
tasks_strategy.scoped_get = lambda session, model, run_id, ws: run

tasks_strategy.run_discovery(
    workspace_id=str(workspace_id),
    competitor_id=str(competitor_id),
    domain="example.com",
    url_pattern="/p/{id}",
    sample_urls=["https://example.com/p/1"],
    triggered_by="OPERATOR",
    run_id=str(run.id),
)

if probes:
    print("REPLAY_RE_PROBED:" + str(len(probes)))
    sys.exit(1)
if run.status != DiscoveryRunStatus.RUNNING:
    print("REPLAY_MUTATED_RUN:" + str(run.status))
    sys.exit(1)
print("OK")
"""
        )
    )


def test_discovery_replay_of_a_finished_run_does_not_re_probe() -> None:
    _assert_ok(
        _run(
            _DISCOVERY_SETUP
            + """
run.status = DiscoveryRunStatus.COMPLETED
tasks_strategy.scoped_get = lambda session, model, run_id, ws: run

tasks_strategy.run_discovery(
    workspace_id=str(workspace_id),
    competitor_id=str(competitor_id),
    domain="example.com",
    url_pattern="/p/{id}",
    sample_urls=["https://example.com/p/1"],
    triggered_by="OPERATOR",
    run_id=str(run.id),
)

if probes:
    print("REPLAY_RE_PROBED:" + str(len(probes)))
    sys.exit(1)
if run.status != DiscoveryRunStatus.COMPLETED:
    print("REPLAY_MUTATED_RUN:" + str(run.status))
    sys.exit(1)
print("OK")
"""
        )
    )


def test_auto_discovery_replay_is_suppressed_while_a_run_is_in_flight() -> None:
    """The AUTO trigger has no `run_id` to key on, so its guard is "is a
    recent RUNNING run already covering this (competitor, url_pattern)?".
    """
    _assert_ok(
        _run(
            _DISCOVERY_SETUP
            + """
seen = {}


def fake_auto_run_in_flight(session, *, workspace_id, competitor_id, url_pattern, now):
    seen["called"] = True
    return True


tasks_strategy._auto_run_in_flight = fake_auto_run_in_flight

tasks_strategy.run_discovery(
    workspace_id=str(workspace_id),
    competitor_id=str(competitor_id),
    domain="example.com",
    url_pattern="/p/{id}",
    sample_urls=["https://example.com/p/1"],
    triggered_by="AUTO",
)

if not seen.get("called"):
    print("AUTO_GUARD_NOT_CONSULTED")
    sys.exit(1)
if probes:
    print("REPLAY_RE_PROBED:" + str(len(probes)))
    sys.exit(1)
print("OK")
"""
        )
    )
