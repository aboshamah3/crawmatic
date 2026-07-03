"""RLS DDL render tests for `emit_global_readable_rls_policy` (SPEC-06 T013, FR-004/FR-021, SC-007).

Pure string assertions against `emit_global_readable_rls_policy(...)`
for `scrape_profiles` — no database. Proves the dual-scope RLS pair
(read own+global, write own-only) matches `contracts/rls-global-readable.md`
exactly — what the SPEC-06 migration
(`alembic/versions/a4f205e8d7de_scrape_profiles_table.py`) executes.
"""

from __future__ import annotations

from app_shared.models import emit_global_readable_rls_policy

FAIL_CLOSED_CTX = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def test_emit_global_readable_rls_policy_returns_four_statements() -> None:
    statements = emit_global_readable_rls_policy("scrape_profiles")
    assert len(statements) == 4


def test_enables_and_forces_rls() -> None:
    enable_stmt, force_stmt, _, _ = emit_global_readable_rls_policy("scrape_profiles")
    assert "ALTER TABLE scrape_profiles ENABLE ROW LEVEL SECURITY" in enable_stmt
    assert "ALTER TABLE scrape_profiles FORCE ROW LEVEL SECURITY" in force_stmt


def test_select_policy_reads_own_or_global() -> None:
    _, _, read_stmt, _ = emit_global_readable_rls_policy("scrape_profiles")
    assert "CREATE POLICY scrape_profiles_workspace_read ON scrape_profiles" in read_stmt
    assert "FOR SELECT" in read_stmt
    assert "IS NULL OR" in read_stmt
    assert f"workspace_id IS NULL OR workspace_id = {FAIL_CLOSED_CTX}" in read_stmt


def test_write_policy_is_for_all_own_only_using_and_with_check() -> None:
    _, _, _, write_stmt = emit_global_readable_rls_policy("scrape_profiles")
    assert "CREATE POLICY scrape_profiles_workspace_write ON scrape_profiles" in write_stmt
    assert "FOR ALL" in write_stmt
    assert "IS NULL" not in write_stmt
    assert f"USING (workspace_id = {FAIL_CLOSED_CTX})" in write_stmt
    assert f"WITH CHECK (workspace_id = {FAIL_CLOSED_CTX})" in write_stmt


def test_fail_closed_context_expression_present_in_both_policies() -> None:
    _, _, read_stmt, write_stmt = emit_global_readable_rls_policy("scrape_profiles")
    assert FAIL_CLOSED_CTX in read_stmt
    assert FAIL_CLOSED_CTX in write_stmt


def test_workspace_column_override_reflected_in_all_four_statements() -> None:
    statements = emit_global_readable_rls_policy(
        "scrape_profiles", workspace_column="ws_id"
    )
    assert len(statements) == 4
    for stmt in statements:
        assert "workspace_id" not in stmt or "ws_id" in stmt
    enable_stmt, force_stmt, read_stmt, write_stmt = statements
    assert "ws_id IS NULL OR ws_id" in read_stmt
    assert "ws_id =" in write_stmt


def test_emit_rls_policy_untouched_returns_three_statements() -> None:
    # emit_rls_policy (the standard single-policy emitter) is unaffected
    # by the new dual-scope emitter living alongside it.
    from app_shared.models import emit_rls_policy

    assert len(emit_rls_policy("scrape_profiles")) == 3
