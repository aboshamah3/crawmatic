"""Offline DDL assertions for the usage-export covering indexes (PLAN risk P2).

Mirrors `tests/unit/test_migration_offline_webhooks.py`: runs
`alembic upgrade head --sql` (offline, no DB connection) via subprocess
and asserts the rendered SQL contains the two covering indexes that
make the SaaS usage export scale on the partitioned `request_attempts`
and `price_observations` tables. Also asserts `alembic heads` reports
exactly one head — this migration must not fork the linear history.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _offline_sql() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_request_attempts_covering_index_is_created() -> None:
    sql = _offline_sql()
    assert "ix_request_attempts_workspace_id_created_at" in sql


def test_price_observations_covering_index_is_created() -> None:
    sql = _offline_sql()
    assert "ix_price_observations_workspace_id_scraped_at" in sql


def test_single_alembic_head() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, result.stdout
