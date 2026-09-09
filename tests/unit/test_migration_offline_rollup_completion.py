"""Offline migration render test for `rollup_completion` (EPA C7, F12).

The repo's standard guard for a new revision (the
`tests/unit/test_migration_offline_rollups.py` precedent): runs `alembic
upgrade head --sql` offline — no DB connection — and asserts the rendered
DDL, the single head, and that this revision chains from the head that
existed when it was written.

Why the columns are asserted individually: `complete` is the column C8's
retention gate reads before allowing a `price_observations` partition to
be dropped. If it were rendered nullable, or without its `false` default,
a row could exist that neither asserts nor denies the day is finished —
and the safe reading of that ambiguity is not something a downstream
gate should have to guess at.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The single head this revision chains from (EPA C5's last migration).
EXPECTED_DOWN_REVISION = "e2a4f80c6b71"
REVISION = "b3f0c95a7d21"


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


def test_offline_upgrade_head_renders_the_rollup_completion_table() -> None:
    assert "CREATE TABLE rollup_completion" in _upgrade_sql()


def test_the_day_is_the_primary_key_with_no_surrogate_id() -> None:
    """The natural key IS the identity (the `rollup_watermarks`
    precedent) — a surrogate `id` would allow two rows for one day."""
    create = _create_statement(_upgrade_sql())
    assert "CONSTRAINT pk_rollup_completion PRIMARY KEY (date)" in create
    assert " id " not in create


def test_complete_is_not_null_and_defaults_to_false() -> None:
    create = _create_statement(_upgrade_sql())
    assert "complete BOOLEAN DEFAULT false NOT NULL" in create


def test_the_keyset_cursor_columns_are_nullable_uuids() -> None:
    """NULL means "start from the beginning" — a real position and not a
    missing value, so it must be representable."""
    create = _create_statement(_upgrade_sql())
    assert "last_key_workspace_id UUID" in create
    assert "last_key_variant_id UUID" in create
    assert "last_key_workspace_id UUID NOT NULL" not in create
    assert "last_key_variant_id UUID NOT NULL" not in create


def test_the_cursor_columns_are_not_foreign_keys() -> None:
    """A cursor is a scan POSITION. An FK would make deleting a workspace
    delete the record of how far a past day's rollup got."""
    create = _create_statement(_upgrade_sql())
    assert "FOREIGN KEY" not in create
    assert "REFERENCES" not in create


def test_the_table_gets_no_rls_policy() -> None:
    """Global, no `workspace_id` — one rollup day is one cross-tenant
    window (the `rollup_watermarks`/`maintenance_cadences` shape)."""
    sql = _upgrade_sql()
    assert "ENABLE ROW LEVEL SECURITY" not in _statements_naming(sql, "rollup_completion")
    # `last_key_workspace_id` is a scan position, not the RLS anchor: no
    # bare `workspace_id` column exists for a policy to filter on.
    create = _create_statement(sql)
    assert "\n    workspace_id" not in create
    assert "(workspace_id" not in create


def test_revision_chains_from_the_recorded_head() -> None:
    module = (
        REPO_ROOT / "alembic" / "versions" / f"{REVISION}_rollup_completion.py"
    ).read_text()
    assert f"revision: str = '{REVISION}'" in module
    assert f"down_revision: Union[str, Sequence[str], None] = '{EXPECTED_DOWN_REVISION}'" in module


def test_exactly_one_head() -> None:
    result = _run_alembic("heads")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("(head)") == 1, result.stdout


def _create_statement(sql: str) -> str:
    start = sql.index("CREATE TABLE rollup_completion")
    return sql[start : sql.index(");", start)]


def _statements_naming(sql: str, table: str) -> str:
    return "\n".join(line for line in sql.splitlines() if table in line)
