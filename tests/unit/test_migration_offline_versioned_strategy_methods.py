"""Offline DDL/backfill contract for Phase-1 versioned strategy methods."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def offline_sql() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_creates_version_history_and_workspace_isolated_method_table(offline_sql: str) -> None:
    assert "CREATE TABLE scrape_profile_revisions" in offline_sql
    assert "CREATE TABLE domain_strategy_methods" in offline_sql
    assert "domain_strategy_methods_workspace_isolation" in offline_sql
    assert "scrape_profile_revisions_workspace_read" in offline_sql


def test_backfills_preferred_candidates_as_proven_without_deleting_stats(
    offline_sql: str,
) -> None:
    assert "INSERT INTO domain_strategy_methods" in offline_sql
    assert "'PROVEN'" in offline_sql
    assert "ALTER TABLE strategy_attempt_stats ADD COLUMN strategy_method_id" in offline_sql
    assert "DELETE FROM strategy_attempt_stats" not in offline_sql
    assert "DROP COLUMN preferred_access_method" not in offline_sql
    assert "DROP COLUMN preferred_extraction_method" not in offline_sql


def test_adds_attempt_and_cross_mode_audit_contract(offline_sql: str) -> None:
    for column in (
        "strategy_method_id",
        "scrape_profile_id",
        "scrape_profile_version",
        "adapter_key",
        "final_url",
        "identity_validation_result",
        "terminal_for_target",
    ):
        assert f"ALTER TABLE request_attempts ADD COLUMN {column}" in offline_sql
    assert "ALTER TABLE scrape_job_targets ADD COLUMN chain_token UUID" in offline_sql
    assert (
        "ALTER TABLE scrape_job_targets ADD COLUMN strategy_url_override TEXT"
        in offline_sql
    )
    assert "ALTER TABLE domain_playbooks ADD COLUMN method_templates JSONB" in offline_sql
