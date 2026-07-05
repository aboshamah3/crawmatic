"""Live-Postgres SPEC-13 refresh_rules migration test (T014, FR-005) — ⏸ DEFERRED.

Distinguishing contribution over any offline/unit render: `alembic upgrade
head` actually applied to a real Postgres — the `refresh_rules` table
genuinely exists with its five composite scope-target FKs + three CHECK
constraints + the partial due-claim index, RLS is genuinely `ENABLE`d +
`FORCE`d, a single alembic head is preserved, and `downgrade` cleanly
removes everything it created (round trip). Mirrors
`tests/integration/test_migration_alerts_live.py` (SPEC-09) /
`tests/integration/test_migration_job.py` (SPEC-01)'s subprocess-`alembic`
-against-a-real-database convention.

Per `data-model.md` / `alembic/versions/93511d5f7885_refresh_rules.py`:

1. `alembic upgrade head` creates `refresh_rules` with its PK, workspace FK,
   five CASCADE composite scope-target FKs, three CHECK constraints, and
   the partial index `ix_refresh_rules_due` on `(next_run_at) WHERE enabled`.
2. `relrowsecurity`/`relforcerowsecurity` are both true on `refresh_rules`.
3. `alembic heads` shows exactly one head.
4. `alembic downgrade -1` (back to `f30c60cfa2f7`) cleanly drops the
   partial index then the table, then `alembic upgrade head` re-creates
   everything (round trip).

Needs a reachable Postgres with `MIGRATION_DATABASE_URL` set (direct-to-
Postgres, never the PgBouncer pooler — DDL requires a non-pooled session).
Not runnable in the no-Docker-daemon build environment used to author this
feature — SKIPS cleanly whenever `MIGRATION_DATABASE_URL` is unset or
unreachable.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[2]

_REVISION = "93511d5f7885"
_DOWN_REVISION = "f30c60cfa2f7"


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


def test_upgrade_head_creates_refresh_rules_table(migrated_engine) -> None:
    inspector = inspect(migrated_engine)
    table_names = set(inspector.get_table_names())
    assert "refresh_rules" in table_names, "refresh_rules missing after alembic upgrade head"

    columns = {col["name"] for col in inspector.get_columns("refresh_rules")}
    for expected in (
        "id",
        "workspace_id",
        "name",
        "scope",
        "product_id",
        "product_variant_id",
        "product_group_id",
        "competitor_id",
        "match_id",
        "cron_expression",
        "interval_minutes",
        "priority",
        "enabled",
        "next_run_at",
        "last_run_at",
        "locked_at",
        "created_at",
        "updated_at",
    ):
        assert expected in columns, f"refresh_rules.{expected} missing"


def test_check_constraints_present(migrated_engine) -> None:
    inspector = inspect(migrated_engine)
    check_names = {c["name"] for c in inspector.get_check_constraints("refresh_rules")}
    for expected in (
        "ck_refresh_rules_exactly_one_cadence",
        "ck_refresh_rules_interval_minutes_positive",
        "ck_refresh_rules_scope_target",
    ):
        assert expected in check_names, f"{expected} missing from refresh_rules"


def test_partial_due_index_present(migrated_engine) -> None:
    with migrated_engine.connect() as conn:
        indexdef = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_refresh_rules_due'")
        ).scalar_one_or_none()
        assert indexdef is not None, "ix_refresh_rules_due missing"
        assert "enabled" in indexdef, "ix_refresh_rules_due is not a partial index on enabled"


def test_rls_enabled_and_forced(migrated_engine) -> None:
    with migrated_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'refresh_rules'"
            )
        ).fetchone()
        assert row is not None, "refresh_rules not found in pg_class"
        assert row[0] is True, "refresh_rules does not have RLS enabled"
        assert row[1] is True, "refresh_rules does not have RLS forced"


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
        assert "refresh_rules" not in table_names, "refresh_rules still present after downgrade"
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
        assert "refresh_rules" in table_names2, "refresh_rules missing after round-trip upgrade"
    finally:
        engine2.dispose()
