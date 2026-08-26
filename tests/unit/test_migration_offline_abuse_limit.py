"""Offline migration render test for the `api_abuse_limit_counters`
migration (EPA W5.5-L1 item 2, the engine half of `apps/api/app/abuse_limit.py`).

Mirrors `tests/unit/test_migration_offline_rollups.py`: runs
`alembic upgrade head --sql` (offline, no DB connection) via subprocess
and asserts the rendered SQL contains the `api_abuse_limit_counters`
`CREATE TABLE` statement, its load-bearing composite primary key, the
`window_starts_at` index the retention sweep depends on, and the
`crawmatic_app` grant -- plus that `alembic heads` yields a single head
and this revision's `down_revision` is the head it was authored against.
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


def test_offline_upgrade_head_renders_api_abuse_limit_counters_table() -> None:
    sql = _upgrade_sql()
    assert "CREATE TABLE api_abuse_limit_counters" in sql


def test_offline_upgrade_head_renders_the_load_bearing_composite_pk() -> None:
    """The (bucket_key, window_starts_at) PK is what the limiter's
    `INSERT ... ON CONFLICT DO UPDATE` serialises on -- see the migration
    docstring for why dropping it reintroduces the read-then-write race."""
    sql = _upgrade_sql()
    create_stmt_start = sql.index("CREATE TABLE api_abuse_limit_counters")
    create_stmt_end = sql.index(");", create_stmt_start)
    create_stmt = sql[create_stmt_start:create_stmt_end]
    assert "bucket_key TEXT NOT NULL" in create_stmt
    assert "window_starts_at TIMESTAMP WITH TIME ZONE NOT NULL" in create_stmt
    assert "count INTEGER DEFAULT '1' NOT NULL" in create_stmt
    assert (
        "CONSTRAINT pk_api_abuse_limit_counters "
        "PRIMARY KEY (bucket_key, window_starts_at)" in create_stmt
    )


def test_offline_upgrade_head_renders_the_window_index() -> None:
    sql = _upgrade_sql()
    assert (
        "CREATE INDEX ix_api_abuse_limit_counters_window "
        "ON api_abuse_limit_counters (window_starts_at)" in sql
    )


def test_offline_upgrade_head_grants_the_app_role() -> None:
    """Without this grant the limiter's very first write raises, and
    because it fails CLOSED that is a 429 on every limited surface."""
    sql = _upgrade_sql()
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON api_abuse_limit_counters" in sql
    assert "TO crawmatic_app" in sql


def test_the_grant_is_role_conditional_so_a_role_less_database_can_migrate() -> None:
    """EPA Phase C F11: a bare ``GRANT ... TO crawmatic_app`` ABORTS the
    whole upgrade on a database that has not been role-provisioned yet —
    a fresh CI database, a scratch container, a first bootstrap — leaving
    every later revision unapplied. The grant must therefore be guarded
    by a ``pg_roles`` existence check: identical behaviour where the role
    exists, a NOTICE instead of a failed migration where it does not.
    """
    sql = _upgrade_sql()
    grant_pos = sql.index("GRANT SELECT, INSERT, UPDATE, DELETE ON api_abuse_limit_counters")
    guard_pos = sql.rindex(
        "SELECT FROM pg_roles WHERE rolname = 'crawmatic_app'", 0, grant_pos
    )
    do_pos = sql.rindex("DO $$", 0, guard_pos)
    # The guard is inside the same DO block as, and strictly before, the GRANT.
    assert do_pos < guard_pos < grant_pos
    assert "RAISE NOTICE" in sql[guard_pos:]


def test_offline_upgrade_head_renders_no_rls() -> None:
    """Deliberately no RLS -- see `scripts/rls_table_manifest.txt`'s entry:
    the bucket key never carries a workspace, and the limiter must be able
    to count an attempt from a caller whose workspace is not yet resolved."""
    sql = _upgrade_sql()
    assert "api_abuse_limit_counters ENABLE ROW LEVEL SECURITY" not in sql
    assert "api_abuse_limit_counters_workspace_isolation" not in sql


def test_downgrade_drops_index_then_table() -> None:
    result = _run_alembic("downgrade", "e92029e9902c:42bb8b878fe9", "--sql")
    assert result.returncode == 0, (
        f"alembic downgrade --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    sql = result.stdout
    assert "DROP INDEX ix_api_abuse_limit_counters_window" in sql
    assert "DROP TABLE api_abuse_limit_counters" in sql
    idx_pos = sql.index("DROP INDEX ix_api_abuse_limit_counters_window")
    table_pos = sql.index("DROP TABLE api_abuse_limit_counters")
    assert idx_pos < table_pos


def test_alembic_heads_reports_exactly_one_head() -> None:
    result = _run_alembic("heads")

    assert result.returncode == 0, (
        f"alembic heads failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    head_lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert len(head_lines) == 1, f"expected exactly one head, got: {head_lines!r}"
    assert "(head)" in head_lines[0]
    assert "e92029e9902c" in head_lines[0]


def test_down_revision_is_the_head_this_revision_was_authored_against() -> None:
    result = _run_alembic("history")
    assert result.returncode == 0, (
        f"alembic history failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    matching_lines = [
        line for line in result.stdout.splitlines() if line.startswith("42bb8b878fe9 -> ")
    ]
    assert matching_lines, result.stdout
