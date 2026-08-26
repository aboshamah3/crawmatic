"""Offline migration render test for the W3.1 `offer_observation_fields` migration.

Mirrors `tests/unit/test_migration_offline_rollups.py`: runs alembic
`--sql` (offline, no DB connection) via subprocess and asserts what the
migration actually renders, rather than trusting its Python source.

The thing worth pinning here is *symmetry*. `03e34c8406cd` adds 33
nullable `offer_*` columns to the partitioned `price_observations`
parent from one `_OFFER_COLUMNS` list, and its downgrade walks that same
list reversed. A hand-edit that adds a column to the upgrade and forgets
the downgrade leaves a migration that cannot be rolled back — this test
counts both sides and compares the two column sets.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

REVISION = "03e34c8406cd"
DOWN_REVISION = "c4b19e7a2f08"
TABLE = "price_observations"

#: The count stated in the migration's own docstring and in
#: `app_shared.models.observations.PriceObservation`'s W3.1 block.
EXPECTED_OFFER_COLUMNS = 33

_ADD_COLUMN_RE = re.compile(rf"ALTER TABLE {TABLE} ADD COLUMN (\w+) ")
_DROP_COLUMN_RE = re.compile(rf"ALTER TABLE {TABLE} DROP COLUMN (\w+)")


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _upgrade_sql() -> str:
    result = _run_alembic("upgrade", f"{DOWN_REVISION}:{REVISION}", "--sql")
    assert result.returncode == 0, (
        f"alembic upgrade --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


def _downgrade_sql() -> str:
    result = _run_alembic("downgrade", f"{REVISION}:{DOWN_REVISION}", "--sql")
    assert result.returncode == 0, (
        f"alembic downgrade --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


def test_upgrade_renders_thirty_three_add_column_statements() -> None:
    added = _ADD_COLUMN_RE.findall(_upgrade_sql())

    assert len(added) == EXPECTED_OFFER_COLUMNS, added
    assert all(name.startswith("offer_") for name in added), added
    # Every column named once — a duplicated entry would render twice and
    # fail on the second `ADD COLUMN` against a real database.
    assert len(set(added)) == EXPECTED_OFFER_COLUMNS


def test_downgrade_renders_thirty_three_drop_column_statements() -> None:
    dropped = _DROP_COLUMN_RE.findall(_downgrade_sql())

    assert len(dropped) == EXPECTED_OFFER_COLUMNS, dropped
    assert len(set(dropped)) == EXPECTED_OFFER_COLUMNS


def test_upgrade_and_downgrade_are_symmetric() -> None:
    """Same columns both ways, dropped in reverse order.

    Reverse order is not cosmetic: it is the evidence that both sides are
    generated from the one `_OFFER_COLUMNS` list rather than
    independently maintained.
    """
    added = _ADD_COLUMN_RE.findall(_upgrade_sql())
    dropped = _DROP_COLUMN_RE.findall(_downgrade_sql())

    assert set(added) == set(dropped)
    assert dropped == list(reversed(added))


def test_columns_are_added_to_the_partitioned_parent_only() -> None:
    """`ALTER TABLE ... ADD COLUMN` on a RANGE-partitioned parent
    propagates to every existing and future partition, so a per-partition
    loop would be both redundant and a source of drift. Nothing may
    target a `price_observations_YYYY_MM` partition directly."""
    sql = _upgrade_sql()

    assert re.search(rf"ALTER TABLE {TABLE}_\d{{4}}_\d{{2}}", sql) is None, sql


def test_columns_are_nullable() -> None:
    """§7: unknown is NULL, never 0/"" — and a NOT NULL add against a
    populated partitioned table would need a rewrite this migration
    deliberately does not do."""
    sql = _upgrade_sql()

    for statement in sql.splitlines():
        if f"ALTER TABLE {TABLE} ADD COLUMN" in statement:
            assert "NOT NULL" not in statement, statement


def test_down_revision_is_the_w3_1_predecessor() -> None:
    result = _run_alembic("history")
    assert result.returncode == 0, (
        f"alembic history failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert f"{DOWN_REVISION} -> {REVISION}" in result.stdout
