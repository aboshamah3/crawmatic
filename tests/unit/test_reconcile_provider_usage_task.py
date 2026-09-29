"""`maintenance.reconcile_provider_usage` cadence (2026-09-29, plan E7.2/E7.4).

* The cadence reconciles every imported window still lacking a settlement
  over `PROVIDER_RECONCILE_LOOKBACK_DAYS`, not only yesterday's.
* No provider evidence while the fleet paid a provider is a WARNING with a
  name (`..._import_missing`), not silence.

Subprocess-loaded like test_reap_stale_targets_task.py (the workers' `app`
package clashes with the API's).
"""

from __future__ import annotations

import os
import subprocess
import sys

_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "AUTH_DATABASE_URL": "postgresql+psycopg://crawmatic_auth:x@postgres:5432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}

_SETUP = """
import logging, sys
sys.path.insert(0, "apps/workers")
from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock
from app.workers import tasks_maintenance as tm

session = MagicMock(name="session")

@contextmanager
def fake_session(_name):
    yield session

tm._system_session = fake_session
calls = {}
def awaiting(sess, *, since_date, until_date, provider=None):
    calls["range"] = (since_date, until_date, provider)
    return list(WINDOWS)
tm.windows_awaiting_settlement = awaiting
tm.count_paid_operations = lambda sess, *, since, until: PAID
tm.reconcile_window = lambda w: MagicMock(passed=True)
records = []
class H(logging.Handler):
    def emit(self, record):
        records.append((record.levelname, record.getMessage()))
tm.logger.addHandler(H())
tm.logger.setLevel(logging.INFO)
"""


def _run(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _SETUP + body],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **_ENV},
    )


def test_the_cadence_reconciles_the_whole_lookback() -> None:
    result = _run(
        """
WINDOWS = [MagicMock(id=1), MagicMock(id=2)]
PAID = 5
tm.reconcile_provider_usage()
since, until, provider = calls["range"]
assert (until - since).days == tm.get_settings().PROVIDER_RECONCILE_LOOKBACK_DAYS, (since, until)
assert until == date.today() or (date.today() - until).days <= 1
assert not [m for lvl, m in records if "import_missing" in m]
print("OK")
"""
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("OK")


def test_no_evidence_while_paying_a_provider_is_a_named_warning() -> None:
    result = _run(
        """
WINDOWS = []
PAID = 98403
tm.reconcile_provider_usage()
warnings = [m for lvl, m in records if lvl == "WARNING" and "import_missing" in m]
assert warnings and "paid_operations=98403" in warnings[0], records
print("OK")
"""
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("OK")


def test_no_evidence_and_no_paid_traffic_is_quiet() -> None:
    result = _run(
        """
WINDOWS = []
PAID = 0
tm.reconcile_provider_usage()
assert not [m for lvl, m in records if "import_missing" in m], records
print("OK")
"""
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("OK")
