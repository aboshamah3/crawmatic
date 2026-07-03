"""Offline migration render test for the scrape_profiles migration (SPEC-06 T014, FR-001/FR-021/FR-023).

Mirrors `tests/unit/test_migration_offline_competitors_matches.py`
(SPEC-05): runs `alembic upgrade head --sql` (offline, no DB connection)
via subprocess and asserts the rendered SQL contains the `scrape_profiles`
`CREATE TABLE`, both partial unique indexes with their exact predicates,
the four custom global-readable RLS statements, and the three
`ON DELETE SET NULL` FK alterations. Also asserts `alembic heads` reports
exactly one head — this migration (`a4f205e8d7de`) must not fork the
linear history, and its `down_revision` is the SPEC-05 head
(`f4c8a391d5c9`).
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


def test_offline_upgrade_head_renders_scrape_profiles_table() -> None:
    result = _run_alembic("upgrade", "head", "--sql")

    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "CREATE TABLE scrape_profiles" in result.stdout


def test_offline_upgrade_head_renders_both_partial_unique_indexes() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert (
        "CREATE UNIQUE INDEX uq_scrape_profiles_workspace_id_name "
        "ON scrape_profiles (workspace_id, name) WHERE workspace_id IS NOT NULL" in sql
    )
    assert (
        "CREATE UNIQUE INDEX uq_scrape_profiles_name_global "
        "ON scrape_profiles (name) WHERE workspace_id IS NULL" in sql
    )


def test_offline_upgrade_head_renders_workspace_id_index_and_fk() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert "CREATE INDEX ix_scrape_profiles_workspace_id ON scrape_profiles" in sql
    assert "fk_scrape_profiles_workspace_id_workspaces" in sql


def test_offline_upgrade_head_renders_custom_global_readable_rls() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert "ALTER TABLE scrape_profiles ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE scrape_profiles FORCE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY scrape_profiles_workspace_read ON scrape_profiles FOR SELECT" in sql
    assert f"workspace_id IS NULL OR workspace_id = {FAIL_CLOSED_CTX}" in sql
    assert "CREATE POLICY scrape_profiles_workspace_write ON scrape_profiles FOR ALL" in sql
    assert f"USING (workspace_id = {FAIL_CLOSED_CTX})" in sql
    assert f"WITH CHECK (workspace_id = {FAIL_CLOSED_CTX})" in sql


def test_offline_upgrade_head_renders_three_on_delete_set_null_fks() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert (
        "ALTER TABLE competitors ADD CONSTRAINT "
        "fk_competitors_default_scrape_profile_id_scrape_profiles "
        "FOREIGN KEY(default_scrape_profile_id) REFERENCES scrape_profiles (id) "
        "ON DELETE SET NULL" in sql
    )
    assert (
        "ALTER TABLE competitor_product_matches ADD CONSTRAINT "
        "fk_cpm_scrape_profile_id_scrape_profiles "
        "FOREIGN KEY(scrape_profile_id) REFERENCES scrape_profiles (id) "
        "ON DELETE SET NULL" in sql
    )
    assert (
        "ALTER TABLE workspaces ADD CONSTRAINT "
        "fk_workspaces_default_scrape_profile_id_scrape_profiles "
        "FOREIGN KEY(default_scrape_profile_id) REFERENCES scrape_profiles (id) "
        "ON DELETE SET NULL" in sql
    )


def test_alembic_heads_reports_exactly_one_head() -> None:
    result = _run_alembic("heads")

    assert result.returncode == 0, (
        f"alembic heads failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    head_lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert len(head_lines) == 1, f"expected exactly one head, got: {head_lines!r}"
    assert "(head)" in head_lines[0]
    # The current head has since moved forward to the SPEC-07
    # observations migration (2db33dea5e14, down_revision=a4f205e8d7de)
    # — this test only asserts *this* migration isn't itself a fork
    # (single linear history up to and including it), not that it's
    # still the head. tests/unit/test_migration_offline_observations.py
    # owns the current-head assertion.


def test_down_revision_is_the_spec05_head() -> None:
    result = _run_alembic("history")
    assert result.returncode == 0, (
        f"alembic history failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "f4c8a391d5c9 -> a4f205e8d7de" in result.stdout
