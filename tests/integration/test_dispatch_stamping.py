"""`stamp_targets_dispatched` unit tests (EPA B2, READY-002).

`apps/workers/app/workers/tasks_dispatch.py::stamp_targets_dispatched` —
fake session (`FakeOrmSession`, the same double
`test_jobs_dispatch_task.py`/`test_jobs_stall_recovery.py` use) + fake
Redis, driving the REAL, unchanged `DispatchIntentStore` (B1) and
`get_committed_dispatch` (B1). Never a real DB/Redis.

Per the plan (EPA B2): the state model stays `status=PENDING` +
`dispatched_at` — no `DISPATCHED` enum member is introduced.
`stamp_targets_dispatched` is the SINGLE writer of `dispatched_at` /
`dispatch_intent_id`, and stamps ONLY when a committed guard/intent
whose `identity_payload` matches the batch's exact `DispatchIdentity`
(EPA B1) exists — otherwise it raises `DispatchIntegrityError` and logs
`dispatch_integrity_error identity=... found=...` for alerting.

## Why this lives in `tests/integration/`

Matches the plan's file path. It is DB-free by design (no Postgres
connection is opened, `.env`/`get_settings()` are never touched) —
`FakeOrmSession` evaluates real SQLAlchemy `WHERE` clauses in memory, so
`DispatchIntentStore`'s actual `scoped_select`/`_load`/`plan`/`confirm`
code runs unmodified against seeded rows, exactly as
`test_jobs_dispatch_task.py` already does for `dispatch_job`.

## Why a subprocess

`apps/api/app` and `apps/workers/app` are both named `app`, so
`app.workers.tasks_dispatch` is only unambiguously importable in a
process that has not already resolved `app` to the other package —
exactly the reason `test_jobs_dispatch_task.py`/`test_dispatch_routing.py`/
`test_jobs_stall_recovery.py` each drive their task from a subprocess.
This file is collected in the SAME pytest run as those (and the rest of
`tests/unit`), so it follows the identical isolation pattern rather than
risking `sys.modules["app"]` having already been claimed.
"""

from __future__ import annotations

import os
import subprocess
import sys

# --- shared fixture setup, prepended to every check below ------------------

_FIXTURES = """
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

from _jobs_fake_session import FakeOrmSession
from app_shared.enums import (
    ScrapeErrorCode,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeProfileMode,
    ScrapeScope,
    ScrapeTargetStatus,
)
from app_shared.jobs.dispatch_intents import DispatchIntentStore
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.scrapyd.client import DispatchIntegrityError
from app_shared.scrapyd.identity import build_dispatch_identity

from app.workers.tasks_dispatch import DispatchedBatch, stamp_targets_dispatched


class FakeRedis:
    \"\"\"Only `.get` is exercised -- `stamp_targets_dispatched` never writes Redis.\"\"\"

    def __init__(self):
        self.store = {}

    def get(self, name):
        return self.store.get(name)

    def set(self, name, value, *, nx=False, ex=None):
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True

    def delete(self, *names):
        for name in names:
            self.store.pop(name, None)


fake_redis = FakeRedis()
session = FakeOrmSession()

workspace_id = uuid.uuid4()
job_id = uuid.uuid4()
match_a_id = uuid.uuid4()
match_b_id = uuid.uuid4()
now = datetime.now(timezone.utc)

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
session.seed(job)

# target_a: a plain in-flight PENDING row. target_b: a DEFERRED handback
# (SPEC-11) with an error_code set -- proves stamping flips it back to
# PENDING and clears the code, exactly like the pre-B2 inline loops did.
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
    status=ScrapeTargetStatus.DEFERRED,
    error_code=ScrapeErrorCode.TIMEOUT,
    created_at=now,
)
target_b.id = uuid.uuid4()
session.seed(target_a, target_b)

identity = build_dispatch_identity(
    scrape_job_id=str(job_id),
    planning_generation=1,
    strategy_method="css/v1",
    domain="example.com",
    mode=ScrapeProfileMode.HTTP,
    node_class="price_monitor:generic_price_spider",
    match_ids=[match_a_id, match_b_id],
)

planned_batch = DispatchedBatch(
    workspace_id=workspace_id,
    scrape_job_id=job_id,
    identity=identity,
    targets=[target_a, target_b],
)
"""

# --- Step 1, test 1: no guard/intent at all -> refuse -----------------------

_TEST_REQUIRES_MATCHING_GUARD = (
    _FIXTURES
    + """
try:
    stamp_targets_dispatched(session, fake_redis, batch=planned_batch)
except DispatchIntegrityError:
    pass
else:
    print("DID_NOT_RAISE")
    sys.exit(1)

if any(t.dispatched_at is not None for t in planned_batch.targets):
    print("TARGETS_STAMPED_WITHOUT_ANY_GUARD")
    sys.exit(1)
if any(t.dispatch_intent_id is not None for t in planned_batch.targets):
    print("INTENT_ID_SET_WITHOUT_ANY_GUARD")
    sys.exit(1)

print("OK")
sys.exit(0)
"""
)

# --- Step 1, test 2: a committed guard exists, but for a DIFFERENT --------
# identity (a stall-recovery replan's smaller subset) -> still refuse, and
# the new (unscheduled) batch must not be restamped off someone else's proof.

_TEST_RECOVERY_CANNOT_RESTAMP_UNSCHEDULED_BATCH = (
    _FIXTURES
    + """
stale_identity = build_dispatch_identity(
    scrape_job_id=str(job_id),
    planning_generation=0,
    strategy_method="css/v1",
    domain="example.com",
    mode=ScrapeProfileMode.HTTP,
    node_class="price_monitor:generic_price_spider",
    # A DIFFERENT (smaller, earlier-generation) subset than
    # `planned_batch.identity` -- e.g. the original batch before a stall
    # replan dropped a target that had already succeeded.
    match_ids=[match_a_id],
)
stale_store = DispatchIntentStore(session, workspace_id=workspace_id, scrape_job_id=job_id)
stale_store.plan(stale_identity, match_ids=[match_a_id])
stale_store.confirm(stale_identity, "job-stale-1")

try:
    stamp_targets_dispatched(session, fake_redis, batch=planned_batch)
except DispatchIntegrityError:
    pass
else:
    print("DID_NOT_RAISE")
    sys.exit(1)

if any(t.dispatched_at is not None for t in planned_batch.targets):
    print("TARGETS_STAMPED_OFF_A_STALE_GUARD")
    sys.exit(1)

print("OK")
sys.exit(0)
"""
)

# --- Step 1, test 3: a committed guard for the EXACT identity -> stamp -----

_TEST_STAMPING_RECORDS_TIMESTAMP_AND_INTENT_FK = (
    _FIXTURES
    + """
store = DispatchIntentStore(session, workspace_id=workspace_id, scrape_job_id=job_id)
committed_guard = store.plan(identity, match_ids=[match_a_id, match_b_id])
store.confirm(identity, "job-committed-1")

stamp_targets_dispatched(session, fake_redis, batch=planned_batch)

for t in planned_batch.targets:
    if t.status != ScrapeTargetStatus.PENDING:
        print("STATUS_NOT_PENDING:" + str(t.status))
        sys.exit(1)
    if t.dispatched_at is None:
        print("DISPATCHED_AT_NOT_SET")
        sys.exit(1)
    if t.dispatch_intent_id != committed_guard.intent_id:
        print(
            "INTENT_ID_MISMATCH:"
            + str(t.dispatch_intent_id)
            + " vs "
            + str(committed_guard.intent_id)
        )
        sys.exit(1)

if target_b.error_code is not None:
    print("ERROR_CODE_NOT_CLEARED_ON_DEFERRED_RESTAMP")
    sys.exit(1)

print("OK")
sys.exit(0)
"""
)

# --- Bonus: the durable `dispatch_intents` row alone (no Redis guard) is --
# also a sufficient proof -- the Redis-guard-expired-but-DB-confirmed path
# B1 exists for.

_TEST_DB_ONLY_COMMITTED_INTENT_IS_SUFFICIENT = (
    _FIXTURES
    + """
store = DispatchIntentStore(session, workspace_id=workspace_id, scrape_job_id=job_id)
committed_guard = store.plan(identity, match_ids=[match_a_id, match_b_id])
store.confirm(identity, "job-committed-2")

# No Redis guard written at all -- fake_redis.store stays empty.
if fake_redis.store:
    print("UNEXPECTED_REDIS_STATE")
    sys.exit(1)

stamp_targets_dispatched(session, fake_redis, batch=planned_batch)

for t in planned_batch.targets:
    if t.dispatched_at is None:
        print("DISPATCHED_AT_NOT_SET_FROM_DB_ONLY_INTENT")
        sys.exit(1)
    if t.dispatch_intent_id != committed_guard.intent_id:
        print("INTENT_ID_NOT_SET_FROM_DB_ONLY_INTENT")
        sys.exit(1)

print("OK")
sys.exit(0)
"""
)


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


def _run(script: str) -> None:
    """Run one fixture+assertions script in an isolated subprocess.

    `env` is built from `_ENV` merged over `os.environ` (mirroring
    `test_jobs_dispatch_task.py`/`test_jobs_stall_recovery.py` exactly)
    only so `app_shared.config.Settings` can construct if anything
    imports it transitively -- `stamp_targets_dispatched` itself never
    calls `get_settings()`/opens a DB, so `.env` is never consulted for
    anything this test actually depends on.
    """
    env = {**os.environ, **_ENV}
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


def test_stamping_requires_matching_guard() -> None:
    """No committed guard/intent anywhere -> `DispatchIntegrityError`, nothing stamped."""
    _run(_TEST_REQUIRES_MATCHING_GUARD)


def test_recovery_cannot_restamp_unscheduled_batch() -> None:
    """A guard committed for a DIFFERENT (stale, smaller-subset) identity
    must not authorize stamping the current, unscheduled batch."""
    _run(_TEST_RECOVERY_CANNOT_RESTAMP_UNSCHEDULED_BATCH)


def test_stamping_records_timestamp_and_intent_fk() -> None:
    """A committed guard for the EXACT identity -> `dispatched_at` +
    `dispatch_intent_id` set, `status` stays PENDING (no DISPATCHED enum),
    and a DEFERRED target flips back to PENDING with its error cleared."""
    _run(_TEST_STAMPING_RECORDS_TIMESTAMP_AND_INTENT_FK)


def test_db_only_committed_intent_is_sufficient() -> None:
    """A CONFIRMED `dispatch_intents` row with no Redis guard at all is
    still sufficient proof (the Redis-guard-expired-but-DB-confirmed path
    B1 exists for)."""
    _run(_TEST_DB_ONLY_COMMITTED_INTENT_IS_SUFFICIENT)
