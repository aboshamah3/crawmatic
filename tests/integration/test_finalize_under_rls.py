"""finalize_jobs must finalize a job when the worker runs under the
PRODUCTION role split: ordinary engine = RLS-confined role (NOT
BYPASSRLS), system engine = BYPASSRLS role. This is the exact
configuration that silently killed finalization on 2026-08-21 — a
BYPASSRLS test role would have hidden it ("a type cannot tell a real
guard from a claim"). Skips loudly without RLS_TEST_DATABASE_URL.

## Why this file exists (mushtryati F-1, 2026-08-21)

`app.workers.tasks_jobs.finalize_jobs` sweeps EVERY workspace's
non-terminal jobs. Under `FORCE ROW LEVEL SECURITY` that sweep, run on
the ordinary pooler role with no `app.workspace_id` GUC set, returns
ZERO rows — it does not error, it simply finds nothing, so every loop
body downstream never runs and jobs sit `RUNNING` at 0/0 forever. It
was invisible for 6.5 h in production and invisible to the test suite
too, because the test databases had always been reached through a
superuser/owner URL, which bypasses RLS unconditionally.

So this test does not assert against a convenient role. It reuses the
harness in `test_rls_cross_workspace.py` verbatim — one owner URL
(`RLS_TEST_DATABASE_URL`, falling back to `MIGRATION_DATABASE_URL`),
`alembic upgrade head`, then the real deploy step
`migrate.provision_roles`, which asserts `crawmatic_app` is
NOSUPERUSER/NOBYPASSRLS before any claim below is made — and drives the
SHIPPED task with its ordinary engine bound to that confined role.

## Running it

    docker run -d --name cm-rls-test \\
      -e POSTGRES_USER=crawmatic_owner -e POSTGRES_PASSWORD=ownerpw \\
      -e POSTGRES_DB=crawmatic -p 127.0.0.1:55444:5432 postgres:17.5-bookworm

    RLS_TEST_DATABASE_URL=postgresql+psycopg://crawmatic_owner:ownerpw@127.0.0.1:55444/crawmatic \\
      uv run pytest tests/integration/test_finalize_under_rls.py -v

## Why the task runs in a subprocess

`apps/api/app` and `apps/workers/app` are both named `app`, and the API
one wins on the installed path — so `app.workers.tasks_jobs` is only
importable with `apps/workers` prepended to `sys.path` in a process
that has not already resolved `app`. Every other worker-task check in
this repo therefore drives the task from a subprocess script
(`tests/unit/test_jobs_counters.py`), and this one does the same. The
engine wiring the spec calls for is done inside that script — assigning
`app_shared.database._engine` / `._system_engine` exactly as production
wires them — and every assertion is made in the parent, against the
owner connection, on the rows the shipped task did or did not touch.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# The harness is reused, not re-implemented: `provisioned` migrates the
# throwaway database, runs the real `provision_roles` deploy step, and
# skips loudly (with instructions) when no owner URL is reachable.
from integration.test_rls_cross_workspace import provisioned  # noqa: F401 - pytest fixture

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Settings are validated at import time of `app.workers.tasks_jobs`, so
#: the subprocess needs a complete-looking environment. Only the two
#: database URLs matter to what is being proven — nothing in
#: `finalize_jobs` opens Redis or Scrapyd.
_BASE_WORKER_ENV = {
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",  # noqa: S106 - throwaway test environment
    "JWT_SECRET": "test-jwt-secret",  # noqa: S106 - throwaway test environment
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}

#: Wires the worker's two engines the way prod wires them, then runs the
#: SHIPPED task. `CM_T3_ORDINARY_URL` is the engine every row read/write
#: goes through; `CM_T3_SCAN_URL` is the engine the cross-tenant id scan
#: goes through. Pointing the second at the confined role IS the bug.
_FINALIZE_SCRIPT = """
import os
import sys

sys.path.insert(0, "apps/workers")

from sqlalchemy import create_engine

import app_shared.database as db


def _make(url):
    return create_engine(url, pool_pre_ping=True, connect_args={"prepare_threshold": None})


db._engine = _make(os.environ["CM_T3_ORDINARY_URL"])
db._sessionmaker = None
db._system_engine = _make(os.environ["CM_T3_SCAN_URL"])
db._system_sessionmaker = None

from app.workers.tasks_jobs import finalize_jobs

finalize_jobs()

print("OK")
"""


@pytest.fixture()
def rls_engines(provisioned: dict[str, str]) -> Iterator[tuple[Engine, Engine, Engine]]:
    """`(owner_engine, app_engine, bypass_engine)` over the provisioned database.

    `app_engine` is `crawmatic_app` — NOSUPERUSER, NOBYPASSRLS, asserted
    so by `provision_roles.verify()` inside `provisioned`. `bypass_engine`
    is `crawmatic_auth`, the BYPASSRLS role the system session uses.
    """
    owner = create_engine(provisioned["admin_url"])
    app_engine = create_engine(provisioned["app_url"])
    bypass_engine = create_engine(provisioned["auth_url"])
    try:
        yield owner, app_engine, bypass_engine
    finally:
        owner.dispose()
        app_engine.dispose()
        bypass_engine.dispose()


def _seed_all_terminal_job(owner: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    """One workspace, one RUNNING job, two terminal targets, counters still 0.

    Written as plain INSERTs through the owner (superuser) connection so
    the seed itself is not a claim about what the confined role can do.
    """
    workspace_id = uuid.uuid4()
    job_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    with owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
                "VALUES (:id, :name, :slug, 'active', :now, :now)"
            ),
            {
                "id": str(workspace_id),
                "name": "F-1 finalize under RLS",
                "slug": f"f1-finalize-{workspace_id.hex[:8]}",
                "now": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO scrape_jobs "
                "(id, workspace_id, type, scope, status, priority, total_targets, "
                " success_count, failure_count, skipped_count, source, started_at, created_at) "
                "VALUES (:id, :ws, 'MANUAL', 'MATCH', 'RUNNING', 'NORMAL', 2, "
                " 0, 0, 0, 'API', :now, :now)"
            ),
            {"id": str(job_id), "ws": str(workspace_id), "now": now},
        )
        for status in ("COMPLETED", "FAILED"):
            conn.execute(
                text(
                    "INSERT INTO scrape_job_targets "
                    "(id, workspace_id, scrape_job_id, match_id, status, "
                    " started_at, completed_at, created_at) "
                    "VALUES (:id, :ws, :job, :match, :status, :now, :now, :now)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "ws": str(workspace_id),
                    "job": str(job_id),
                    "match": str(uuid.uuid4()),
                    "status": status,
                    "now": now,
                },
            )

    return workspace_id, job_id


def _purge(owner: Engine, workspace_id: uuid.UUID) -> None:
    with owner.begin() as conn:
        for table in (
            "outbox_messages",
            "scrape_job_targets",
            "scrape_jobs",
            "workspaces",
        ):
            column = "id" if table == "workspaces" else "workspace_id"
            conn.execute(
                text(f"DELETE FROM {table} WHERE {column} = :ws"),  # noqa: S608 - fixed names
                {"ws": str(workspace_id)},
            )


def _run_finalize(*, ordinary_url: str, scan_url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _FINALIZE_SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            **_BASE_WORKER_ENV,
            "DATABASE_URL": ordinary_url,
            "SYSTEM_DATABASE_URL": scan_url,
            "AUTH_DATABASE_URL": scan_url,
            "CM_T3_ORDINARY_URL": ordinary_url,
            "CM_T3_SCAN_URL": scan_url,
        },
    )


def _job_row(owner: Engine, job_id: uuid.UUID):
    with owner.connect() as conn:
        return conn.execute(
            text(
                "SELECT status, success_count, failure_count, completed_at "
                "FROM scrape_jobs WHERE id = :id"
            ),
            {"id": str(job_id)},
        ).one()


def test_finalize_jobs_finalizes_under_confined_role(
    rls_engines: tuple[Engine, Engine, Engine], provisioned: dict[str, str]
) -> None:
    """The production wiring: confined ordinary engine + BYPASSRLS scan engine."""
    owner, _app_engine, _bypass_engine = rls_engines

    workspace_id, job_id = _seed_all_terminal_job(owner)
    try:
        result = _run_finalize(
            ordinary_url=provisioned["app_url"], scan_url=provisioned["auth_url"]
        )
        assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        assert result.stdout.strip().endswith("OK")

        row = _job_row(owner, job_id)
        assert row.status == "PARTIAL_FAILED"
        assert (row.success_count, row.failure_count) == (1, 1)
        assert row.completed_at is not None
    finally:
        _purge(owner, workspace_id)


def test_finalize_jobs_is_blind_when_scan_role_is_confined(
    rls_engines: tuple[Engine, Engine, Engine], provisioned: dict[str, str]
) -> None:
    """The 2026-08-21 configuration, pinned as the failure it is.

    Both engines on the RLS-confined role: the cross-tenant id scan runs
    with no `app.workspace_id` set, `FORCE ROW LEVEL SECURITY` fails it
    closed to zero rows, and the task completes successfully having
    finalized nothing. No exception, no log line, no finished job — which
    is exactly why this needs a test rather than an alert.
    """
    owner, _app_engine, _bypass_engine = rls_engines

    workspace_id, job_id = _seed_all_terminal_job(owner)
    try:
        result = _run_finalize(
            ordinary_url=provisioned["app_url"], scan_url=provisioned["app_url"]
        )
        assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        assert result.stdout.strip().endswith("OK")

        row = _job_row(owner, job_id)
        assert row.status == "RUNNING"
        assert (row.success_count, row.failure_count) == (0, 0)
        assert row.completed_at is None
    finally:
        _purge(owner, workspace_id)
