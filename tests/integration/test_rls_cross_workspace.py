"""Live cross-workspace RLS test (SPEC-03 T046, US3) — ⏸ DEFERRED.

FR-019/FR-021/FR-020a/SC-005.

Needs a reachable Postgres with the SPEC-03 migration already applied
(`alembic upgrade head`) AND the two-role setup from quickstart.md:
`crawmatic_app` (`DATABASE_URL`, NO BYPASSRLS) and `crawmatic_auth`
(`AUTH_DATABASE_URL`, BYPASSRLS). Not runnable in the no-Docker-daemon
build environment used to author this feature — SKIPS cleanly whenever
`DATABASE_URL`/`AUTH_DATABASE_URL`/`MIGRATION_DATABASE_URL` aren't usable
or a real connection attempt fails.

Proves, on `users` (one of the two RLS-protected identity tables):

(a) with `app.workspace_id` set to workspace A, a query against the app
    role's engine (`DATABASE_URL`) returns 0 of workspace B's rows —
    including a deliberately app-UNSCOPED query (no `WHERE workspace_id
    = ...` at all) — RLS alone enforces isolation.
(b) with NO `app.workspace_id` set at all, the same query returns 0
    rows for either workspace's seeded user (fail closed).
(c) the two-role path: the auth role (`AUTH_DATABASE_URL`, BYPASSRLS)
    finds a seeded user by email with no workspace context set (the
    pre-auth credential-lookup boundary, `get_auth_session`), while the
    app role (`DATABASE_URL`, NOT BYPASSRLS) sees nothing for that same
    lookup with no context set.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with both DB roles provisioned).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


def _auth_database_url() -> str | None:
    return os.environ.get("AUTH_DATABASE_URL")


def _migration_database_url() -> str | None:
    return os.environ.get("MIGRATION_DATABASE_URL")


def _all_reachable() -> bool:
    """Best-effort probe: True only if all three URLs are set and connectable.

    Any failure (unset, unreachable, auth error, ...) is treated as "not
    reachable" so the test skips cleanly instead of erroring in
    environments without live Postgres roles (e.g. no Docker daemon).
    """
    urls = (_database_url(), _auth_database_url(), _migration_database_url())
    if not all(urls):
        return False
    try:
        for url in urls:
            engine = create_engine(url)
            with engine.connect():
                pass
            engine.dispose()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _all_reachable(),
    reason=(
        "Needs reachable DATABASE_URL (app role, no BYPASSRLS) + "
        "AUTH_DATABASE_URL (crawmatic_auth BYPASSRLS role) + "
        "MIGRATION_DATABASE_URL (seeding), with the SPEC-03 migration "
        "already applied -- not available in this environment."
    ),
)


@pytest.fixture()
def two_workspaces() -> Iterator[dict[str, object]]:
    """Seed two workspaces, one ACTIVE user each, via the privileged migration engine."""
    from app_shared.enums import UserRole, UserStatus, WorkspaceStatus
    from app_shared.models import User, Workspace
    from app_shared.security.passwords import hash_password

    engine = create_engine(_migration_database_url())
    session_factory = sessionmaker(bind=engine)

    with session_factory() as session:
        ws_a = Workspace(
            name="RLS Test A", slug=f"rls-a-{uuid.uuid4().hex[:8]}", status=WorkspaceStatus.ACTIVE
        )
        ws_b = Workspace(
            name="RLS Test B", slug=f"rls-b-{uuid.uuid4().hex[:8]}", status=WorkspaceStatus.ACTIVE
        )
        session.add_all([ws_a, ws_b])
        session.flush()

        user_a = User(
            workspace_id=ws_a.id,
            email=f"user-a-{uuid.uuid4().hex}@example.com",
            password_hash=hash_password("irrelevant"),
            role=UserRole.WORKSPACE_ADMIN,
            status=UserStatus.ACTIVE,
        )
        user_b = User(
            workspace_id=ws_b.id,
            email=f"user-b-{uuid.uuid4().hex}@example.com",
            password_hash=hash_password("irrelevant"),
            role=UserRole.WORKSPACE_ADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add_all([user_a, user_b])
        session.commit()

        ids = {
            "workspace_a_id": ws_a.id,
            "workspace_b_id": ws_b.id,
            "user_a_id": user_a.id,
            "user_b_id": user_b.id,
            "user_a_email": user_a.email,
        }

    try:
        yield ids
    finally:
        with session_factory() as session:
            session.execute(text("DELETE FROM users WHERE id = :id"), {"id": ids["user_a_id"]})
            session.execute(text("DELETE FROM users WHERE id = :id"), {"id": ids["user_b_id"]})
            session.execute(
                text("DELETE FROM workspaces WHERE id = :id"), {"id": ids["workspace_a_id"]}
            )
            session.execute(
                text("DELETE FROM workspaces WHERE id = :id"), {"id": ids["workspace_b_id"]}
            )
            session.commit()
        engine.dispose()


@pytest.fixture()
def app_engine() -> Iterator[Engine]:
    engine = create_engine(_database_url())
    yield engine
    engine.dispose()


def test_scoped_context_returns_only_own_workspace_rows_even_when_query_is_app_unscoped(
    two_workspaces: dict[str, object], app_engine: Engine
) -> None:
    """RLS alone (no app-layer WHERE) confines the app role to workspace A's row."""
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": str(two_workspaces["workspace_a_id"])},
        )
        # Deliberately app-unscoped -- no WHERE workspace_id = ... at all;
        # RLS is the only thing standing between this query and workspace B's row.
        rows = conn.execute(text("SELECT id, workspace_id FROM users")).fetchall()

    ids = {row[0] for row in rows}
    assert two_workspaces["user_a_id"] in ids
    assert two_workspaces["user_b_id"] not in ids


def test_no_workspace_context_returns_zero_rows_fail_closed(
    two_workspaces: dict[str, object], app_engine: Engine
) -> None:
    """No `app.workspace_id` set at all -> zero rows for either workspace's user."""
    with app_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id FROM users WHERE id IN (:a, :b)"),
            {"a": str(two_workspaces["user_a_id"]), "b": str(two_workspaces["user_b_id"])},
        ).fetchall()

    assert rows == []


def test_two_role_path_auth_role_finds_credential_app_role_does_not(
    two_workspaces: dict[str, object], app_engine: Engine
) -> None:
    """BYPASSRLS auth role resolves the pre-auth lookup with no context set;
    the app role (no BYPASSRLS, no context) sees nothing for the same lookup.
    """
    from app_shared.database import get_auth_session

    with get_auth_session() as auth_session:
        found = auth_session.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": two_workspaces["user_a_email"]},
        ).fetchone()
    assert found is not None
    assert found[0] == two_workspaces["user_a_id"]

    with app_engine.begin() as conn:
        found_by_app = conn.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": two_workspaces["user_a_email"]},
        ).fetchone()
    assert found_by_app is None
