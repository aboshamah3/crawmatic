"""Offline migration render test for the webhooks migration (SPEC-16 T011, FR-016/FR-017).

Mirrors `tests/unit/test_migration_offline_alerts.py` (SPEC-09): runs
`alembic upgrade head --sql` (offline, no DB connection) via subprocess
and asserts the rendered SQL contains both `CREATE TABLE`s, the
`webhook_events` `PARTITION BY RANGE (created_at)` parent + current/
next-month `PARTITION OF` children + composite PK, and the six RLS
statements (3 per table x 2). Also asserts `alembic heads` reports
exactly one head — this migration (`03dec3037c8f`) must not fork the
linear history, and its `down_revision` is the SPEC-15 head
(`4a1dca402f78`).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

FAIL_CLOSED_CTX = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"

WEBHOOK_TABLES = ["webhook_endpoints", "webhook_events"]


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _current_and_next_month_suffixes() -> list[str]:
    """Same current+next month suffix logic the migration itself uses
    (`_month_partition_bounds` in
    `03dec3037c8f_webhook_events_and_endpoints.py`), duplicated here
    (not imported) so this test independently verifies the rendered SQL
    rather than trivially re-checking the migration's own helper."""
    now = datetime.now(timezone.utc)
    suffixes: list[str] = []
    year, month = now.year, now.month
    for offset in range(2):
        m = month + offset
        y = year
        if m > 12:
            m -= 12
            y += 1
        suffixes.append(f"{y:04d}_{m:02d}")
    return suffixes


def test_offline_upgrade_head_renders_both_tables() -> None:
    result = _run_alembic("upgrade", "head", "--sql")

    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    sql = result.stdout
    for table_name in WEBHOOK_TABLES:
        assert f"CREATE TABLE {table_name}" in sql


def test_offline_upgrade_head_renders_partition_by_range() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert "PARTITION BY RANGE (created_at)" in sql


def test_offline_upgrade_head_renders_composite_pk_incl_partition_key() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert "CONSTRAINT pk_webhook_events PRIMARY KEY (id, created_at)" in sql


def test_offline_upgrade_head_renders_current_and_next_month_partitions() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    for suffix in _current_and_next_month_suffixes():
        assert (
            f"CREATE TABLE webhook_events_{suffix} PARTITION OF webhook_events" in sql
        )


def test_offline_upgrade_head_renders_six_rls_statements() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    for table_name in WEBHOOK_TABLES:
        assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY {table_name}_workspace_isolation ON {table_name}" in sql

    assert FAIL_CLOSED_CTX in sql


def test_alembic_heads_reports_exactly_one_head() -> None:
    result = _run_alembic("heads")

    assert result.returncode == 0, (
        f"alembic heads failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    head_lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert len(head_lines) == 1, f"expected exactly one head, got: {head_lines!r}"
    assert "(head)" in head_lines[0]


def test_down_revision_is_the_spec15_head() -> None:
    result = _run_alembic("history")
    assert result.returncode == 0, (
        f"alembic history failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "4a1dca402f78 -> 03dec3037c8f" in result.stdout
