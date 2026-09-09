"""Every long-lived process class actually emits a heartbeat (EPA B9, F22;
phase-B review findings 1 and 2).

`PeriodicHeartbeat` (`tests/unit/test_heartbeat_periodic.py`) is only the
mechanism. This file asserts the *wiring*: that the Celery worker pools
and the scheduler each start one, with the right `service` name and the
right instance identity, and that a Redis failure at that seam degrades
to "no heartbeat" instead of "no worker" / "no scheduler".

Why this matters beyond the criterion: `apps/api/app/routers/ready.py`
turns `READY_REQUIRED_HEARTBEAT_SERVICES` into a hard `/ready` gate, and
`docs/ops/RELEASE_1_2026-09.md` sets it to `scheduler,worker` at release.
A declared service that nothing emits for is a permanent 503, so these
two emitters are what make that variable safe to set.

Loaded in fresh subprocesses (the `test_celery_time_limits.py` /
`test_fire_refresh_rule_predicate.py` idiom): `apps/api`, `apps/scheduler`
and `apps/workers` each ship their own top-level `app` package, so a
plain `import app.workers.celery_app` in the shared test process resolves
ambiguously to whichever `app` another test module imported first.
`celery_app.py` and `scheduler_app.py` also both call `get_settings()`
eagerly, hence the `_ENV` below.
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

# A recording stand-in for `PeriodicHeartbeat` plus a fake Redis client,
# shared by both halves. Patching the *class* on the module under test
# (rather than driving a real thread) keeps these tests deterministic:
# what is under test is which emitter gets constructed, not the timing
# loop `test_heartbeat_periodic.py` already covers.
_FAKES = '''
class FakeRedis:
    def __init__(self):
        self.calls = []

    def incr(self, key):
        self.calls.append(("incr", key))
        return 7

    def set(self, key, value, ex=None, **kw):
        self.calls.append(("set", key))
        return True


class RecordingPeriodic:
    """Stands in for `PeriodicHeartbeat`; records, never spawns a thread."""

    started = []
    stopped = []

    def __init__(self, emitter, **kwargs):
        self.emitter = emitter

    def start(self):
        RecordingPeriodic.started.append(self.emitter)
        return self

    def stop(self, **kwargs):
        RecordingPeriodic.stopped.append(self.emitter)


class Boom:
    """A `get_redis_client` that fails the way a Redis outage at boot does."""

    def __call__(self, *a, **kw):
        raise RuntimeError("redis unreachable")
'''

_WORKER_SETUP = (
    '''
import sys
sys.path.insert(0, "apps/workers")

from app.workers import celery_app as mod
'''
    + _FAKES
    + '''
mod.PeriodicHeartbeat = RecordingPeriodic
mod.get_redis_client = lambda: FakeRedis()


class FakeConsumer:
    """Celery hands `worker_ready` the pool's own Consumer; `hostname` is
    the `-n critical@%h` / `-n bulk@%h` node name from `start.sh`."""

    def __init__(self, hostname):
        self.hostname = hostname
'''
)

_SCHEDULER_SETUP = (
    '''
import sys
sys.path.insert(0, "apps/scheduler")

from app.scheduler import scheduler_app as mod
'''
    + _FAKES
    + '''
import app_shared.redis_client as redis_client_mod

mod.PeriodicHeartbeat = RecordingPeriodic
redis_client_mod.get_redis_client = lambda: FakeRedis()
'''
)


def _run(setup: str, body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", setup + body],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **_ENV},
    )


def _assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")


# --------------------------------------------------------------------------
# Finding 1 — Celery worker pools
# --------------------------------------------------------------------------


def test_the_signals_themselves_start_and_stop_the_heartbeat() -> None:
    """Driven through Celery's real `worker_ready`/`worker_shutdown`
    dispatch, not by calling the receivers directly: a receiver that is
    defined but never `.connect`ed would pass every other test in this
    file and still leave the pools silent in production."""
    _assert_ok(
        _run(
            _WORKER_SETUP,
            """
from celery.signals import worker_ready, worker_shutdown

mod._worker_heartbeat = None
worker_ready.send(sender=FakeConsumer("critical@box-1"))

assert len(RecordingPeriodic.started) == 1, RecordingPeriodic.started
assert RecordingPeriodic.started[0].service == "worker"
assert RecordingPeriodic.started[0].instance_id == "critical@box-1"

worker_shutdown.send(sender=object())
assert len(RecordingPeriodic.stopped) == 1, RecordingPeriodic.stopped
assert mod._worker_heartbeat is None
print("OK")
""",
        )
    )


def test_each_pool_gets_its_own_worker_heartbeat_under_its_celery_node_name() -> None:
    """`critical@` and `bulk@` are separate processes: each starts exactly
    one emitter, `service="worker"`, keyed by its own `-n` node name."""
    _assert_ok(
        _run(
            _WORKER_SETUP,
            """
for node in ("critical@box-1", "bulk@box-1"):
    mod._worker_heartbeat = None
    mod._start_worker_pool_heartbeat(sender=FakeConsumer(node))

assert len(RecordingPeriodic.started) == 2, RecordingPeriodic.started
services = [e.service for e in RecordingPeriodic.started]
instances = [e.instance_id for e in RecordingPeriodic.started]
assert services == ["worker", "worker"], services
assert instances == ["critical@box-1", "bulk@box-1"], instances

# The key `/ready` aggregates on must differ per pool, or a dead bulk@
# would be masked by a healthy critical@.
from app_shared.heartbeat import heartbeat_key

keys = {heartbeat_key("worker", i) for i in instances}
assert keys == {"heartbeat:worker:critical@box-1", "heartbeat:worker:bulk@box-1"}, keys
print("OK")
""",
        )
    )


def test_worker_heartbeat_start_is_idempotent_and_shutdown_stops_it() -> None:
    _assert_ok(
        _run(
            _WORKER_SETUP,
            """
mod._worker_heartbeat = None
mod._start_worker_pool_heartbeat(sender=FakeConsumer("critical@box-1"))
mod._start_worker_pool_heartbeat(sender=FakeConsumer("critical@box-1"))
assert len(RecordingPeriodic.started) == 1, RecordingPeriodic.started
assert mod._worker_heartbeat is not None

mod._stop_worker_pool_heartbeat()
assert len(RecordingPeriodic.stopped) == 1, RecordingPeriodic.stopped
assert mod._worker_heartbeat is None
# Stopping twice (or with nothing running) must not raise.
mod._stop_worker_pool_heartbeat()
assert len(RecordingPeriodic.stopped) == 1
print("OK")
""",
        )
    )


def test_a_redis_failure_does_not_stop_the_worker_from_starting() -> None:
    """A monitoring outage must never become a processing outage."""
    _assert_ok(
        _run(
            _WORKER_SETUP,
            """
mod.get_redis_client = Boom()
mod._worker_heartbeat = None

mod._start_worker_pool_heartbeat(sender=FakeConsumer("critical@box-1"))  # must not raise

assert RecordingPeriodic.started == [], RecordingPeriodic.started
assert mod._worker_heartbeat is None
# ... and the shutdown receiver still copes with never having started.
mod._stop_worker_pool_heartbeat()
print("OK")
""",
        )
    )


def test_worker_heartbeat_falls_back_to_the_default_instance_id() -> None:
    """A sender without a `hostname` (a Celery version change, an
    embedded worker) must still beat, not crash the boot."""
    _assert_ok(
        _run(
            _WORKER_SETUP,
            """
mod._worker_heartbeat = None
mod._start_worker_pool_heartbeat(sender=object())

assert len(RecordingPeriodic.started) == 1, RecordingPeriodic.started
emitter = RecordingPeriodic.started[0]
assert emitter.service == "worker"
assert emitter.instance_id, "instance id must never be empty"
print("OK")
""",
        )
    )


# --------------------------------------------------------------------------
# Finding 2 — the scheduler process
# --------------------------------------------------------------------------


def test_scheduler_starts_a_scheduler_heartbeat() -> None:
    _assert_ok(
        _run(
            _SCHEDULER_SETUP,
            """
handle = mod._start_scheduler_heartbeat()

assert handle is not None
assert len(RecordingPeriodic.started) == 1, RecordingPeriodic.started
emitter = RecordingPeriodic.started[0]
assert emitter.service == "scheduler", emitter.service
assert emitter.instance_id, "instance id must never be empty"

from app_shared.heartbeat import heartbeat_key

assert heartbeat_key("scheduler", emitter.instance_id).startswith("heartbeat:scheduler:")
print("OK")
""",
        )
    )


def test_a_redis_failure_does_not_stop_the_scheduler_from_starting() -> None:
    _assert_ok(
        _run(
            _SCHEDULER_SETUP,
            """
redis_client_mod.get_redis_client = Boom()

handle = mod._start_scheduler_heartbeat()  # must not raise

assert handle is None
assert RecordingPeriodic.started == [], RecordingPeriodic.started
print("OK")
""",
        )
    )


def test_scheduler_main_starts_the_heartbeat_before_any_pass_and_stops_it_at_exit() -> None:
    """Ordering is the point: the beat must precede the boot-time cadence
    and health passes, so a scheduler wedged in one of them still reads as
    alive; and a graceful shutdown stops it rather than waiting out a TTL."""
    _assert_ok(
        _run(
            _SCHEDULER_SETUP,
            """
order = []

mod._start_scheduler_heartbeat = lambda: (order.append("heartbeat"), Handle())[1]
mod._run_durable_cadence_tick = lambda *a, **kw: order.append("cadence")
mod._run_health_tick = lambda *a, **kw: order.append("health")
mod.assert_production_safe = lambda *a, **kw: None
mod.signal.signal = lambda *a, **kw: None


class Handle:
    def stop(self, **kw):
        order.append("stop")


# The loop is entered only while `_shutdown_requested` is False; setting
# it True makes `main()` fall straight through to the shutdown path.
mod._shutdown_requested = True
mod.main()

assert order == ["heartbeat", "cadence", "health", "stop"], order
print("OK")
""",
        )
    )
