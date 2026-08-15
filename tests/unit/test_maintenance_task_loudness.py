"""The maintenance tasks must fail LOUDLY (2026-08-15 readiness cycle).

Second, independent root cause of the same incident: the `worker` Railway
service had neither `SYSTEM_DATABASE_URL` nor `AUTH_DATABASE_URL`, so
every `partition_create` / `daily_rollup` / `retention_drop` delivery died
in `get_system_session()` with a `RuntimeError` whose message talks about
the *scheduler's refresh-rule claim* — a sentence that appears nowhere in
a maintenance runbook and is not greppable as "maintenance is dead".
Verified live: worker logs for 2026-08-14 21:38 show all three tasks
received and all three raising that error, on a scheduler that had been
up for two days and was enqueueing perfectly.

So: a named ERROR event when the session cannot be opened, and a
verify-after-create pass so a partition that is *still* missing after a
"successful" run is reported rather than assumed.

Subprocess-loaded because `apps/workers` ships its own top-level `app`
package (same idiom as `test_jobs_dispatch_task.py`).
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
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO)

from app.workers import tasks_maintenance
from app_shared.maintenance.health import EVENT_PARTITION_MISSING
from app_shared.maintenance.partitions import RunReport


class _Recorder(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self, level):
        return "\\n".join(
            r.getMessage() for r in self.records if r.levelno >= level
        )


_recorder = _Recorder()
tasks_maintenance.logger.addHandler(_recorder)
tasks_maintenance.logger.setLevel(logging.INFO)


class _FakeSession:
    def commit(self):
        pass

    def rollback(self):
        pass


@contextmanager
def _fake_system_session(task_name):
    yield _FakeSession()
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


def test_missing_system_database_url_emits_a_named_error_event_and_still_raises() -> None:
    """Exactly the live production failure. It must be greppable by event
    name and carry the remedy — and must still fail the task."""
    _assert_ok(
        _run(
            """
from app_shared.config import get_settings

settings = get_settings()
assert settings.SYSTEM_DATABASE_URL is None
assert settings.AUTH_DATABASE_URL is None

raised = False
try:
    with tasks_maintenance._system_session("partition_create"):
        pass
except RuntimeError:
    raised = True

assert raised, "the task must still fail loudly, not be silently skipped"
text = _recorder.text(logging.ERROR)
assert tasks_maintenance.EVENT_SYSTEM_SESSION_UNAVAILABLE in text, text
assert "task=partition_create" in text, text
assert "SYSTEM_DATABASE_URL" in text, text
print("OK")
"""
        )
    )


def test_partition_still_missing_after_a_successful_run_is_reported() -> None:
    """A run that creates nothing and leaves a gap must not log a cheerful
    INFO and stop there — that is precisely the silence that let a dated
    write outage sit undetected for a month."""
    _assert_ok(
        _run(
            """
tasks_maintenance._system_session = _fake_system_session
tasks_maintenance.create_missing_partitions = (
    lambda session, *, now_utc, lookahead_months: RunReport()
)

from app_shared.maintenance.health import MissingPartition

tasks_maintenance.find_missing_partitions = lambda session, *, now_utc, months_ahead: (
    [
        MissingPartition(
            table="price_observations",
            partition="price_observations_2026_09",
            month_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
    ],
    [],
)

tasks_maintenance.partition_create()

text = _recorder.text(logging.ERROR)
assert EVENT_PARTITION_MISSING in text, text
assert "partition=price_observations_2026_09" in text, text
assert "source=partition_create_verify" in text, text
print("OK")
"""
        )
    )


def test_a_complete_run_reports_no_missing_partitions() -> None:
    _assert_ok(
        _run(
            """
tasks_maintenance._system_session = _fake_system_session
tasks_maintenance.create_missing_partitions = (
    lambda session, *, now_utc, lookahead_months: RunReport(
        partitions_created=["price_observations_2026_09"]
    )
)
tasks_maintenance.find_missing_partitions = lambda session, *, now_utc, months_ahead: ([], [])

tasks_maintenance.partition_create()

assert EVENT_PARTITION_MISSING not in _recorder.text(logging.ERROR)
info = _recorder.text(logging.INFO)
assert "partitions_still_missing=[]" in info, info
assert "lookahead_months=3" in info, info
print("OK")
"""
        )
    )
