"""`maintenance.fleet_budget_rollforward` — the budget-policy writer task (EPA A4/B3).

The gap this task closes is DATED. `fleet_cost_budgets` is the only
fleet-wide money ceiling there is, and it was seeded by one operator run
covering a fixed number of months ending `2026_10`. A budget row is born
with `NULL` limits, so the first paid dispatch of the next month creates
an uncapped row and the ceiling is gone — silently, with the provider
bill as the only symptom.

So the properties that matter here are behavioural:

1. the task really rolls the caps forward, with BOTH configured caps and
   a month of lookahead, and COMMITS (an uncommitted cap caps nothing);
2. an uncapped `(scope, period)` pair is announced at ERROR — that log
   line is the only signal an operator gets that the fleet is spending
   with no ceiling, so a healthy-looking INFO there would be worse than
   no logging at all.

Subprocess-loaded because `apps/workers` ships its own top-level `app`
package (same idiom as `test_breaker_evaluate_task.py`).
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

import logging
from contextlib import contextmanager
from unittest.mock import MagicMock

from app.workers import tasks_maintenance
from app_shared.costauth.fleet_budget_policy import RollForwardReport
from app_shared.costauth.service import FLEET_PROVIDER_BROWSER, FLEET_PROVIDER_PROXY
from app_shared.maintenance.scoping import MaintenanceScope, maintenance_scope_of
from app_shared.task_names import MAINTENANCE_FLEET_BUDGET_ROLLFORWARD


class _RecordingLogger:
    def __init__(self):
        self.calls = []

    def error(self, msg, *args):
        self.calls.append(("error", msg % args if args else msg))

    def info(self, msg, *args):
        self.calls.append(("info", msg % args if args else msg))

    def exception(self, msg, *args):
        self.calls.append(("exception", msg % args if args else msg))


def _install(*, proxy_cap=75.0, browser_cap=25.0, report=None):
    session = MagicMock(name="session")

    @contextmanager
    def _fake_system_session(task_name):
        assert task_name == "fleet_budget_rollforward", task_name
        yield session

    settings = MagicMock(
        FLEET_BUDGET_MONTHLY_CAP_USD_PROXY=proxy_cap,
        FLEET_BUDGET_MONTHLY_CAP_USD_BROWSER=browser_cap,
    )
    roll = MagicMock(
        name="roll_fleet_budget_caps_forward",
        return_value=report
        or RollForwardReport(written=4, carried=[], uncapped=[]),
    )
    log = _RecordingLogger()

    tasks_maintenance._system_session = _fake_system_session
    tasks_maintenance.get_settings = lambda: settings
    tasks_maintenance.roll_fleet_budget_caps_forward = roll
    tasks_maintenance.logger = log
    return session, roll, log
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


def test_both_configured_caps_are_rolled_forward_a_month_and_committed() -> None:
    """The whole point: October is capped without an operator. Both PAID
    transport classes, a month of lookahead so the boundary is crossed
    already capped, and a COMMIT — an uncommitted limit caps nothing."""
    _assert_ok(
        _run(
            """
session, roll, log = _install(proxy_cap=75.0, browser_cap=25.0)

tasks_maintenance.fleet_budget_rollforward()

assert roll.call_count == 1, roll.call_args_list
args, kwargs = roll.call_args
assert args == (session,), args
assert kwargs["months_ahead"] == 1, kwargs
assert kwargs["caps_usd"] == {
    FLEET_PROVIDER_PROXY: 75.0,
    FLEET_PROVIDER_BROWSER: 25.0,
}, kwargs
assert kwargs["now"].tzinfo is not None, kwargs["now"]
assert session.commit.call_count == 1, session.commit.call_args_list
print("OK")
"""
        )
    )


def test_unset_caps_are_passed_through_as_none_not_as_zero() -> None:
    """`None` means "carry the last cap forward"; `0` would mean "a
    ceiling of nothing", i.e. deny every paid request in the fleet. The
    task must never turn the first into the second."""
    _assert_ok(
        _run(
            """
session, roll, log = _install(proxy_cap=None, browser_cap=None)

tasks_maintenance.fleet_budget_rollforward()

_, kwargs = roll.call_args
assert kwargs["caps_usd"] == {
    FLEET_PROVIDER_PROXY: None,
    FLEET_PROVIDER_BROWSER: None,
}, kwargs
print("OK")
"""
        )
    )


def test_an_uncapped_pair_is_logged_at_error() -> None:
    """The only signal an operator gets that the fleet is spending with no
    money ceiling. INFO here would be indistinguishable from health."""
    _assert_ok(
        _run(
            """
report = RollForwardReport(
    written=0,
    carried=[],
    uncapped=[(FLEET_PROVIDER_PROXY, "2026_09"), (FLEET_PROVIDER_BROWSER, "2026_09")],
)
session, roll, log = _install(proxy_cap=None, browser_cap=None, report=report)

tasks_maintenance.fleet_budget_rollforward()

levels = [level for level, _ in log.calls]
assert levels == ["error"], log.calls
message = log.calls[0][1]
assert "uncapped=2" in message, message
assert "proxy/2026_09" in message, message
assert "browser/2026_09" in message, message
print("OK")
"""
        )
    )


def test_a_fully_capped_pass_logs_at_info_with_its_counts() -> None:
    """Steady state must be quiet but auditable: how many rows were
    written and how many caps were carried rather than configured."""
    _assert_ok(
        _run(
            """
report = RollForwardReport(
    written=2, carried=[(FLEET_PROVIDER_PROXY, "2026_10")], uncapped=[]
)
session, roll, log = _install(report=report)

tasks_maintenance.fleet_budget_rollforward()

levels = [level for level, _ in log.calls]
assert levels == ["info"], log.calls
message = log.calls[0][1]
assert "written=2" in message, message
assert "carried=1" in message, message
assert "uncapped=0" in message, message
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
assert (
    tasks_maintenance.fleet_budget_rollforward.name
    == MAINTENANCE_FLEET_BUDGET_ROLLFORWARD
)
assert (
    maintenance_scope_of(tasks_maintenance.fleet_budget_rollforward)
    is MaintenanceScope.FLEET
)
print("OK")
"""
        )
    )
