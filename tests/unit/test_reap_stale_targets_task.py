"""`maintenance.reap_stale_targets` — the Celery task wrapper (EPA A3/B2).

The sweeps themselves are covered against a real engine in
`test_jobs_reaper.py`. What is left for the task is the wiring that the
sweeps cannot test and that, when wrong, fails silently:

1. both passes actually run, with the CONFIGURED thresholds — a task that
   read the wrong setting (or hardcoded a literal) would reap live work
   or nothing at all, and either way the logs would look healthy;
2. the work is COMMITTED — an uncommitted revert un-wedges nothing, and
   the sweep would appear to succeed forever while the job stayed
   `RUNNING`;
3. both passes share ONE `now`, so a target the reaper rescues in this
   tick is measured against the same instant by the deadline pass;
4. the task is registered under the name the scheduler enqueues and
   declares the FLEET scope `register_maintenance_tasks` refuses to
   accept a maintenance task without.

Subprocess-loaded (the `test_breaker_evaluate_task.py` idiom): `apps/api`
and `apps/workers` each ship a top-level `app` package, ambiguous once
another test module in the same session has imported one, and
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

_SETUP = """
import sys
sys.path.insert(0, "apps/workers")

from contextlib import contextmanager
from unittest.mock import MagicMock

from app.workers import tasks_jobs
from app_shared.maintenance.scoping import MaintenanceScope, maintenance_scope_of
from app_shared.task_names import SCRAPE_REAP_STALE_TARGETS


def _install(*, reap_after=2100, max_runtime=43200):
    session = MagicMock(name="session")

    @contextmanager
    def _fake_system_session():
        yield session

    settings = MagicMock(
        SCRAPE_STARTED_REAP_AFTER_SECONDS=reap_after,
        SCRAPE_JOB_MAX_RUNTIME_SECONDS=max_runtime,
    )
    revert = MagicMock(name="revert_stale_started_targets", return_value=3)
    fail = MagicMock(name="fail_targets_past_job_deadline", return_value=5)

    tasks_jobs.get_system_session = _fake_system_session
    tasks_jobs.get_settings = lambda: settings
    tasks_jobs.revert_stale_started_targets = revert
    tasks_jobs.fail_targets_past_job_deadline = fail
    return session, revert, fail
"""


def _run(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _SETUP + body],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **_ENV},
    )


def _assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")


def test_both_sweeps_run_with_the_configured_thresholds_and_commit() -> None:
    """The thresholds are the safety property. Reading the wrong setting
    would either reap targets a live spider still owns or reap nothing at
    all — and an uncommitted sweep un-wedges nothing while logging a
    perfectly healthy row count."""
    _assert_ok(
        _run(
            """
session, revert, fail = _install(reap_after=2100, max_runtime=43200)

tasks_jobs.reap_stale_targets()

assert revert.call_count == 1, revert.call_args_list
args, kwargs = revert.call_args
assert args == (session,), args
assert kwargs["older_than_seconds"] == 2100, kwargs

assert fail.call_count == 1, fail.call_args_list
args, kwargs = fail.call_args
assert args == (session,), args
assert kwargs["max_runtime_seconds"] == 43200, kwargs

assert session.commit.call_count == 1, session.commit.call_args_list
print("OK")
"""
        )
    )


def test_the_thresholds_come_from_settings_not_from_literals() -> None:
    """A hardcoded default would be invisible until the day an operator
    retunes the knob in the DB and nothing changes."""
    _assert_ok(
        _run(
            """
session, revert, fail = _install(reap_after=999, max_runtime=111)

tasks_jobs.reap_stale_targets()

assert revert.call_args.kwargs["older_than_seconds"] == 999
assert fail.call_args.kwargs["max_runtime_seconds"] == 111
print("OK")
"""
        )
    )


def test_both_passes_share_one_now() -> None:
    """Reverting first only helps if the deadline pass measures the same
    instant — two clock reads would let a target rescued in this tick be
    judged against a later `now` than the job it belongs to."""
    _assert_ok(
        _run(
            """
session, revert, fail = _install()

tasks_jobs.reap_stale_targets()

assert revert.call_args.kwargs["now"] == fail.call_args.kwargs["now"]
assert revert.call_args.kwargs["now"].tzinfo is not None
print("OK")
"""
        )
    )


def test_task_is_registered_under_its_name_and_declares_a_fleet_scope() -> None:
    """The two-part declaration every maintenance task carries: the Celery
    name the scheduler enqueues, and the FLEET scope
    `register_maintenance_tasks` refuses to accept a task without. The
    sweep is fleet-wide by nature — a wedged job in ANY workspace is the
    thing being fixed."""
    _assert_ok(
        _run(
            """
assert tasks_jobs.reap_stale_targets.name == SCRAPE_REAP_STALE_TARGETS
assert maintenance_scope_of(tasks_jobs.reap_stale_targets) is MaintenanceScope.FLEET
print("OK")
"""
        )
    )
