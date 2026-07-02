"""Live-Postgres migration job test (T033, FR-011, FR-016, SC-001, US1 AS-1) — ⏸ DEFERRED.

Exercises the one-shot migration job (`alembic upgrade head`) against a
real database:

1. `alembic upgrade head` (online) brings a fresh database to head and
   the `_smoke_foundation` table exists afterwards.
2. The downgrade round-trip — `alembic downgrade base` then
   `alembic upgrade head` — succeeds and leaves the table present again
   (FR-016 downgrade path verified live, [analyze G3]).

This needs a reachable Postgres instance with `MIGRATION_DATABASE_URL`
set (direct-to-Postgres, e.g. `postgres:5432` — never the PgBouncer
pooler). It is **not** runnable in the no-Docker-daemon build
environment used to author this feature — it SKIPS cleanly whenever:

* `MIGRATION_DATABASE_URL` is unset, or
* a real connection attempt against it fails (no reachable server, auth
  error, wrong credentials, ...).

Where Postgres *is* reachable, this test actually invokes `alembic`
(via subprocess, against the repo-root `alembic.ini`/`alembic/env.py`)
and asserts against the live schema through a throwaway SQLAlchemy
engine/connection.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[2]


def _migration_database_url() -> str | None:
    return os.environ.get("MIGRATION_DATABASE_URL")


def _postgres_reachable() -> bool:
    """Best-effort probe: True only if MIGRATION_DATABASE_URL is set and connectable.

    Any failure (unset, unreachable, auth error, ...) is treated as "not
    reachable" so the test skips cleanly instead of erroring in
    environments without a live Postgres (e.g. no Docker daemon).
    """
    url = _migration_database_url()
    if not url:
        return False

    try:
        engine = create_engine(url)
        with engine.connect():
            pass
        engine.dispose()
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="No reachable Postgres / MIGRATION_DATABASE_URL configured in this environment",
)


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _smoke_table_exists() -> bool:
    engine = create_engine(_migration_database_url())
    try:
        return inspect(engine).has_table("_smoke_foundation")
    finally:
        engine.dispose()


def test_upgrade_head_creates_smoke_foundation_table() -> None:
    """`alembic upgrade head` (online) brings the DB to head and creates the demo table."""
    result = _run_alembic("upgrade", "head")

    assert result.returncode == 0, (
        f"alembic upgrade head failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert _smoke_table_exists(), "_smoke_foundation table missing after `alembic upgrade head`"


def test_downgrade_base_then_upgrade_head_round_trip() -> None:
    """`alembic downgrade base` then `alembic upgrade head` succeeds (FR-016 downgrade path)."""
    # Ensure we start from head (independent test ordering safety net).
    setup = _run_alembic("upgrade", "head")
    assert setup.returncode == 0, (
        f"alembic upgrade head (setup) failed:\nstdout={setup.stdout}\nstderr={setup.stderr}"
    )

    down = _run_alembic("downgrade", "base")
    assert down.returncode == 0, (
        f"alembic downgrade base failed:\nstdout={down.stdout}\nstderr={down.stderr}"
    )
    assert not _smoke_table_exists(), (
        "_smoke_foundation table still present after `alembic downgrade base`"
    )

    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, (
        f"alembic upgrade head (round-trip) failed:\nstdout={up.stdout}\nstderr={up.stderr}"
    )
    assert _smoke_table_exists(), (
        "_smoke_foundation table missing after the downgrade/upgrade round-trip"
    )
