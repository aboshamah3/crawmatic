"""Offline migration render test for the domain-strategy-optimizer migration
(SPEC-12 T013, FR-026/FR-027/FR-028, `contracts/rls-and-migration.md`).

Mirrors `tests/unit/test_migration_offline_access.py` (SPEC-10): runs
`alembic upgrade head --sql` (offline, no DB connection) via subprocess
and asserts the rendered SQL contains the `domain_strategy_profiles`/
`strategy_attempt_stats`/`strategy_discovery_runs` `CREATE TABLE`
statements, their unique constraints/indexes, the standard
`emit_rls_policy` statements for the two workspace-owned tables, the
transitive `emit_fk_transitive_rls_policy` statement for
`strategy_attempt_stats`, that `alembic heads` yields a single head, and
that `down_revision == "851220acab90"` (the SPEC-10 head).
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
    assert "CREATE TABLE domain_strategy_profiles" in sql
    assert "CREATE TABLE strategy_attempt_stats" in sql
    assert "CREATE TABLE strategy_discovery_runs" in sql


def test_offline_upgrade_head_renders_domain_strategy_profiles_constraints() -> None:
    sql = _upgrade_sql()
    assert (
        "CONSTRAINT uq_dsp_ws_competitor_domain_pattern UNIQUE "
        "(workspace_id, competitor_id, domain, url_pattern)" in sql
    )
    assert "fk_dsp_workspace_competitor_competitors" in sql
    assert "fk_domain_strategy_profiles_workspace_id_workspaces" in sql
    assert "CREATE INDEX ix_domain_strategy_profiles_workspace_id ON domain_strategy_profiles" in sql
    assert (
        "CREATE INDEX ix_dsp_ws_competitor_domain_pattern_version "
        "ON domain_strategy_profiles (workspace_id, competitor_id, domain, url_pattern, "
        "url_pattern_version)" in sql
    )


def test_offline_upgrade_head_renders_strategy_attempt_stats_constraints() -> None:
    sql = _upgrade_sql()
    assert (
        "CONSTRAINT uq_sas_profile_method_type_name UNIQUE "
        "(domain_strategy_profile_id, method_type, method_name)" in sql
    )
    assert "fk_sas_profile_id_domain_strategy_profiles" in sql
    assert (
        "CREATE INDEX ix_strategy_attempt_stats_domain_strategy_profile_id "
        "ON strategy_attempt_stats" in sql
    )
    # No workspace_id column of its own (research D3).
    assert "strategy_attempt_stats" in sql
    create_stmt_start = sql.index("CREATE TABLE strategy_attempt_stats")
    create_stmt_end = sql.index(")", sql.index("PRIMARY KEY", create_stmt_start))
    create_stmt = sql[create_stmt_start:create_stmt_end]
    assert "workspace_id" not in create_stmt


def test_offline_upgrade_head_renders_strategy_discovery_runs_constraints() -> None:
    sql = _upgrade_sql()
    assert "fk_sdr_workspace_competitor_competitors" in sql
    assert "fk_strategy_discovery_runs_workspace_id_workspaces" in sql
    assert "CREATE INDEX ix_strategy_discovery_runs_workspace_id ON strategy_discovery_runs" in sql
    assert (
        "CREATE INDEX ix_sdr_ws_competitor_domain_pattern "
        "ON strategy_discovery_runs (workspace_id, competitor_id, domain, url_pattern)" in sql
    )


def test_offline_upgrade_head_renders_standard_rls_for_workspace_owned_tables() -> None:
    sql = _upgrade_sql()
    for table in ("domain_strategy_profiles", "strategy_discovery_runs"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
        assert (
            f"CREATE POLICY {table}_workspace_isolation ON {table} "
            f"USING (workspace_id = {FAIL_CLOSED_CTX})" in sql
        )


def test_offline_upgrade_head_renders_transitive_rls_for_strategy_attempt_stats() -> None:
    sql = _upgrade_sql()
    assert "ALTER TABLE strategy_attempt_stats ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE strategy_attempt_stats FORCE ROW LEVEL SECURITY" in sql
    assert (
        "CREATE POLICY strategy_attempt_stats_workspace_isolation ON strategy_attempt_stats "
        "USING (EXISTS (SELECT 1 FROM domain_strategy_profiles p "
        "WHERE p.id = strategy_attempt_stats.domain_strategy_profile_id "
        f"AND p.workspace_id = {FAIL_CLOSED_CTX}))" in sql
    )


def test_downgrade_drops_tables_in_reverse_order() -> None:
    result = _run_alembic("downgrade", "f30c60cfa2f7:851220acab90", "--sql")
    assert result.returncode == 0, (
        f"alembic downgrade --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    sql = result.stdout
    assert "DROP TABLE strategy_discovery_runs" in sql
    assert "DROP TABLE strategy_attempt_stats" in sql
    assert "DROP TABLE domain_strategy_profiles" in sql
    runs_pos = sql.index("DROP TABLE strategy_discovery_runs")
    stats_pos = sql.index("DROP TABLE strategy_attempt_stats")
    profiles_pos = sql.index("DROP TABLE domain_strategy_profiles")
    assert runs_pos < stats_pos < profiles_pos


def test_alembic_heads_reports_exactly_one_head() -> None:
    result = _run_alembic("heads")

    assert result.returncode == 0, (
        f"alembic heads failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    head_lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert len(head_lines) == 1, f"expected exactly one head, got: {head_lines!r}"
    assert "(head)" in head_lines[0]


def test_down_revision_is_the_spec10_head() -> None:
    result = _run_alembic("history")
    assert result.returncode == 0, (
        f"alembic history failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    matching_lines = [
        line for line in result.stdout.splitlines() if line.startswith("851220acab90 -> ")
    ]
    assert matching_lines, result.stdout
