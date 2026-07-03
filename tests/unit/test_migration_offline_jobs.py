"""Offline migration render test for the jobs migration (SPEC-08 T015, FR-005).

Mirrors `tests/unit/test_migration_offline_competitors_matches.py`
(SPEC-05): runs `alembic upgrade head --sql` (offline, no DB connection)
via subprocess and asserts the rendered SQL contains both `CREATE
TABLE`s, `unique(workspace_id, id)` on jobs, `unique(scrape_job_id,
match_id)` on targets, the composite target->job FK, and the six RLS
statements (3 per table x 2). Also asserts `alembic heads` reports
exactly one head — this migration (`a6b0234cd4ad`) is the current head
and must not fork the linear history, and its `down_revision` is the
SPEC-07 head (`2db33dea5e14`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

FAIL_CLOSED_CTX = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"

JOBS_TABLES = ["scrape_jobs", "scrape_job_targets"]


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
    assert "CREATE TABLE scrape_jobs" in sql
    assert "CREATE TABLE scrape_job_targets" in sql


def test_offline_upgrade_head_renders_scrape_jobs_unique_workspace_id_id() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert (
        "CONSTRAINT uq_scrape_jobs_workspace_id_id UNIQUE (workspace_id, id)" in sql
    )


def test_offline_upgrade_head_renders_scrape_job_targets_unique() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert (
        "CONSTRAINT uq_scrape_job_targets_scrape_job_id_match_id "
        "UNIQUE (scrape_job_id, match_id)" in sql
    )


def test_offline_upgrade_head_renders_composite_fk_target_to_job() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert "fk_scrape_job_targets_workspace_scrape_job_scrape_jobs" in sql


def test_offline_upgrade_head_renders_rls_anchor_fks() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    assert "fk_scrape_jobs_workspace_id_workspaces" in sql
    assert "fk_scrape_job_targets_workspace_id_workspaces" in sql


def test_offline_upgrade_head_renders_six_rls_statements() -> None:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0
    sql = result.stdout

    for table_name in JOBS_TABLES:
        assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY {table_name}_workspace_isolation ON {table_name}" in sql

    assert FAIL_CLOSED_CTX in sql


def test_alembic_heads_reports_exactly_one_head() -> None:
    result = _run_alembic("heads")

    assert result.returncode == 0, (
        f"alembic heads failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    head_lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert len(head_lines) == 1, f"expected exactly one head, got: {head_lines!r}"
    assert "(head)" in head_lines[0]
    # The current head has since moved forward to the SPEC-09 alerts
    # migration (e4a75b48360c, down_revision=a6b0234cd4ad) — this test
    # only asserts *this* migration isn't itself a fork (single linear
    # history up to and including it), not that it's still the head.
    # tests/unit/test_migration_offline_alerts.py owns the current-head
    # assertion.


def test_down_revision_is_the_spec07_head() -> None:
    result = _run_alembic("history")
    assert result.returncode == 0, (
        f"alembic history failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "2db33dea5e14 -> a6b0234cd4ad" in result.stdout
