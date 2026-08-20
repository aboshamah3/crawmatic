"""Offline migration render test for `request_attempts.origin` (Task 2.3,
proxy-cost-reduction plan §2.3, safety prerequisite for §3.3).

Mirrors `tests/unit/test_strategy_single_head.py`: runs `alembic upgrade
head --sql` (offline, no DB connection) via subprocess and asserts the
rendered SQL adds the `origin` column (NOT NULL, `server_default`
`'scrape'`) to `request_attempts`, that `alembic heads` yields a single
head, and that the new revision chains off `b3e7c9a15d42` (the head this
plan's task 2.3 was scoped against).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_offline_upgrade_head_adds_origin_column_not_null_default_scrape() -> None:
    sql = _upgrade_sql()
    assert "ALTER TABLE request_attempts ADD COLUMN origin VARCHAR(32) DEFAULT 'scrape' NOT NULL" in sql


def test_offline_upgrade_head_does_not_touch_any_other_table() -> None:
    sql = _upgrade_sql()
    # Purely additive: exactly one `CREATE TABLE request_attempts` in the
    # whole rendered history (the original SPEC-07 migration) -- this
    # revision only ever ADDs a column, no data-migration UPDATE pass, and
    # no new partition-child table.
    assert sql.count("CREATE TABLE request_attempts (") == 1
    assert "UPDATE request_attempts" not in sql


def test_downgrade_drops_the_origin_column() -> None:
    result = _run_alembic("downgrade", "0fc4c9c9c8b3:b3e7c9a15d42", "--sql")
    assert result.returncode == 0, (
        f"alembic downgrade --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "ALTER TABLE request_attempts DROP COLUMN origin" in result.stdout


def test_alembic_heads_reports_exactly_one_head() -> None:
    result = _run_alembic("heads")
    assert result.returncode == 0, (
        f"alembic heads failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    head_lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert len(head_lines) == 1, f"expected exactly one head, got: {head_lines!r}"
    assert "(head)" in head_lines[0]


def test_down_revision_is_the_task_2_3_scoped_head() -> None:
    result = _run_alembic("history")
    assert result.returncode == 0, (
        f"alembic history failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    matching_lines = [
        line for line in result.stdout.splitlines() if line.startswith("b3e7c9a15d42 -> ")
    ]
    assert matching_lines, result.stdout
