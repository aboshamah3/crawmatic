"""RLS DDL render tests for the catalog tables (SPEC-04 T010, FR-003/SC-004).

Pure string assertions against `emit_rls_policy(...)` for the four
catalog tables — no database. Complements `tests/unit/test_rls_policy.py`
(SPEC-02, generic renderer) and `tests/unit/test_rls_identity.py`
(SPEC-03) by proving all four catalog applications each render
ENABLE + FORCE + the fail-closed
`NULLIF(current_setting('app.workspace_id', true), '')::uuid` predicate
— exactly what the migration
(`alembic/versions/c2987b29555e_catalog_tables.py`) executes.
"""

from __future__ import annotations

import pytest

from app_shared.models import emit_rls_policy

FAIL_CLOSED_PREDICATE = (
    "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
)

CATALOG_TABLES = ["products", "product_variants", "product_groups", "product_group_items"]


@pytest.mark.parametrize("table_name", CATALOG_TABLES)
def test_emit_rls_policy_returns_three_statements(table_name: str) -> None:
    statements = emit_rls_policy(table_name)
    assert len(statements) == 3


@pytest.mark.parametrize("table_name", CATALOG_TABLES)
def test_emit_rls_policy_enables_and_forces_rls(table_name: str) -> None:
    enable_stmt, force_stmt, _ = emit_rls_policy(table_name)
    assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in enable_stmt
    assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in force_stmt


@pytest.mark.parametrize("table_name", CATALOG_TABLES)
def test_emit_rls_policy_predicate_is_fail_closed(table_name: str) -> None:
    _, _, policy_stmt = emit_rls_policy(table_name)
    assert "CREATE POLICY" in policy_stmt
    assert table_name in policy_stmt
    assert FAIL_CLOSED_PREDICATE in policy_stmt


def test_all_four_catalog_tables_have_distinct_policy_names() -> None:
    policy_stmts = [emit_rls_policy(table_name)[2] for table_name in CATALOG_TABLES]
    for table_name, stmt in zip(CATALOG_TABLES, policy_stmts):
        assert f"CREATE POLICY {table_name}_workspace_isolation ON {table_name}" in stmt
    assert len({stmt for stmt in policy_stmts}) == len(CATALOG_TABLES)


def test_twelve_statements_across_all_four_catalog_tables() -> None:
    """The migration executes exactly twelve RLS statements (3 per table x 4)."""
    statements: list[str] = []
    for table_name in CATALOG_TABLES:
        statements.extend(emit_rls_policy(table_name))
    assert len(statements) == 12
