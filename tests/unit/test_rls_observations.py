"""RLS DDL render tests for the observation/current-price tables (SPEC-07 T014, FR-012).

Pure string assertions against `emit_rls_policy(...)` for
`price_observations`, `request_attempts`, and `match_current_prices` —
no database. Mirrors `tests/unit/test_rls_competitors_matches.py`
(SPEC-05) by proving all three applications render ENABLE + FORCE + the
fail-closed `NULLIF(current_setting('app.workspace_id', true),
'')::uuid` predicate — exactly what the migration
(`alembic/versions/2db33dea5e14_observations_current_prices_tables.py`)
executes. RLS applied to a partitioned parent (`price_observations`,
`request_attempts`) propagates to its partitions, so a single
`emit_rls_policy` call per parent is correct/sufficient.
"""

from __future__ import annotations

import pytest

from app_shared.models import emit_rls_policy

FAIL_CLOSED_PREDICATE = (
    "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
)

OBSERVATIONS_TABLES = ["price_observations", "request_attempts", "match_current_prices"]


@pytest.mark.parametrize("table_name", OBSERVATIONS_TABLES)
def test_emit_rls_policy_returns_three_statements(table_name: str) -> None:
    statements = emit_rls_policy(table_name)
    assert len(statements) == 3


@pytest.mark.parametrize("table_name", OBSERVATIONS_TABLES)
def test_emit_rls_policy_enables_and_forces_rls(table_name: str) -> None:
    enable_stmt, force_stmt, _ = emit_rls_policy(table_name)
    assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in enable_stmt
    assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in force_stmt


@pytest.mark.parametrize("table_name", OBSERVATIONS_TABLES)
def test_emit_rls_policy_predicate_is_fail_closed(table_name: str) -> None:
    _, _, policy_stmt = emit_rls_policy(table_name)
    assert "CREATE POLICY" in policy_stmt
    assert table_name in policy_stmt
    assert FAIL_CLOSED_PREDICATE in policy_stmt


def test_all_three_tables_have_distinct_policy_names() -> None:
    policy_stmts = [emit_rls_policy(table_name)[2] for table_name in OBSERVATIONS_TABLES]
    for table_name, stmt in zip(OBSERVATIONS_TABLES, policy_stmts):
        assert f"CREATE POLICY {table_name}_workspace_isolation ON {table_name}" in stmt
    assert len({stmt for stmt in policy_stmts}) == len(OBSERVATIONS_TABLES)


def test_nine_statements_across_all_three_tables() -> None:
    """The migration executes exactly nine RLS statements (3 per table x 3)."""
    statements: list[str] = []
    for table_name in OBSERVATIONS_TABLES:
        statements.extend(emit_rls_policy(table_name))
    assert len(statements) == 9
