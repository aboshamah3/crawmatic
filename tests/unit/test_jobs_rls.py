"""RLS DDL render tests for the jobs tables (SPEC-08 T014, FR-004, SC-006).

Pure string assertions against `emit_rls_policy(...)` for `scrape_jobs`
and `scrape_job_targets` — no database. Mirrors
`tests/unit/test_rls_observations.py` (SPEC-07) / `test_rls_competitors_
matches.py` (SPEC-05): proves both applications render ENABLE + FORCE +
the fail-closed `NULLIF(current_setting('app.workspace_id', true),
'')::uuid` predicate — exactly what the migration
(`alembic/versions/a6b0234cd4ad_scrape_jobs_targets_tables.py`) executes.
"""

from __future__ import annotations

import pytest

from app_shared.models import emit_rls_policy

FAIL_CLOSED_PREDICATE = (
    "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
)

JOBS_TABLES = ["scrape_jobs", "scrape_job_targets"]


@pytest.mark.parametrize("table_name", JOBS_TABLES)
def test_emit_rls_policy_returns_three_statements(table_name: str) -> None:
    statements = emit_rls_policy(table_name)
    assert len(statements) == 3


@pytest.mark.parametrize("table_name", JOBS_TABLES)
def test_emit_rls_policy_enables_and_forces_rls(table_name: str) -> None:
    enable_stmt, force_stmt, _ = emit_rls_policy(table_name)
    assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in enable_stmt
    assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in force_stmt


@pytest.mark.parametrize("table_name", JOBS_TABLES)
def test_emit_rls_policy_predicate_is_fail_closed(table_name: str) -> None:
    _, _, policy_stmt = emit_rls_policy(table_name)
    assert "CREATE POLICY" in policy_stmt
    assert table_name in policy_stmt
    assert FAIL_CLOSED_PREDICATE in policy_stmt


def test_both_tables_have_distinct_policy_names() -> None:
    policy_stmts = [emit_rls_policy(table_name)[2] for table_name in JOBS_TABLES]
    for table_name, stmt in zip(JOBS_TABLES, policy_stmts):
        assert f"CREATE POLICY {table_name}_workspace_isolation ON {table_name}" in stmt
    assert len({stmt for stmt in policy_stmts}) == len(JOBS_TABLES)


def test_six_statements_across_both_tables() -> None:
    """The migration executes exactly six RLS statements (3 per table x 2)."""
    statements: list[str] = []
    for table_name in JOBS_TABLES:
        statements.extend(emit_rls_policy(table_name))
    assert len(statements) == 6
