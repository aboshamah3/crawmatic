"""Scheduler wiring for the durable daily cadences (2026-08-15 readiness cycle).

The defect: `main()` drove `partition_create` / `daily_rollup` /
`retention_drop` off in-process float accumulators re-initialised to
``0.0`` on every process start, against an 86400s interval. On Railway a
deploy is a restart, so those three tasks could go — and did go —
arbitrarily long without firing. `test_main_no_longer_uses_in_process_
accumulators_for_daily_cadences` fails against that code by construction.

Loaded in a fresh subprocess (mirrors `test_scheduler_outbox_wiring.py`)
because `apps/api`, `apps/workers` and `apps/scheduler` each ship their
own top-level `app` package.
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

from contextlib import contextmanager

from app.scheduler import scheduler_app
from app_shared.config import get_settings
from app_shared.task_names import (
    MAINTENANCE_DAILY_ROLLUP,
    MAINTENANCE_PARTITION_CREATE,
    MAINTENANCE_RETENTION_DROP,
)
from app_shared.models.maintenance_cadence import (
    CADENCE_DAILY_ROLLUP,
    CADENCE_PARTITION_CREATE,
    CADENCE_RETENTION_DROP,
)


class _RecordingEnqueue:
    def __init__(self):
        self.calls = []

    def __call__(self, name, *, queue, kwargs=None):
        self.calls.append((name, queue))


class _FakeSession:
    def __init__(self):
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _install(claim_keys, session=None):
    \"\"\"Wire scheduler_app onto a fake system session + a claim function
    that only grants `claim_keys`.\"\"\"
    session = session or _FakeSession()
    scheduler_app.get_system_sessionmaker = lambda: (lambda: session)
    scheduler_app.ensure_cadence_rows = lambda s: None
    scheduler_app.claim_cadence = (
        lambda s, key, *, interval_seconds, now=None: key in claim_keys
    )
    enqueue = _RecordingEnqueue()
    scheduler_app.enqueue = enqueue
    return enqueue, session
"""


def _run(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _SETUP + body],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **_ENV},
    )


def _assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")


def test_main_no_longer_uses_in_process_accumulators_for_daily_cadences() -> None:
    """THE regression guard. A float that resets on process start must
    never again be the only thing standing between the calendar and a
    total write outage. Fails against the pre-fix `main()`."""
    _assert_ok(
        _run(
            """
import inspect

source = inspect.getsource(scheduler_app.main)
for banned in (
    "partition_create_elapsed",
    "daily_rollup_elapsed",
    "retention_elapsed",
):
    assert banned not in source, banned

for required in (
    "_run_durable_cadence_tick(settings)",
    "_run_health_tick(settings)",
    "MAINTENANCE_CADENCE_POLL_INTERVAL_SECONDS",
    "MAINTENANCE_HEALTH_INTERVAL_SECONDS",
):
    assert required in source, required
print("OK")
"""
        )
    )


def test_boot_runs_the_cadence_and_health_passes_before_the_loop() -> None:
    """An overdue cadence must fire promptly on boot, not one poll
    interval later — and a partition gap must be reported at once."""
    _assert_ok(
        _run(
            """
import inspect

source = inspect.getsource(scheduler_app.main)
boot = source.split("while not _shutdown_requested")[0]
assert "_run_durable_cadence_tick(settings)" in boot
assert "_run_health_tick(settings)" in boot
print("OK")
"""
        )
    )


def test_durable_cadences_cover_all_three_daily_tasks() -> None:
    _assert_ok(
        _run(
            """
keys = [c[0] for c in scheduler_app._DURABLE_CADENCES]
attrs = [c[1] for c in scheduler_app._DURABLE_CADENCES]

assert keys == [CADENCE_PARTITION_CREATE, CADENCE_DAILY_ROLLUP, CADENCE_RETENTION_DROP], keys
assert attrs == [
    "PARTITION_CREATE_INTERVAL_SECONDS",
    "DAILY_ROLLUP_INTERVAL_SECONDS",
    "RETENTION_INTERVAL_SECONDS",
], attrs
print("OK")
"""
        )
    )


def test_tick_enqueues_only_the_cadences_it_actually_claimed() -> None:
    _assert_ok(
        _run(
            """
enqueue, session = _install({CADENCE_PARTITION_CREATE, CADENCE_RETENTION_DROP})

claimed = scheduler_app._run_durable_cadence_tick(get_settings())

assert claimed == [CADENCE_PARTITION_CREATE, CADENCE_RETENTION_DROP], claimed
assert enqueue.calls == [
    (MAINTENANCE_PARTITION_CREATE, "maintenance"),
    (MAINTENANCE_RETENTION_DROP, "maintenance"),
], enqueue.calls
print("OK")
"""
        )
    )


def test_tick_enqueues_nothing_when_no_cadence_is_due() -> None:
    """The steady state: a scheduler polling every 60s against three
    daily deadlines must sit silent, not spam the maintenance queue."""
    _assert_ok(
        _run(
            """
enqueue, session = _install(set())

claimed = scheduler_app._run_durable_cadence_tick(get_settings())

assert claimed == [], claimed
assert enqueue.calls == [], enqueue.calls
print("OK")
"""
        )
    )


def test_a_database_failure_in_the_tick_never_crashes_the_scheduler() -> None:
    """A failed tick is retried on the next poll — and, unlike the
    accumulator design, it no longer LOSES the cadence: the deadline is
    still sitting unclaimed in Postgres."""
    _assert_ok(
        _run(
            """
def _boom():
    raise RuntimeError("SYSTEM_DATABASE_URL is required")

scheduler_app.get_system_sessionmaker = _boom

claimed = scheduler_app._run_durable_cadence_tick(get_settings())  # must not raise
assert claimed == [], claimed

scheduler_app._run_health_tick(get_settings())  # must not raise either
print("OK")
"""
        )
    )


def test_partition_lookahead_gives_months_of_margin_not_days() -> None:
    """With a lookahead of 1 the entire margin between "maintenance
    stopped" and "every INSERT fails" is whatever is left of the current
    month. The whole point of the fix is that the margin outlives a slow
    detection cycle."""
    _assert_ok(
        _run(
            """
from app_shared.config import Settings

lookahead = Settings.model_fields["PARTITION_CREATE_LOOKAHEAD_MONTHS"].default
assert lookahead >= 3, lookahead

poll = Settings.model_fields["MAINTENANCE_CADENCE_POLL_INTERVAL_SECONDS"].default
daily = Settings.model_fields["PARTITION_CREATE_INTERVAL_SECONDS"].default
assert poll < daily, (poll, daily)
print("OK")
"""
        )
    )
