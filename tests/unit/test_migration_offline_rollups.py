"""Offline migration render test for the `variant_price_daily_rollups`
migration (SPEC-15 T022, FR-009a, `contracts/daily-rollup.md`,
`data-model.md` §1).

Mirrors `tests/unit/test_strategy_single_head.py` (SPEC-12): runs
`alembic upgrade head --sql` (offline, no DB connection) via subprocess
and asserts the rendered SQL contains the `variant_price_daily_rollups`
`CREATE TABLE` statement, its unique constraint/indexes, the standard
`emit_rls_policy` statements, that `alembic heads` yields a single head,
and that the new revision's `down_revision == "93511d5f7885"` (the
SPEC-13 head this feature's one migration chains off of).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

FAIL_CLOSED_CTX = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _upgrade_sql() -> str:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


def test_offline_upgrade_head_renders_variant_price_daily_rollups_table() -> None:
    sql = _upgrade_sql()
    assert "CREATE TABLE variant_price_daily_rollups" in sql


def test_offline_upgrade_head_renders_constraints_and_indexes() -> None:
    sql = _upgrade_sql()
    assert (
        "CONSTRAINT uq_vpdr_workspace_id_product_variant_id_date UNIQUE "
        "(workspace_id, product_variant_id, date)" in sql
    )
    assert "fk_variant_price_daily_rollups_workspace_id_workspaces" in sql
    assert "pk_variant_price_daily_rollups" in sql
    assert (
        "CREATE INDEX ix_variant_price_daily_rollups_workspace_id "
        "ON variant_price_daily_rollups (workspace_id)" in sql
    )
    assert (
        "CREATE INDEX ix_variant_price_daily_rollups_date "
        "ON variant_price_daily_rollups (date)" in sql
    )


def test_offline_upgrade_head_renders_money_and_date_column_types() -> None:
    sql = _upgrade_sql()
    create_stmt_start = sql.index("CREATE TABLE variant_price_daily_rollups")
    create_stmt_end = sql.index(");", create_stmt_start)
    create_stmt = sql[create_stmt_start:create_stmt_end]
    assert "date DATE NOT NULL" in create_stmt
    assert "currency CHAR(3) NOT NULL" in create_stmt
    assert "client_price NUMERIC(18, 4) NOT NULL" in create_stmt
    assert "cheapest_competitor_price NUMERIC(18, 4)" in create_stmt
    assert "average_competitor_price NUMERIC(18, 4)" in create_stmt
    assert "highest_competitor_price NUMERIC(18, 4)" in create_stmt
    assert "comparable_competitor_count INTEGER NOT NULL" in create_stmt
    assert "latest_alert_type VARCHAR(32) NOT NULL" in create_stmt


def test_offline_upgrade_head_renders_standard_rls() -> None:
    sql = _upgrade_sql()
    table = "variant_price_daily_rollups"
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
    assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
    assert (
        f"CREATE POLICY {table}_workspace_isolation ON {table} "
        f"USING (workspace_id = {FAIL_CLOSED_CTX})" in sql
    )


def test_downgrade_drops_indexes_then_table() -> None:
    result = _run_alembic("downgrade", "4a1dca402f78:93511d5f7885", "--sql")
    assert result.returncode == 0, (
        f"alembic downgrade --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    sql = result.stdout
    assert "DROP INDEX ix_variant_price_daily_rollups_date" in sql
    assert "DROP INDEX ix_variant_price_daily_rollups_workspace_id" in sql
    assert "DROP TABLE variant_price_daily_rollups" in sql
    date_idx_pos = sql.index("DROP INDEX ix_variant_price_daily_rollups_date")
    ws_idx_pos = sql.index("DROP INDEX ix_variant_price_daily_rollups_workspace_id")
    table_pos = sql.index("DROP TABLE variant_price_daily_rollups")
    assert date_idx_pos < ws_idx_pos < table_pos


def test_alembic_heads_reports_exactly_one_head() -> None:
    result = _run_alembic("heads")

    assert result.returncode == 0, (
        f"alembic heads failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    head_lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert len(head_lines) == 1, f"expected exactly one head, got: {head_lines!r}"
    assert "(head)" in head_lines[0]


def test_down_revision_is_the_spec13_head() -> None:
    result = _run_alembic("history")
    assert result.returncode == 0, (
        f"alembic history failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    matching_lines = [
        line for line in result.stdout.splitlines() if line.startswith("93511d5f7885 -> ")
    ]
    assert matching_lines, result.stdout
