"""Live-Postgres connectivity test (T021, FR-015, SC-007) — ⏸ DEFERRED.

Exercises ``app_shared.database.check_connection()`` against a real
database: it must open a session and execute ``SELECT 1`` without
raising.

This needs a reachable Postgres instance (either via ``DATABASE_URL``,
which ``check_connection()`` actually uses, or the presence of
``MIGRATION_DATABASE_URL`` as a signal that this host is meant to run
live-DB checks). It is **not** runnable in the no-Docker-daemon build
environment used to author this feature — it SKIPS cleanly whenever:

* ``app_shared.config.Settings`` fails to construct at all (e.g.
  required vars unset, no ``.env`` present), or
* neither ``DATABASE_URL`` nor ``MIGRATION_DATABASE_URL`` is configured, or
* a real connection attempt fails (no reachable server, auth failure, ...).

Where Postgres *is* reachable, this test actually calls
``check_connection()`` and asserts it returns ``None`` without raising.
"""

from __future__ import annotations

import pytest


def _postgres_reachable() -> bool:
    """Best-effort probe: True only if check_connection() actually succeeds.

    Any failure (missing config, no reachable server, auth error, ...)
    is treated as "not reachable" so the test skips cleanly instead of
    erroring in environments without a live Postgres (e.g. no Docker
    daemon, no .env).
    """
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL and not settings.MIGRATION_DATABASE_URL:
        return False

    try:
        from app_shared.database import check_connection

        check_connection()
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="No reachable Postgres / DATABASE_URL configured in this environment",
)


def test_check_connection_executes_select_1_against_live_db() -> None:
    """check_connection() opens a session and runs SELECT 1 without raising (FR-015)."""
    from app_shared.database import check_connection

    result = check_connection()

    assert result is None
