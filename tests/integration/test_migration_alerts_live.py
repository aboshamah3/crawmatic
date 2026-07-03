"""Live-Postgres SPEC-09 alerts migration test (T039, FR-006) — ⏸ DEFERRED.

`tests/unit/test_migration_offline_alerts.py` already proves the SQL
`alembic upgrade head --sql` renders (offline, no DB). This live test's
distinguishing contribution: `alembic upgrade head` actually applied to
a real Postgres — the three tables genuinely exist, the
`price_alert_events` parent is genuinely `PARTITION BY RANGE
(created_at)` with the current + next-month partitions genuinely
attached, RLS is genuinely `ENABLE`d + `FORCE`d on all three, a single
alembic head is preserved, and `downgrade` cleanly removes everything it
created. Mirrors `tests/integration/test_migration_job.py` (SPEC-01) /
`tests/integration/test_seed_bootstrap.py` (SPEC-03)'s
subprocess-`alembic`-against-a-real-database convention.

Per contracts/migration-alerts.md:

1. `alembic upgrade head` creates `variant_price_states`,
   `variant_alert_states`, `price_alert_events` (+ the current and
   next-month `price_alert_events_YYYY_MM` partitions attached to it).
2. `relrowsecurity`/`relforcerowsecurity` are both true on all three
   parent tables (RLS applied to a partitioned parent propagates to its
   partitions — not independently re-checked here, per the SPEC-07
   `price_observations` precedent this migration mirrors).
3. `alembic heads` shows exactly one head.
4. `alembic downgrade -1` (back to `a6b0234cd4ad`) cleanly drops the two
   current-state tables + every `price_alert_events_*` partition + the
   parent, then `alembic upgrade head` re-creates everything (round
   trip).

Needs a reachable Postgres with `MIGRATION_DATABASE_URL` set
(direct-to-Postgres, never the PgBouncer pooler — DDL requires a
non-pooled session). Not runnable in the no-Docker-daemon build
environment used to author this feature — SKIPS cleanly whenever
`MIGRATION_DATABASE_URL` is unset or unreachable.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host).
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[2]

_REVISION = "e4a75b48360c"
_DOWN_REVISION = "a6b0234cd4ad"


def _migration_database_url() -> str | None:
    return os.environ.get("MIGRATION_DATABASE_URL")


def _postgres_reachable() -> bool:
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


def _partition_suffixes() -> list[str]:
    now = datetime.now(UTC)
    suffixes = []
    year, month = now.year, now.month
    for offset in range(2):
        m = month + offset
        y = year
        if m > 12:
            m -= 12
            y += 1
        suffixes.append(f"{y:04d}_{m:02d}")
    return suffixes


@pytest.fixture()
def migrated_engine():
    setup = _run_alembic("upgrade", "head")
    assert setup.returncode == 0, (
        f"alembic upgrade head (setup) failed:\nstdout={setup.stdout}\nstderr={setup.stderr}"
    )
    engine = create_engine(_migration_database_url())
    try:
        yield engine
    finally:
        engine.dispose()


def test_upgrade_head_creates_three_tables_and_partitions(migrated_engine) -> None:
    inspector = inspect(migrated_engine)
    table_names = set(inspector.get_table_names())
    for expected in ("variant_price_states", "variant_alert_states", "price_alert_events"):
        assert expected in table_names, f"{expected} missing after alembic upgrade head"

    with migrated_engine.connect() as conn:
        partition_by = conn.execute(
            text(
                "SELECT partstrat FROM pg_partitioned_table pt "
                "JOIN pg_class c ON c.oid = pt.partrelid WHERE c.relname = 'price_alert_events'"
            )
        ).scalar_one_or_none()
        assert partition_by == "r", "price_alert_events is not RANGE-partitioned"

        for suffix in _partition_suffixes():
            partition_name = f"price_alert_events_{suffix}"
            exists = conn.execute(
                text("SELECT to_regclass(:name) IS NOT NULL"), {"name": partition_name}
            ).scalar_one()
            assert exists, f"{partition_name} partition missing"


def test_rls_enabled_and_forced_on_all_three_tables(migrated_engine) -> None:
    with migrated_engine.connect() as conn:
        for table in ("variant_price_states", "variant_alert_states", "price_alert_events"):
            row = conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"
                ),
                {"t": table},
            ).fetchone()
            assert row is not None, f"{table} not found in pg_class"
            assert row[0] is True, f"{table} does not have RLS enabled"
            assert row[1] is True, f"{table} does not have RLS forced"


def test_single_alembic_head() -> None:
    result = _run_alembic("heads")
    assert result.returncode == 0, f"alembic heads failed:\n{result.stdout}\n{result.stderr}"
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"expected exactly one alembic head, got: {result.stdout!r}"
    assert _REVISION in result.stdout


def test_downgrade_then_upgrade_round_trip() -> None:
    setup = _run_alembic("upgrade", "head")
    assert setup.returncode == 0, (
        f"alembic upgrade head (setup) failed:\nstdout={setup.stdout}\nstderr={setup.stderr}"
    )

    down = _run_alembic("downgrade", _DOWN_REVISION)
    assert down.returncode == 0, (
        f"alembic downgrade {_DOWN_REVISION} failed:\nstdout={down.stdout}\nstderr={down.stderr}"
    )

    engine = create_engine(_migration_database_url())
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        for dropped in ("variant_price_states", "variant_alert_states", "price_alert_events"):
            assert dropped not in table_names, f"{dropped} still present after downgrade"
        with engine.connect() as conn:
            for suffix in _partition_suffixes():
                partition_name = f"price_alert_events_{suffix}"
                exists = conn.execute(
                    text("SELECT to_regclass(:name) IS NOT NULL"), {"name": partition_name}
                ).scalar_one()
                assert not exists, f"{partition_name} partition still present after downgrade"
    finally:
        engine.dispose()

    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, (
        f"alembic upgrade head (round-trip) failed:\nstdout={up.stdout}\nstderr={up.stderr}"
    )

    engine2 = create_engine(_migration_database_url())
    try:
        inspector2 = inspect(engine2)
        table_names2 = set(inspector2.get_table_names())
        for expected in ("variant_price_states", "variant_alert_states", "price_alert_events"):
            assert expected in table_names2, f"{expected} missing after round-trip upgrade"
    finally:
        engine2.dispose()
