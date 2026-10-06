"""Offline DDL for the competitor-prices keyset index (risk review 2026-10-06, P6).

`GET /v1/variants/competitor-prices` walks `competitor_product_matches`
by `workspace_id = :ws ORDER BY (created_at, id)` (see
`apps/api/app/routers/variants.py::list_all_competitor_prices`). The
migration must build `(workspace_id, created_at, id)` on exactly that
table (the model's `__tablename__`, not a guess), CONCURRENTLY, and keep
the history linear.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app_shared.models.competitors_matches import CompetitorProductMatch

REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS = "d7a1f3c5e902"
REVISION = "e2b8d4f6a1c3"


def _alembic(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_upgrade_builds_the_keyset_index_concurrently_on_the_matches_table() -> None:
    sql = _alembic("upgrade", f"{PREVIOUS}:{REVISION}", "--sql")
    table = CompetitorProductMatch.__tablename__
    assert (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_cpm_ws_created_id "
        f"ON {table} (workspace_id, created_at, id)"
    ) in sql


def test_downgrade_drops_it() -> None:
    sql = _alembic("downgrade", f"{REVISION}:{PREVIOUS}", "--sql")
    assert "DROP INDEX CONCURRENTLY IF EXISTS ix_cpm_ws_created_id" in sql


def test_the_new_revision_is_the_single_head() -> None:
    heads = [line for line in _alembic("heads").splitlines() if line.strip()]
    assert heads == [f"{REVISION} (head)"], heads
