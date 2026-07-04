"""Offline migration render test for the access/proxy migration (SPEC-10
T013, FR-006/FR-014, `contracts/migration-access.md`).

Mirrors `tests/unit/test_migration_offline_alerts.py` (SPEC-09): runs
`alembic upgrade head --sql` (offline, no DB connection) via subprocess
and asserts the rendered SQL contains the `proxy_providers`/
`access_policies`/`domain_access_rules` `CREATE TABLE` statements, both
partial-unique namespaces on each dual-scope table, the
`COALESCE(url_pattern, '')` expression unique index, the dual
read/write `emit_global_readable_rls_policy` statements for the two
dual-scope tables, the single fail-closed `emit_rls_policy` for
`domain_access_rules`, that `request_attempts` is absent from this
migration's diff (already exists, SPEC-07), that `alembic heads` yields
a single head, and that `down_revision == "e4a75b48360c"`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

FAIL_CLOSED_CTX = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _upgrade_sql() -> str:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


def test_offline_upgrade_head_renders_all_three_tables() -> None:
    sql = _upgrade_sql()
    assert "CREATE TABLE proxy_providers" in sql
    assert "CREATE TABLE access_policies" in sql
    assert "CREATE TABLE domain_access_rules" in sql


def test_offline_upgrade_head_renders_proxy_providers_partial_unique_indexes() -> None:
    sql = _upgrade_sql()
    assert (
        "CREATE UNIQUE INDEX uq_proxy_providers_workspace_id_name "
        "ON proxy_providers (workspace_id, name) WHERE workspace_id IS NOT NULL" in sql
    )
    assert (
        "CREATE UNIQUE INDEX uq_proxy_providers_name_global "
        "ON proxy_providers (name) WHERE workspace_id IS NULL" in sql
    )


def test_offline_upgrade_head_renders_access_policies_partial_unique_indexes() -> None:
    sql = _upgrade_sql()
    assert (
        "CREATE UNIQUE INDEX uq_access_policies_workspace_id_name "
        "ON access_policies (workspace_id, name) WHERE workspace_id IS NOT NULL" in sql
    )
    assert (
        "CREATE UNIQUE INDEX uq_access_policies_name_global "
        "ON access_policies (name) WHERE workspace_id IS NULL" in sql
    )


def test_offline_upgrade_head_renders_domain_access_rules_coalesce_unique_index() -> None:
    sql = _upgrade_sql()
    assert (
        "CREATE UNIQUE INDEX uq_domain_access_rules_ws_cid_domain_pattern "
        "ON domain_access_rules (workspace_id, competitor_id, domain, "
        "COALESCE(url_pattern, ''))" in sql
    )
    assert (
        "CREATE INDEX ix_domain_access_rules_workspace_id_competitor_id_domain "
        "ON domain_access_rules (workspace_id, competitor_id, domain)" in sql
    )


def test_offline_upgrade_head_renders_workspace_id_indexes_and_fks() -> None:
    sql = _upgrade_sql()
    assert "CREATE INDEX ix_proxy_providers_workspace_id ON proxy_providers" in sql
    assert "fk_proxy_providers_workspace_id_workspaces" in sql
    assert "CREATE INDEX ix_access_policies_workspace_id ON access_policies" in sql
    assert "fk_access_policies_workspace_id_workspaces" in sql
    assert "CREATE INDEX ix_domain_access_rules_workspace_id ON domain_access_rules" in sql
    assert "fk_domain_access_rules_workspace_id_workspaces" in sql


def test_offline_upgrade_head_renders_dual_scope_rls_for_proxy_providers_and_access_policies() -> (
    None
):
    sql = _upgrade_sql()
    for table in ("proxy_providers", "access_policies"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY {table}_workspace_read ON {table} FOR SELECT" in sql
        assert f"workspace_id IS NULL OR workspace_id = {FAIL_CLOSED_CTX}" in sql
        assert f"CREATE POLICY {table}_workspace_write ON {table} FOR ALL" in sql
        assert f"USING (workspace_id = {FAIL_CLOSED_CTX})" in sql
        assert f"WITH CHECK (workspace_id = {FAIL_CLOSED_CTX})" in sql


def test_offline_upgrade_head_renders_fail_closed_rls_for_domain_access_rules() -> None:
    sql = _upgrade_sql()
    assert "ALTER TABLE domain_access_rules ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE domain_access_rules FORCE ROW LEVEL SECURITY" in sql
    assert (
        "CREATE POLICY domain_access_rules_workspace_isolation ON domain_access_rules "
        f"USING (workspace_id = {FAIL_CLOSED_CTX})" in sql
    )


def test_request_attempts_is_absent_from_this_migration() -> None:
    # Locate the diff for THIS migration specifically (the SQL emitted
    # after its "-- Running upgrade e4a75b48360c -> ..." marker), not the
    # full multi-migration history — request_attempts legitimately
    # appears earlier in history (SPEC-07) but must not reappear here.
    sql = _upgrade_sql()
    upgrade_section = sql.split("-- Running upgrade e4a75b48360c -> ", 1)[-1]
    assert "request_attempts" not in upgrade_section
    assert "ADD COLUMN" not in upgrade_section


def test_alembic_heads_reports_exactly_one_head() -> None:
    result = _run_alembic("heads")

    assert result.returncode == 0, (
        f"alembic heads failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    head_lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert len(head_lines) == 1, f"expected exactly one head, got: {head_lines!r}"
    assert "(head)" in head_lines[0]


def test_down_revision_is_the_spec09_head() -> None:
    result = _run_alembic("history")
    assert result.returncode == 0, (
        f"alembic history failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "e4a75b48360c -> " in result.stdout
    # The new revision's down_revision is the SPEC-09 head.
    matching_lines = [
        line for line in result.stdout.splitlines() if line.startswith("e4a75b48360c -> ")
    ]
    assert matching_lines, result.stdout
