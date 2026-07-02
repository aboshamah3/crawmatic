"""Live-Postgres RLS fail-closed behavioral test (T036, FR-007, [analyze I2]) — ⏸ DEFERRED.

Applies :func:`app_shared.models.emit_rls_policy` to a throwaway table on
a real Postgres and exercises the fail-closed predicate end-to-end
(complementing the pure-string assertions in
``tests/unit/test_rls_policy.py``):

(a) **absent** ``app.workspace_id`` (no ``SET LOCAL``/``set_config`` at
    all in the transaction) — a SELECT against the table returns ZERO
    rows, never all rows and never an error.
(b) **empty** ``app.workspace_id`` (``set_config('app.workspace_id',
    '', true)``) — the ``NULLIF(..., '')`` wrapper ([analyze I2]) maps
    this to the same fail-closed NULL as (a): ZERO rows, and critically
    no ``invalid input syntax for type uuid: ""`` error.
(c) a **matching** ``app.workspace_id`` — only the row(s) owned by that
    workspace are returned, never the other workspace's rows.

This needs a reachable Postgres instance with ``MIGRATION_DATABASE_URL``
set (same convention as ``tests/integration/test_migration_job.py`` and
``tests/integration/test_db_connectivity.py``). It is **not** runnable
in the no-Docker-daemon build environment used to author this feature —
it SKIPS cleanly whenever:

* ``MIGRATION_DATABASE_URL`` is unset, or
* a real connection attempt against it fails (no reachable server, auth
  error, wrong credentials, ...).

Caveat: Postgres superusers and roles with ``BYPASSRLS`` always bypass
row security, even with ``FORCE ROW LEVEL SECURITY`` — this test is
only a faithful reproduction of production behavior when
``MIGRATION_DATABASE_URL`` connects as a non-superuser role (as it does
in the docker-compose setup: the ``crawmatic`` app role), which is also
the role that owns the throwaway table created here.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app_shared.models import emit_rls_policy

TABLE_NAME = "_rls_behavior_smoke"


def _migration_database_url() -> str | None:
    return os.environ.get("MIGRATION_DATABASE_URL")


def _postgres_reachable() -> bool:
    """Best-effort probe, mirroring test_migration_job.py's convention.

    Any failure (unset, unreachable, auth error, ...) is treated as "not
    reachable" so the test skips cleanly instead of erroring in
    environments without a live Postgres (e.g. no Docker daemon).
    """
    url = _migration_database_url()
    if not url:
        return False

    try:
        engine = create_engine(url)
        with engine.connect():
            pass
        engine.dispose()
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="No reachable Postgres / MIGRATION_DATABASE_URL configured in this environment",
)


@pytest.fixture()
def rls_table() -> Iterator[tuple[Engine, uuid.UUID, uuid.UUID]]:
    """Create a throwaway RLS-protected table seeded with two workspaces' rows."""
    engine = create_engine(_migration_database_url())
    workspace_id = uuid.uuid4()
    other_workspace_id = uuid.uuid4()

    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {TABLE_NAME}"))
        conn.execute(
            text(
                f"CREATE TABLE {TABLE_NAME} ("
                "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
                "workspace_id uuid NOT NULL)"
            )
        )
        for stmt in emit_rls_policy(TABLE_NAME):
            conn.execute(text(stmt))

    # Seed one row per workspace. FORCE ROW LEVEL SECURITY makes the same
    # USING predicate double as a WITH CHECK on INSERT, so each insert
    # must run with app.workspace_id set to that row's own workspace —
    # otherwise the insert itself would be rejected by the policy.
    for wid in (workspace_id, other_workspace_id):
        with engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.workspace_id', :w, true)"), {"w": str(wid)}
            )
            conn.execute(
                text(f"INSERT INTO {TABLE_NAME} (workspace_id) VALUES (:w)"), {"w": str(wid)}
            )

    try:
        yield engine, workspace_id, other_workspace_id
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {TABLE_NAME}"))
        engine.dispose()


def test_no_workspace_context_returns_zero_rows(
    rls_table: tuple[Engine, uuid.UUID, uuid.UUID],
) -> None:
    """No app.workspace_id set at all -> zero rows (fail-closed, absent case)."""
    engine, _workspace_id, _other_workspace_id = rls_table
    with engine.begin() as conn:
        result = conn.execute(text(f"SELECT id FROM {TABLE_NAME}"))
        assert result.fetchall() == []


def test_empty_workspace_context_returns_zero_rows(
    rls_table: tuple[Engine, uuid.UUID, uuid.UUID],
) -> None:
    """Empty app.workspace_id -> zero rows, no cast error (NULLIF fail-closed, [analyze I2])."""
    engine, _workspace_id, _other_workspace_id = rls_table
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '', true)"))
        result = conn.execute(text(f"SELECT id FROM {TABLE_NAME}"))
        assert result.fetchall() == []


def test_matching_workspace_context_returns_only_matching_rows(
    rls_table: tuple[Engine, uuid.UUID, uuid.UUID],
) -> None:
    """A real app.workspace_id -> only that workspace's row(s) return, never the other's."""
    engine, workspace_id, other_workspace_id = rls_table
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"), {"w": str(workspace_id)}
        )
        result = conn.execute(text(f"SELECT workspace_id FROM {TABLE_NAME}"))
        rows = result.fetchall()

    assert len(rows) == 1
    assert rows[0][0] == workspace_id
    assert rows[0][0] != other_workspace_id
