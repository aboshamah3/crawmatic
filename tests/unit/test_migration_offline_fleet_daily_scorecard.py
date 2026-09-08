"""Offline migration render test for `fleet_daily_scorecard` (EPA D5,
deep dive §12 item 9).

The repo's standard guard for a new revision (the
`tests/unit/test_migration_offline_rollup_completion.py` precedent):
runs `alembic upgrade head --sql` offline — no DB connection — and
asserts the rendered DDL, the single head, and that this revision chains
from the head that existed when it was written.

Every metric column is asserted NULLABLE with no server default: a
column born NOT NULL or with a numeric default could never represent
"this input was not measured today", which is the entire discipline
`app_shared.maintenance.scorecard` is built around.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The single head this revision chains from (EPA C9's partition swap).
EXPECTED_DOWN_REVISION = "a5e0c74b13d9"
REVISION = "f6b28c714a93"

#: Every plan-named metric column (the packet D5 acceptance criterion,
#: verbatim) plus `date`, the primary key.
EXPECTED_COLUMNS = (
    "date",
    "provider_bytes",
    "railway_cpu_seconds",
    "railway_ram_gb_hours",
    "railway_egress_gb",
    "valid_fresh_matches",
    "attempts_per_valid_fresh",
    "browser_share",
    "proxied_share",
    "queue_oldest_seconds_p95",
    "persistence_lag_seconds_p95",
    "missing_metric_fraction",
    "budget_reserved_usd",
    "budget_settled_usd",
    "backup_egress_gb",
    "cost_per_valid_fresh_micro_usd",
)


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _upgrade_sql() -> str:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


def _create_statement(sql: str) -> str:
    start = sql.index("CREATE TABLE fleet_daily_scorecard")
    return sql[start : sql.index(");", start)]


def test_offline_upgrade_head_renders_the_fleet_daily_scorecard_table() -> None:
    assert "CREATE TABLE fleet_daily_scorecard" in _upgrade_sql()


def test_the_day_is_the_primary_key_with_no_surrogate_id() -> None:
    """The natural key IS the identity (the `rollup_completion`
    precedent) — a surrogate `id` would allow two rows for one day."""
    create = _create_statement(_upgrade_sql())
    assert "CONSTRAINT pk_fleet_daily_scorecard PRIMARY KEY (date)" in create
    assert " id " not in create


def test_every_plan_field_is_present() -> None:
    create = _create_statement(_upgrade_sql())
    for column in EXPECTED_COLUMNS:
        assert f"\n\t{column} " in create.replace("    ", "\t") or column in create, (
            f"column {column!r} missing from CREATE TABLE"
        )


def test_every_metric_column_is_nullable_with_no_server_default() -> None:
    """NULL means "unmeasured today" — a NOT NULL column or a numeric
    server default would make an honest "we don't know" unrepresentable."""
    create = _create_statement(_upgrade_sql())
    for column in EXPECTED_COLUMNS:
        if column == "date":
            continue
        # Find this column's own line inside the CREATE TABLE body.
        line = next(
            (raw for raw in create.splitlines() if raw.strip().startswith(column + " ")),
            None,
        )
        assert line is not None, f"no column line found for {column!r}"
        assert "NOT NULL" not in line, f"{column} must be nullable, got: {line!r}"
        assert "DEFAULT" not in line, f"{column} must have no server default, got: {line!r}"


def test_the_table_gets_no_rls_policy() -> None:
    """Global, no `workspace_id` — one scorecard day summarises the
    whole fleet (the `rollup_completion`/`maintenance_cadences` shape)."""
    sql = _upgrade_sql()
    named = "\n".join(
        line for line in sql.splitlines() if "fleet_daily_scorecard" in line
    )
    assert "ENABLE ROW LEVEL SECURITY" not in named
    create = _create_statement(sql)
    assert "\n    workspace_id" not in create
    assert "(workspace_id" not in create


def test_revision_chains_from_the_recorded_head() -> None:
    module = (
        REPO_ROOT / "alembic" / "versions" / f"{REVISION}_fleet_daily_scorecard.py"
    ).read_text()
    assert f"revision: str = '{REVISION}'" in module
    assert (
        f"down_revision: Union[str, Sequence[str], None] = '{EXPECTED_DOWN_REVISION}'"
        in module
    )


def test_exactly_one_head() -> None:
    result = _run_alembic("heads")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("(head)") == 1, result.stdout
