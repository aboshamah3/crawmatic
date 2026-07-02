"""Live migration + bootstrap-seed test (SPEC-03 T050, FR-004/FR-023) — ⏸ DEFERRED.

Exercises the full offline-authored-only path on a real database:

1. `alembic upgrade head` (online, direct-to-Postgres via
   `MIGRATION_DATABASE_URL`) creates all four identity tables
   (`workspaces`, `users`, `refresh_tokens`, `api_keys`) with RLS
   (`ENABLE`/`FORCE ROW LEVEL SECURITY`) enabled on `users` + `api_keys`
   (contracts/migration-identity.md).
2. `scripts/seed_bootstrap.py` (`BootstrapConfig` + `run_seed`, exercised
   directly rather than via subprocess so assertions can inspect the
   returned `SeedResult`) creates exactly one `workspaces` row and one
   `SUPER_ADMIN` `users` row.
3. Re-running the seed against the same database is idempotent: the
   workspace and user counts are unchanged, and the same row ids are
   returned (no duplicate insert, no unique-constraint violation).

This needs a reachable Postgres instance with `MIGRATION_DATABASE_URL`
set (direct-to-Postgres, e.g. `postgres:5432` — never the PgBouncer
pooler). It is **not** runnable in the no-Docker-daemon build
environment used to author this feature — it SKIPS cleanly whenever:

* `MIGRATION_DATABASE_URL` is unset, or
* a real connection attempt against it fails (no reachable server, auth
  error, wrong credentials, ...).

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host).
See quickstart.md §B for the one-time role setup this test assumes
(`crawmatic_app` / `crawmatic_auth` roles are not required for this
specific test — it uses `MIGRATION_DATABASE_URL` directly — but the
migration + seed sequence documented there is exactly what this test
automates).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _migration_database_url() -> str | None:
    return os.environ.get("MIGRATION_DATABASE_URL")


def _postgres_reachable() -> bool:
    """Best-effort probe: True only if MIGRATION_DATABASE_URL is set and connectable.

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


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ},
    )


@pytest.fixture()
def migrated_engine():
    """Bring the database to `head`, yield an engine, then leave it as-is.

    Migrations are forward-only in this fixture (no downgrade teardown —
    `test_migration_job.py` already covers the up/down round trip); this
    test only needs the four tables + RLS to exist.
    """
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}"

    engine = create_engine(_migration_database_url())
    try:
        yield engine
    finally:
        engine.dispose()


def test_migration_creates_four_tables_with_rls_on_users_and_api_keys(migrated_engine) -> None:
    inspector = inspect(migrated_engine)
    table_names = set(inspector.get_table_names())
    for expected in ("workspaces", "users", "refresh_tokens", "api_keys"):
        assert expected in table_names

    with migrated_engine.connect() as conn:
        for table in ("users", "api_keys"):
            row_security = conn.execute(
                text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"),
                {"t": table},
            ).fetchone()
            assert row_security is not None, f"{table} not found in pg_class"
            assert row_security[0] is True, f"{table} does not have RLS enabled"
            assert row_security[1] is True, f"{table} does not have RLS forced"

        for table in ("workspaces", "refresh_tokens"):
            row_security = conn.execute(
                text("SELECT relrowsecurity FROM pg_class WHERE relname = :t"),
                {"t": table},
            ).fetchone()
            assert row_security is not None, f"{table} not found in pg_class"
            assert row_security[0] is False, f"{table} unexpectedly has RLS enabled"


@pytest.fixture()
def bootstrap_env() -> dict[str, str]:
    unique = uuid.uuid4().hex[:8]
    return {
        "BOOTSTRAP_ADMIN_EMAIL": f"bootstrap-{unique}@example.com",
        "BOOTSTRAP_ADMIN_PASSWORD": "correct horse battery staple",
        "BOOTSTRAP_WORKSPACE_NAME": f"Bootstrap Test {unique}",
        "BOOTSTRAP_WORKSPACE_SLUG": f"bootstrap-test-{unique}",
    }


def test_seed_bootstrap_creates_exactly_one_workspace_and_super_admin_and_is_idempotent(
    migrated_engine, bootstrap_env: dict[str, str]
) -> None:
    # Import lazily -- only meaningful once the identity tables exist.
    import scripts.seed_bootstrap as seed_bootstrap

    session_factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)

    config = seed_bootstrap.load_config(bootstrap_env)

    with session_factory() as session:
        first = seed_bootstrap.run_seed(session, config)
        session.commit()

    assert first.workspace_created is True
    assert first.admin_created is True

    with session_factory() as session:
        from app_shared.models.identity import User, Workspace

        workspace_total = session.execute(select(func.count()).select_from(Workspace)).scalar_one()
        admin_total = session.execute(
            select(func.count()).select_from(User).where(User.email == config.admin_email)
        ).scalar_one()

    assert workspace_total == 1
    assert admin_total == 1

    # Re-run: idempotent -- same ids, no new rows, no unique-constraint error.
    with session_factory() as session:
        second = seed_bootstrap.run_seed(session, config)
        session.commit()

    assert second.workspace_created is False
    assert second.admin_created is False
    assert second.workspace_id == first.workspace_id
    assert second.admin_user_id == first.admin_user_id

    with session_factory() as session:
        from app_shared.models.identity import User, Workspace

        workspace_total_after = session.execute(
            select(func.count()).select_from(Workspace)
        ).scalar_one()
        admin_total_after = session.execute(
            select(func.count()).select_from(User).where(User.email == config.admin_email)
        ).scalar_one()

    assert workspace_total_after == workspace_total
    assert admin_total_after == admin_total
