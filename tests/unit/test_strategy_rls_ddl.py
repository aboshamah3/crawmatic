"""RLS DDL render tests for the SPEC-12 domain-strategy-optimizer tables
(FR-026, SC-005, contracts/rls-and-migration.md).

Pure string assertions against ``emit_rls_policy``/``emit_fk_transitive_rls_policy``
for ``domain_strategy_profiles``, ``strategy_discovery_runs`` (standard,
own ``workspace_id``) and ``strategy_attempt_stats`` (transitive, via its
FK to ``domain_strategy_profiles``) — no database. Mirrors
``tests/unit/test_rls_competitors_matches.py`` and proves exactly what
the migration (``alembic/versions/f30c60cfa2f7_domain_strategy_optimizer_tables.py``)
executes.
"""

from __future__ import annotations

import pytest

from app_shared.models import emit_fk_transitive_rls_policy, emit_rls_policy

FAIL_CLOSED_PREDICATE = (
    "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
)

STANDARD_RLS_TABLES = ["domain_strategy_profiles", "strategy_discovery_runs"]

TRANSITIVE_PREDICATE = (
    "EXISTS (SELECT 1 FROM domain_strategy_profiles p "
    "WHERE p.id = strategy_attempt_stats.domain_strategy_profile_id "
    "AND p.workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)"
)


# --- Standard emit_rls_policy on the two workspace-owned tables --------


@pytest.mark.parametrize("table_name", STANDARD_RLS_TABLES)
def test_emit_rls_policy_returns_three_statements(table_name: str) -> None:
    statements = emit_rls_policy(table_name)
    assert len(statements) == 3


@pytest.mark.parametrize("table_name", STANDARD_RLS_TABLES)
def test_emit_rls_policy_enables_and_forces_rls(table_name: str) -> None:
    enable_stmt, force_stmt, _ = emit_rls_policy(table_name)
    assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in enable_stmt
    assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in force_stmt


@pytest.mark.parametrize("table_name", STANDARD_RLS_TABLES)
def test_emit_rls_policy_predicate_is_fail_closed(table_name: str) -> None:
    _, _, policy_stmt = emit_rls_policy(table_name)
    assert "CREATE POLICY" in policy_stmt
    assert table_name in policy_stmt
    assert FAIL_CLOSED_PREDICATE in policy_stmt


def test_both_workspace_owned_tables_have_distinct_policy_names() -> None:
    policy_stmts = [emit_rls_policy(table_name)[2] for table_name in STANDARD_RLS_TABLES]
    for table_name, stmt in zip(STANDARD_RLS_TABLES, policy_stmts):
        assert f"CREATE POLICY {table_name}_workspace_isolation ON {table_name}" in stmt
    assert len({stmt for stmt in policy_stmts}) == len(STANDARD_RLS_TABLES)


# --- New emit_fk_transitive_rls_policy on strategy_attempt_stats -------


def test_emit_fk_transitive_rls_policy_returns_three_statements() -> None:
    statements = emit_fk_transitive_rls_policy(
        "strategy_attempt_stats",
        parent_table="domain_strategy_profiles",
        fk_column="domain_strategy_profile_id",
    )
    assert len(statements) == 3


def test_emit_fk_transitive_rls_policy_enables_and_forces_rls() -> None:
    enable_stmt, force_stmt, _ = emit_fk_transitive_rls_policy(
        "strategy_attempt_stats",
        parent_table="domain_strategy_profiles",
        fk_column="domain_strategy_profile_id",
    )
    assert "ALTER TABLE strategy_attempt_stats ENABLE ROW LEVEL SECURITY" in enable_stmt
    assert "ALTER TABLE strategy_attempt_stats FORCE ROW LEVEL SECURITY" in force_stmt


def test_emit_fk_transitive_rls_policy_predicate_is_exact() -> None:
    _, _, policy_stmt = emit_fk_transitive_rls_policy(
        "strategy_attempt_stats",
        parent_table="domain_strategy_profiles",
        fk_column="domain_strategy_profile_id",
    )
    assert "CREATE POLICY strategy_attempt_stats_workspace_isolation ON strategy_attempt_stats" in (
        policy_stmt
    )
    assert TRANSITIVE_PREDICATE in policy_stmt
    # The fail-closed NULLIF guard is REQUIRED (same as emit_rls_policy):
    # absent or empty workspace context -> NULL -> the EXISTS is never
    # true for any parent row -> zero rows, never an error.
    assert "NULLIF(" in policy_stmt
    assert ", '')" in policy_stmt


def test_emit_fk_transitive_rls_policy_custom_names() -> None:
    _, _, policy_stmt = emit_fk_transitive_rls_policy(
        "child_table",
        parent_table="parent_table",
        fk_column="parent_id",
        parent_pk="uuid",
        workspace_column="tenant_id",
        policy_name="custom_policy",
    )
    assert "CREATE POLICY custom_policy ON child_table" in policy_stmt
    assert (
        "EXISTS (SELECT 1 FROM parent_table p WHERE p.uuid = child_table.parent_id "
        "AND p.tenant_id = NULLIF(" in policy_stmt
    )


def test_nine_statements_across_all_three_tables() -> None:
    """The migration executes exactly nine RLS statements (3 per table x 3)."""
    statements: list[str] = []
    statements.extend(emit_rls_policy("domain_strategy_profiles"))
    statements.extend(
        emit_fk_transitive_rls_policy(
            "strategy_attempt_stats",
            parent_table="domain_strategy_profiles",
            fk_column="domain_strategy_profile_id",
        )
    )
    statements.extend(emit_rls_policy("strategy_discovery_runs"))
    assert len(statements) == 9
