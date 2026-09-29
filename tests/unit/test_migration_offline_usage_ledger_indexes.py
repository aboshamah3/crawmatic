"""Offline DDL assertions for the E6 usage-export indexes (2026-09-29).

`alembic upgrade head --sql` (no database) must render each index as the
live-safe ON ONLY parent registration -- never a plain CREATE INDEX on a
partitioned parent, which would take ACCESS EXCLUSIVE on every partition of
a live table -- and must say, in the script, that the per-partition
CONCURRENTLY + ATTACH steps need a live catalog.
The live path (ON ONLY -> CONCURRENTLY per child -> ATTACH -> parent valid)
is exercised by the integration suite's `alembic upgrade head` against the
scratch Postgres (`tests/integration/test_admin_usage_equivalence.py`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def offline_sql() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "d8c14b7f6a92:e6a1c0d4f2b9", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize(
    ("index", "parent", "definition"),
    [
        (
            "ix_network_operations_parent_operation_id",
            "network_operations",
            "(parent_operation_id) WHERE parent_operation_id IS NOT NULL",
        ),
        ("ix_request_attempts_created_at", "request_attempts", "(created_at)"),
        ("ix_price_observations_scraped_at", "price_observations", "(scraped_at)"),
    ],
)
def test_each_index_is_registered_on_only_the_parent(
    offline_sql: str, index: str, parent: str, definition: str
) -> None:
    assert f"CREATE INDEX IF NOT EXISTS {index} ON ONLY {parent} {definition}" in offline_sql
    assert f"OFFLINE MODE: the per-partition CREATE INDEX CONCURRENTLY + ATTACH PARTITION steps for {index}" in offline_sql
    assert f"CREATE INDEX {index} ON {parent}" not in offline_sql
