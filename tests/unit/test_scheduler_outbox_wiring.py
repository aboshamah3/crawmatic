"""Scheduler wiring for the outbox passes (2026-08-15 audit risk H1).

The outbox is only durable if something actually drains it. These tests
pin that the scheduler loop enqueues `OUTBOX_DRAIN` and `OUTBOX_RECONCILE`
alongside the existing finalize / recover-stalled / redispatch
maintenance passes, on their own accumulators, and that a broker error at
either seam is logged and swallowed rather than crash-looping the
process — which is *safe* here in a way it never was for the old
producer seams: the messages are already committed in Postgres, so a
missed tick delays delivery, it cannot lose it.

Loaded in a fresh subprocess (mirrors `test_webhook_enqueue_seams.py` /
`test_jobs_dispatch_task.py`) because `apps/api`, `apps/workers` and
`apps/scheduler` each ship their own top-level `app` package, so
`app.scheduler` is unimportable in a shared test process once another
module has bound `app` to a different tree.
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
from app_shared.task_names import OUTBOX_DRAIN, OUTBOX_RECONCILE


class _RecordingEnqueue:
    def __init__(self, raise_on=None):
        self.calls = []
        self.raise_on = raise_on or set()

    def __call__(self, name, *, queue, kwargs=None):
        self.calls.append((name, queue))
        if name in self.raise_on:
            raise RuntimeError("simulated broker outage")
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


def test_drain_and_reconcile_are_enqueued_on_the_maintenance_queue() -> None:
    _assert_ok(
        _run(
            """
enqueue = _RecordingEnqueue()
scheduler_app.enqueue = enqueue

scheduler_app._enqueue_outbox_drain()
scheduler_app._enqueue_outbox_reconcile()

assert enqueue.calls == [
    (OUTBOX_DRAIN, "maintenance"),
    (OUTBOX_RECONCILE, "maintenance"),
], enqueue.calls
print("OK")
"""
        )
    )


def test_broker_error_at_either_seam_never_crashes_the_scheduler() -> None:
    """A missed tick delays delivery; it cannot lose it — the messages are
    already committed in Postgres, so swallowing here is safe by
    construction, not merely tolerable."""
    _assert_ok(
        _run(
            """
enqueue = _RecordingEnqueue(raise_on={OUTBOX_DRAIN, OUTBOX_RECONCILE})
scheduler_app.enqueue = enqueue

scheduler_app._enqueue_outbox_drain()      # must not raise
scheduler_app._enqueue_outbox_reconcile()  # must not raise

assert len(enqueue.calls) == 2, enqueue.calls
print("OK")
"""
        )
    )


def test_main_loop_ticks_both_outbox_accumulators() -> None:
    """The seams must actually be reached by the loop, on their own
    interval accumulators — not merely defined."""
    _assert_ok(
        _run(
            """
import inspect

source = inspect.getsource(scheduler_app.main)
for fragment in (
    "outbox_drain_elapsed",
    "outbox_reconcile_elapsed",
    "_enqueue_outbox_drain()",
    "_enqueue_outbox_reconcile()",
    "OUTBOX_DRAIN_INTERVAL_SECONDS",
    "OUTBOX_RECONCILE_INTERVAL_SECONDS",
):
    assert fragment in source, fragment
print("OK")
"""
        )
    )


def test_drain_runs_far_more_often_than_the_maintenance_tick() -> None:
    """The drain interval IS the worst-case added latency between a
    producer's COMMIT and its follow-up task reaching the broker, so it
    must be much tighter than the 60s maintenance cadence."""
    _assert_ok(
        _run(
            """
from app_shared.config import Settings

fields = Settings.model_fields
drain = fields["OUTBOX_DRAIN_INTERVAL_SECONDS"].default
reconcile = fields["OUTBOX_RECONCILE_INTERVAL_SECONDS"].default
maintenance = fields["STRATEGY_STATS_FLUSH_INTERVAL_SECONDS"].default

assert drain < maintenance, (drain, maintenance)
assert reconcile > drain, (reconcile, drain)
print("OK")
"""
        )
    )
