"""RLS DDL render tests for the identity tables (SPEC-03 T040, FR-004/FR-019).

Pure string assertions against `emit_rls_policy("users")`,
`emit_rls_policy("api_keys")` and — since EPA B8b —
`emit_fk_transitive_rls_policy("refresh_tokens", ...)`. No database.
Complements `tests/unit/test_rls_policy.py` (SPEC-02, generic renderer)
by proving the three concrete identity applications each render ENABLE +
FORCE + the fail-closed `NULLIF(current_setting('app.workspace_id',
true), '')::uuid` predicate — exactly what the migrations
(`alembic/versions/55da7d6d939d_auth_identity_tables.py` and
`alembic/versions/b6d94c2f1a70_refresh_tokens_transitive_rls.py`)
execute.

The third table is the one that differs in shape: `refresh_tokens` has
no `workspace_id` column, so its predicate reaches the tenant through
the `users` parent rather than filtering a column of its own.
"""

from __future__ import annotations

import pytest

from app_shared.models import emit_fk_transitive_rls_policy, emit_rls_policy

FAIL_CLOSED_PREDICATE = (
    "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
)


@pytest.mark.parametrize("table_name", ["users", "api_keys"])
def test_emit_rls_policy_returns_three_statements(table_name: str) -> None:
    statements = emit_rls_policy(table_name)
    assert len(statements) == 3


@pytest.mark.parametrize("table_name", ["users", "api_keys"])
def test_emit_rls_policy_enables_and_forces_rls(table_name: str) -> None:
    enable_stmt, force_stmt, _ = emit_rls_policy(table_name)
    assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in enable_stmt
    assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in force_stmt


@pytest.mark.parametrize("table_name", ["users", "api_keys"])
def test_emit_rls_policy_predicate_is_fail_closed(table_name: str) -> None:
    _, _, policy_stmt = emit_rls_policy(table_name)
    assert "CREATE POLICY" in policy_stmt
    assert table_name in policy_stmt
    assert FAIL_CLOSED_PREDICATE in policy_stmt


def test_users_and_api_keys_policies_have_distinct_names() -> None:
    _, _, users_policy = emit_rls_policy("users")
    _, _, api_keys_policy = emit_rls_policy("api_keys")
    assert "CREATE POLICY users_workspace_isolation ON users" in users_policy
    assert "CREATE POLICY api_keys_workspace_isolation ON api_keys" in api_keys_policy


def test_all_six_statements_present_across_both_tables() -> None:
    """The migration executes exactly six RLS statements (3 per table)."""
    statements = list(emit_rls_policy("users")) + list(emit_rls_policy("api_keys"))
    assert len(statements) == 6


# --- refresh_tokens: TRANSITIVE through users.user_id (EPA B8b) --------
#
# The third credential table. It carries no `workspace_id` of its own, so
# `emit_rls_policy` would have nothing to filter on — isolation is
# anchored through its real FK to `users`, the same shape
# `match_audit_classifications` and `strategy_attempt_stats` use. These
# assertions mirror what `alembic/versions/
# b6d94c2f1a70_refresh_tokens_transitive_rls.py` executes.

REFRESH_TOKENS_STATEMENTS = emit_fk_transitive_rls_policy(
    "refresh_tokens", parent_table="users", fk_column="user_id"
)


def test_refresh_tokens_policy_enables_and_forces_rls() -> None:
    enable_stmt, force_stmt, _ = REFRESH_TOKENS_STATEMENTS
    assert "ALTER TABLE refresh_tokens ENABLE ROW LEVEL SECURITY" in enable_stmt
    # FORCE matters more here than anywhere: without it the table owner
    # is exempt, and the owner is the role migrations run as.
    assert "ALTER TABLE refresh_tokens FORCE ROW LEVEL SECURITY" in force_stmt


def test_refresh_tokens_policy_scopes_through_the_users_parent() -> None:
    _, _, policy_stmt = REFRESH_TOKENS_STATEMENTS
    assert "CREATE POLICY refresh_tokens_workspace_isolation ON refresh_tokens" in policy_stmt
    assert "EXISTS (SELECT 1 FROM users p" in policy_stmt
    assert "p.id = refresh_tokens.user_id" in policy_stmt
    assert f"p.{FAIL_CLOSED_PREDICATE}" in policy_stmt


def test_refresh_tokens_policy_never_filters_on_a_column_it_does_not_have() -> None:
    """A `workspace_id = ...` predicate here would be DDL that cannot execute."""
    _, _, policy_stmt = REFRESH_TOKENS_STATEMENTS
    assert "refresh_tokens.workspace_id" not in policy_stmt
