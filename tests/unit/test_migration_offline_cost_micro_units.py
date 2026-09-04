"""Offline render test for the cents -> micro-USD migration (H4/B1).

Mirrors `tests/unit/test_migration_offline_abuse_limit.py`: runs
`alembic upgrade head --sql` (offline, no DB connection) via subprocess
and asserts the rendered SQL says what `b7c1d2e3f4a5` claims to say.

The four things worth pinning here:

1. **Every amount column is rescaled AND renamed.** Either half alone is
   the bug: a rescale without a rename leaves a 10_000x-different number
   under an unchanged name for unchanged code to read, and a rename
   without a rescale leaves every historical amount 10_000x too small
   under a name that promises micro-USD.
2. **`COLUMNS` covers the model, exactly.** The migration's list is
   hand-written; the ORM metadata is the truth. A `*_cost_micro_units`
   column that exists on a model and not in `COLUMNS` is a column whose
   history was never rescaled, and it would be discovered by an operator
   comparing a dashboard to an invoice.
3. **Rescale precedes rename.** A crash between the two must leave a
   column that still answers to its OLD name, so a half-applied migration
   is never an unrescaled number wearing the new name.
4. **The plpgsql trigger function is recreated.** PostgreSQL rewrites
   CHECK-constraint expressions on a column rename but NOT function
   bodies, so `network_operation_allocations_check_total()` would fail at
   runtime on the first allocation written after the migration.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Loaded by PATH, not by import: `alembic` on `sys.path` is the installed
# package, and `alembic/versions/` is a script directory, not a package.
_MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / "b7c1d2e3f4a5_cost_units_to_micro_usd.py"
)
_spec = importlib.util.spec_from_file_location("_h4_migration", _MIGRATION_PATH)
assert _spec is not None and _spec.loader is not None
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)

COLUMNS: list[tuple[str, str, str]] = _migration.COLUMNS
SCALE: int = _migration.SCALE


def _upgrade_sql() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


def test_one_cent_is_ten_thousand_micro_usd() -> None:
    """1 USD == 100 cents == 1_000_000 micro-USD."""
    assert SCALE == 10_000
    assert SCALE * 100 == 1_000_000


def test_every_column_is_both_rescaled_and_renamed() -> None:
    sql = _upgrade_sql()

    for table, old, new in COLUMNS:
        rescale = (
            f'ALTER TABLE "{table}" ALTER COLUMN "{old}" TYPE bigint '
            f'USING "{old}" * {SCALE}'
        )
        rename = f"ALTER TABLE {table} RENAME {old} TO {new}"
        assert rescale in sql, f"missing rescale for {table}.{old}"
        assert rename in sql, f"missing rename for {table}.{old}"
        assert sql.index(rescale) < sql.index(rename), (
            f"{table}.{old} is renamed before it is rescaled — a crash between "
            "the two would leave an unrescaled amount under the new name"
        )


def test_columns_covers_exactly_the_models_amount_columns() -> None:
    """The migration's hand-written list against the ORM metadata."""
    import app_shared.models  # noqa: F401  (registers every mapper)
    from app_shared.models import Base

    from_metadata = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.name.endswith("_cost_micro_units")
    }
    from_migration = {(table, new) for table, _old, new in COLUMNS}

    assert from_metadata == from_migration


def test_the_allocation_total_trigger_function_is_recreated() -> None:
    """Renaming a column does not rewrite a plpgsql body."""
    sql = _upgrade_sql()

    last_definition = sql.rindex(
        "CREATE OR REPLACE FUNCTION network_operation_allocations_check_total()"
    )
    body = sql[last_definition:]
    assert "SELECT o.estimated_cost_micro_units" in body
    assert "SUM(a.allocated_cost_micro_units)" in body
    # And the recreation comes AFTER every rename, or it would be replaced
    # by nothing and reference columns that do not exist yet.
    for table, old, new in COLUMNS:
        assert sql.index(f"ALTER TABLE {table} RENAME {old} TO {new}") < last_definition
