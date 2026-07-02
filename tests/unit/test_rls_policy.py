"""RLS DDL-emitter tests (FR-007, [analyze I2]).

Pure string assertions against the rendered DDL — no database. The key
proof is the fail-closed predicate: ``NULLIF(current_setting(
'app.workspace_id', true), '')::uuid`` must be present verbatim so an
empty (as well as absent) workspace context maps to ``NULL`` (zero
rows) instead of raising ``invalid input syntax for type uuid: ""``.
"""

from __future__ import annotations

from app_shared.models import emit_rls_policy


def test_emit_rls_policy_returns_three_statements() -> None:
    statements = emit_rls_policy("some_table")
    assert len(statements) == 3


def test_emit_rls_policy_enables_and_forces_rls() -> None:
    enable_stmt, force_stmt, _ = emit_rls_policy("some_table")
    assert "ALTER TABLE some_table ENABLE ROW LEVEL SECURITY" in enable_stmt
    assert "ALTER TABLE some_table FORCE ROW LEVEL SECURITY" in force_stmt


def test_emit_rls_policy_predicate_is_fail_closed_via_nullif() -> None:
    _, _, policy_stmt = emit_rls_policy("some_table")

    assert "CREATE POLICY" in policy_stmt
    assert "some_table" in policy_stmt
    # The NULLIF(..., '') wrapper is REQUIRED: it is what makes both an
    # absent AND an empty app.workspace_id map to NULL (zero rows)
    # instead of raising `''::uuid` on an empty context.
    assert "NULLIF(" in policy_stmt
    assert ", '')" in policy_stmt
    assert (
        "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
        in policy_stmt
    )


def test_emit_rls_policy_default_policy_name() -> None:
    _, _, policy_stmt = emit_rls_policy("some_table")
    assert "CREATE POLICY some_table_workspace_isolation ON some_table" in policy_stmt


def test_emit_rls_policy_custom_policy_name_and_column() -> None:
    _, _, policy_stmt = emit_rls_policy(
        "other_table", workspace_column="tenant_id", policy_name="custom_policy"
    )
    assert "CREATE POLICY custom_policy ON other_table" in policy_stmt
    assert "tenant_id = NULLIF(" in policy_stmt
