"""Scheduler cadence for the ops snapshot + alert rules (audit §H5).

`app_shared.opsmetrics` shipped complete — a collector, ~40 alert rules,
and an emitter that writes one `ops.snapshot` line plus one `ops.alert`
line per firing rule — wired to exactly one caller: the on-demand
`GET /ops/metrics` endpoint. Nothing evaluated the rules on a schedule,
so every alert could only fire while an operator was already staring at
the dashboard. `test_main_loop_ticks_the_ops_snapshot_accumulator` fails
by construction against that code.

These tests pin the tick, its cadence knob, the fleet-scoped (BYPASSRLS)
session it must use, its read-only posture, and — most importantly — that
a failure anywhere in it is swallowed. An observability probe that can
crash the scheduler is worse than no probe: it would take the partition
cadence, the outbox drain and the refresh pass down with it.

Loaded in a fresh subprocess (mirrors `test_scheduler_durable_cadence.py`
/ `test_scheduler_outbox_wiring.py`) because `apps/api`, `apps/workers`
and `apps/scheduler` each ship their own top-level `app` package.
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

_SETUP = """
import sys
sys.path.insert(0, "apps/scheduler")

from app.scheduler import scheduler_app
from app_shared.config import get_settings


class _FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Recorder:
    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result
        self.raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result


def _install(*, collect_raises=None, emit_raises=None, alerts=None):
    \"\"\"Wire the ops tick onto a fake system session + fake collector/emitter.\"\"\"
    session = _FakeSession()
    scheduler_app.get_system_sessionmaker = lambda: (lambda: session)
    collect = _Recorder(result="SNAPSHOT", raises=collect_raises)
    emit = _Recorder(result=alerts if alerts is not None else [], raises=emit_raises)
    scheduler_app.collect_snapshot = collect
    scheduler_app.emit_snapshot = emit
    scheduler_app._ops_snapshot_redis = lambda: "REDIS"
    return session, collect, emit
"""


def _run(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _SETUP + body],
        capture_output=True,
        text=True,
        timeout=90,
        env={**os.environ, **_ENV},
    )


def _assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")


def test_tick_collects_a_snapshot_and_emits_it() -> None:
    """The one thing the tick exists to do: `emit_snapshot(collect_snapshot(...))`."""
    _assert_ok(
        _run(
            """
session, collect, emit = _install()

scheduler_app._run_ops_snapshot_tick(get_settings())

assert len(collect.calls) == 1, collect.calls
assert collect.calls[0][0][0] is session, collect.calls
assert emit.calls == [(("SNAPSHOT",), {})], emit.calls
print("OK")
"""
        )
    )


def test_tick_uses_the_bypassrls_system_session() -> None:
    """Fleet aggregates. A workspace-scoped session would silently
    under-report every metric — spend most damagingly — which is worse
    than not collecting at all, because the numbers still look real."""
    _assert_ok(
        _run(
            """
import inspect

source = inspect.getsource(scheduler_app._run_ops_snapshot_tick)
assert "get_system_sessionmaker()" in source, source
print("OK")
"""
        )
    )


def test_tick_is_read_only_and_never_commits() -> None:
    _assert_ok(
        _run(
            """
session, collect, emit = _install()

scheduler_app._run_ops_snapshot_tick(get_settings())

assert session.commits == 0, session.commits
assert session.rollbacks == 1, session.rollbacks
assert session.closed is True
print("OK")
"""
        )
    )


def test_tick_passes_settings_and_a_redis_client_to_the_collector() -> None:
    """Both are optional collector arguments that gate real signal:
    `settings` carries the breaker's configured ceiling, and without a
    redis client the `redis.evictions` / `redis.memory` rules have no
    counters to fire on."""
    _assert_ok(
        _run(
            """
settings = get_settings()
session, collect, emit = _install()

scheduler_app._run_ops_snapshot_tick(settings)

kwargs = collect.calls[0][1]
assert kwargs["settings"] is settings, kwargs
assert kwargs["redis"] == "REDIS", kwargs
assert kwargs["now"] is not None, kwargs
print("OK")
"""
        )
    )


def test_a_collector_failure_never_crashes_the_scheduler() -> None:
    """The tick shares a process with the partition cadence, the outbox
    drain and the refresh pass. An observability probe that can take
    those down is worse than no probe."""
    _assert_ok(
        _run(
            """
session, collect, emit = _install(collect_raises=RuntimeError("catalog is on fire"))

scheduler_app._run_ops_snapshot_tick(get_settings())  # must not raise

assert emit.calls == [], emit.calls
print("OK")
"""
        )
    )


def test_an_emit_failure_never_crashes_the_scheduler() -> None:
    _assert_ok(
        _run(
            """
session, collect, emit = _install(emit_raises=RuntimeError("log drain refused"))

scheduler_app._run_ops_snapshot_tick(get_settings())  # must not raise
print("OK")
"""
        )
    )


def test_an_unreachable_database_never_crashes_the_scheduler() -> None:
    _assert_ok(
        _run(
            """
def _boom():
    raise RuntimeError("SYSTEM_DATABASE_URL (or its AUTH_DATABASE_URL fallback) is required")

scheduler_app.get_system_sessionmaker = _boom

scheduler_app._run_ops_snapshot_tick(get_settings())  # must not raise
print("OK")
"""
        )
    )


def test_a_redis_client_that_refuses_degrades_to_none() -> None:
    """`get_redis_client()` runs the one-shot `maxmemory-policy` probe on
    first use and RAISES on an eviction-capable server — precisely the
    deployment state the snapshot exists to report. Letting that escape
    would turn the monitor into a second outage."""
    _assert_ok(
        _run(
            """
import app_shared.redis_client as redis_client

def _boom():
    raise RuntimeError("maxmemory-policy is allkeys-lru")

redis_client.get_redis_client = _boom

assert scheduler_app._ops_snapshot_redis() is None
print("OK")
"""
        )
    )


def test_main_loop_ticks_the_ops_snapshot_accumulator() -> None:
    """THE regression guard. Before this change nothing called the
    collector or the emitter on a schedule, so the ~40 alert rules could
    only fire while a human was already looking at `/ops/metrics`."""
    _assert_ok(
        _run(
            """
import inspect

source = inspect.getsource(scheduler_app.main)
for fragment in (
    "ops_snapshot_elapsed",
    "_run_ops_snapshot_tick(settings)",
    "OPS_SNAPSHOT_INTERVAL_SECONDS",
):
    assert fragment in source, fragment
print("OK")
"""
        )
    )


def test_snapshot_cadence_is_a_quarter_hour_and_settings_driven() -> None:
    """Read from `Settings` like every neighbouring interval, so a
    deployment can retune it without a rebuild — and tight enough to be
    alerting rather than archaeology."""
    _assert_ok(
        _run(
            """
from app_shared.config import Settings

fields = Settings.model_fields
snapshot = fields["OPS_SNAPSHOT_INTERVAL_SECONDS"].default
health = fields["MAINTENANCE_HEALTH_INTERVAL_SECONDS"].default
maintenance = fields["STRATEGY_STATS_FLUSH_INTERVAL_SECONDS"].default

assert snapshot == 900, snapshot
# Between the 60s maintenance tick and the hourly health assertions: the
# collector is far too heavy for the former and the rules move far faster
# than the latter.
assert maintenance < snapshot < health, (maintenance, snapshot, health)
print("OK")
"""
        )
    )


def test_boot_does_not_pay_for_a_snapshot_scan() -> None:
    """Unlike the cadence/health passes, the snapshot is deliberately not
    run before the loop: it is the heaviest read in the process and
    nothing it reports is more urgent in the first quarter-hour after a
    deploy — a crash-looping scheduler would otherwise pay that scan per
    boot, exactly when the database is least likely to be healthy."""
    _assert_ok(
        _run(
            """
import inspect

source = inspect.getsource(scheduler_app.main)
boot = source.split("while not _shutdown_requested")[0]
assert "_run_ops_snapshot_tick(settings)" not in boot, boot
print("OK")
"""
        )
    )
