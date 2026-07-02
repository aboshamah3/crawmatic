"""RLS DDL render tests for the identity tables (SPEC-03 T040, FR-004/FR-019).

Pure string assertions against `emit_rls_policy("users")` and
`emit_rls_policy("api_keys")` — no database. Complements
`tests/unit/test_rls_policy.py` (SPEC-02, generic renderer) by proving
the two concrete identity applications each render ENABLE + FORCE + the
fail-closed `NULLIF(current_setting('app.workspace_id', true), '')::uuid`
predicate — exactly what the migration (`alembic/versions/
55da7d6d939d_auth_identity_tables.py`) executes.
"""

from __future__ import annotations

import pytest

from app_shared.models import emit_rls_policy

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
