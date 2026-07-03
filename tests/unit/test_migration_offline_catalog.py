"""Offline migration render test for the catalog migration (SPEC-04 T012, FR-001/FR-003).

Mirrors `tests/unit/test_migration_offline_auth.py` (SPEC-03): runs
`alembic upgrade head --sql` (offline, no DB connection) via subprocess
and asserts the rendered SQL contains the four catalog `CREATE TABLE`s,
the six partial unique indexes, and the twelve RLS statements (3 per
table x 4). Also asserts `alembic heads` reports exactly one head — this
migration (`c2987b29555e`) must not fork the linear history.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

CATALOG_TABLES = ["products", "product_variants", "product_groups", "product_group_items"]


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_offline_upgrade_head_renders_the_four_catalog_tables() -> None:
    result = _run_alembic("upgrade", "head", "--sql")

    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    sql = result.stdout
    assert "CREATE TABLE products" in sql
    assert "CREATE TABLE product_variants" in sql
    assert "CREATE TABLE product_groups" in sql
    assert "CREATE TABLE product_group_items" in sql


def test_offline_upgrade_head_renders_the_six_partial_unique_indexes() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert (
        "CREATE UNIQUE INDEX uq_products_workspace_id_external_id "
        "ON products (workspace_id, external_id) WHERE external_id IS NOT NULL" in sql
    )
    assert (
        "CREATE UNIQUE INDEX uq_products_workspace_id_sku "
        "ON products (workspace_id, sku) WHERE sku IS NOT NULL" in sql
    )
    assert "uq_product_variants_workspace_id_external_id" in sql
    assert "uq_product_variants_workspace_id_sku" in sql
    assert (
        "uq_product_group_items_workspace_id_group_id_product_id" in sql
    )
    assert (
        "uq_product_group_items_workspace_id_group_id_variant_id" in sql
    )
    # All six partial indexes carry a WHERE ... IS NOT NULL clause.
    assert sql.count("IS NOT NULL") >= 6


def test_offline_upgrade_head_renders_composite_fks() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert "fk_product_variants_workspace_id_product_id_products" in sql
    assert "fk_product_group_items_workspace_id_group_id_product_groups" in sql
    assert "fk_product_group_items_workspace_id_product_id_products" in sql
    assert (
        "fk_product_group_items_workspace_id_variant_id_product_variants" in sql
    )


def test_offline_upgrade_head_renders_twelve_rls_statements() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    for table_name in CATALOG_TABLES:
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
    # The current head has since moved forward to the SPEC-05
    # competitors/matches migration (f4c8a391d5c9,
    # down_revision=c2987b29555e) — this test only asserts *this*
    # migration isn't itself a fork (single linear history up to and
    # including it), not that it's still the head.
    # tests/unit/test_migration_offline_competitors_matches.py owns the
    # current-head assertion.
