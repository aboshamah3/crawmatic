"""Offline migration render test for the competitors/matches migration (SPEC-05 T009, FR-001/FR-002).

Mirrors `tests/unit/test_migration_offline_catalog.py` (SPEC-04): runs
`alembic upgrade head --sql` (offline, no DB connection) via subprocess
and asserts the rendered SQL contains both `CREATE TABLE`s, the two
competitor unique keys, the 4-column match unique, the three composite
FKs, and the six RLS statements (3 per table x 2). Also asserts
`alembic heads` reports exactly one head — this migration
(`f4c8a391d5c9`) must not fork the linear history, and its
`down_revision` is the SPEC-04 head (`c2987b29555e`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

COMPETITORS_MATCHES_TABLES = ["competitors", "competitor_product_matches"]


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_offline_upgrade_head_renders_both_tables() -> None:
    result = _run_alembic("upgrade", "head", "--sql")

    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    sql = result.stdout
    assert "CREATE TABLE competitors" in sql
    assert "CREATE TABLE competitor_product_matches" in sql


def test_offline_upgrade_head_renders_competitor_unique_keys() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert "uq_competitors_workspace_id_id" in sql
    assert "uq_competitors_workspace_id_domain" in sql


def test_offline_upgrade_head_renders_4_col_match_unique() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert "CONSTRAINT uq_cpm_ws_variant_competitor_norm_url UNIQUE" in sql
    assert (
        "(workspace_id, product_variant_id, competitor_id, normalized_competitor_url)" in sql
    )


def test_offline_upgrade_head_renders_composite_fks() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert "fk_cpm_workspace_product_products" in sql
    assert "fk_cpm_workspace_variant_variants" in sql
    assert "fk_cpm_workspace_competitor_competitors" in sql
    assert "fk_cpm_workspace_workspaces" in sql


def test_offline_upgrade_head_renders_six_rls_statements() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    for table_name in COMPETITORS_MATCHES_TABLES:
        assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY {table_name}_workspace_isolation ON {table_name}" in sql

    assert (
        "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
        in sql
    )


def test_alembic_heads_reports_exactly_one_head() -> None:
    result = _run_alembic("heads")

    assert result.returncode == 0, (
        f"alembic heads failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    head_lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert len(head_lines) == 1, f"expected exactly one head, got: {head_lines!r}"
    assert "(head)" in head_lines[0]
    assert "f4c8a391d5c9" in head_lines[0]


def test_down_revision_is_the_spec04_head() -> None:
    result = _run_alembic("history")
    assert result.returncode == 0, (
        f"alembic history failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "c2987b29555e -> f4c8a391d5c9" in result.stdout
