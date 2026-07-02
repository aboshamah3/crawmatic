"""Offline migration render test for the identity migration (SPEC-03 T043, FR-004/FR-023).

Mirrors `tests/unit/test_migration_offline.py` (SPEC-02): runs `alembic
upgrade head --sql` (offline, no DB connection — see
`alembic/env.py::run_migrations_offline`) via subprocess and asserts the
rendered SQL contains the four `CREATE TABLE`s
(`workspaces`/`users`/`refresh_tokens`/`api_keys`) plus the six RLS
statements (`emit_rls_policy("users")` + `emit_rls_policy("api_keys")`,
3 each). Also asserts `alembic heads` reports exactly one head — this
migration (`55da7d6d939d`) must not fork the linear history established
in SPEC-02.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_offline_upgrade_head_renders_the_four_identity_tables() -> None:
    result = _run_alembic("upgrade", "head", "--sql")

    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    sql = result.stdout
    assert "CREATE TABLE workspaces" in sql
    assert "CREATE TABLE users" in sql
    assert "CREATE TABLE refresh_tokens" in sql
    assert "CREATE TABLE api_keys" in sql


def test_offline_upgrade_head_renders_the_six_rls_statements() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    for table_name in ("users", "api_keys"):
        assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY {table_name}_workspace_isolation ON {table_name}" in sql

    # The fail-closed predicate, verbatim, for at least one of the tables.
    assert (
        "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
        in sql
    )


def test_offline_upgrade_head_does_not_enable_rls_on_workspaces_or_refresh_tokens() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert "ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY" not in sql
    assert "ALTER TABLE refresh_tokens ENABLE ROW LEVEL SECURITY" not in sql


def test_alembic_heads_reports_exactly_one_head() -> None:
    result = _run_alembic("heads")

    assert result.returncode == 0, (
        f"alembic heads failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    head_lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert len(head_lines) == 1, f"expected exactly one head, got: {head_lines!r}"
    assert "(head)" in head_lines[0]
    assert "55da7d6d939d" in head_lines[0]
