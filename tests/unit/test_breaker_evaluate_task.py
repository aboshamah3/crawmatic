"""`maintenance.breaker_evaluate` — the durable evaluator task (EPA B1).

The deadlock this task closes: the cost gate denies ALL paid work once
`proxy_circuit_breakers.evaluated_at` is older than
`DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS`, and the only evaluator that
existed before this one ran *inside* the scraping path that the denial
blocks. So the two properties that actually matter here are behavioural,
not cosmetic:

1. when the breaker is ENABLED the task really runs an evaluation and
   commits it (otherwise the evidence still rots and the deadlock is
   intact);
2. when the breaker is DISABLED it touches nothing at all — a disabled
   breaker has no gate to unblock, and writing to a row an operator has
   deliberately switched off would be a surprise.

Subprocess-loaded because `apps/workers` ships its own top-level `app`
package (same idiom as `test_maintenance_task_loudness.py`).
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
sys.path.insert(0, "apps/workers")

from contextlib import contextmanager
from unittest.mock import MagicMock

from app.workers import tasks_maintenance
from app_shared.maintenance.scoping import MaintenanceScope, maintenance_scope_of
from app_shared.task_names import MAINTENANCE_BREAKER_EVALUATE


def _install(*, enabled=True, interval=300, auto_close=3600):
    session = MagicMock(name="session")

    @contextmanager
    def _fake_system_session(task_name):
        assert task_name == "breaker_evaluate", task_name
        yield session

    settings = MagicMock(
        PROXY_BREAKER_ENABLED=enabled,
        PROXY_BREAKER_EVAL_INTERVAL_SECONDS=interval,
        PROXY_BREAKER_AUTO_CLOSE_AFTER_SECONDS=auto_close,
    )
    evaluate = MagicMock(name="evaluate_and_persist", return_value=None)

    tasks_maintenance._system_session = _fake_system_session
    tasks_maintenance.get_settings = lambda: settings
    tasks_maintenance.evaluate_and_persist = evaluate
    tasks_maintenance.thresholds_from_settings = lambda s: "THR"
    return session, evaluate
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


def test_enabled_breaker_is_evaluated_and_the_verdict_is_committed() -> None:
    """The whole point: an evaluation happens on the scheduler's cadence,
    with the evaluator's own lease interval, and is COMMITTED — an
    uncommitted `evaluated_at` refreshes nothing and the gate keeps
    denying."""
    _assert_ok(
        _run(
            """
session, evaluate = _install(interval=300)

tasks_maintenance.breaker_evaluate()

assert evaluate.call_count == 1, evaluate.call_args_list
args, kwargs = evaluate.call_args
assert args == (session,), args
assert kwargs == {
    "thresholds": "THR",
    "min_interval_seconds": 300,
    # EPA B1 second half: this task is the ONLY evaluator that can close
    # an open breaker, so it must be the one carrying the cooldown.
    "auto_close_after_seconds": 3600,
}, kwargs
assert session.commit.call_count == 1, session.commit.call_args_list
print("OK")
"""
        )
    )


def test_disabled_breaker_evaluates_nothing() -> None:
    """`PROXY_BREAKER_ENABLED=false` is an operator switching the whole
    subsystem off. The cadence still ticks, so the task must return
    without opening a session or writing a row."""
    _assert_ok(
        _run(
            """
session, evaluate = _install(enabled=False)

tasks_maintenance.breaker_evaluate()

assert evaluate.call_count == 0, evaluate.call_args_list
assert session.commit.call_count == 0, session.commit.call_args_list
print("OK")
"""
        )
    )


def test_task_is_registered_under_its_name_and_declares_a_fleet_scope() -> None:
    """Same two-part declaration every other maintenance task carries: the
    Celery name the scheduler enqueues, and the FLEET scope
    `register_maintenance_tasks` refuses to accept a task without."""
    _assert_ok(
        _run(
            """
assert tasks_maintenance.breaker_evaluate.name == MAINTENANCE_BREAKER_EVALUATE
assert (
    maintenance_scope_of(tasks_maintenance.breaker_evaluate) is MaintenanceScope.FLEET
)
print("OK")
"""
        )
    )
