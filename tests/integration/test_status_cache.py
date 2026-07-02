"""Live status-cache test: suspend -> rejected within TTL; 0 per-request
status DB reads under sustained load (SPEC-03 T048, US4) — ⏸ DEFERRED.

FR-022/SC-007.

Needs a reachable Postgres (`DATABASE_URL` app role + `AUTH_DATABASE_URL`
BYPASSRLS role, SPEC-03 migration applied) AND a reachable Redis
(`REDIS_URL`, the status-cache keys live on the noeviction instance).
Not runnable in the no-Docker-daemon build environment used to author
this feature — SKIPS cleanly whenever any of those aren't usable or a
real connection attempt fails.

Two things proved:

1. **Suspend -> rejected within TTL.** A live login/`GET /v1/api-keys`
   round trip through `app.main.app` (FastAPI `TestClient`) succeeds
   while the user/workspace is ACTIVE; after suspending (and, for the
   immediate-propagation path, calling `invalidate_user`/
   `invalidate_workspace`) the same request is rejected. A second test
   relies on pure TTL expiry (no explicit invalidate) by waiting out
   `STATUS_CACHE_TTL_SECONDS` — the SC-007 guarantee holds even without
   an explicit cache-bust.
2. **0 per-request status DB reads in steady state.** Calling
   `get_user_status`/`get_workspace_status` repeatedly against the real
   Redis + an instrumented `session_factory` shows exactly ONE DB read
   (the initial cache miss) followed by zero further reads for every
   subsequent call within the TTL window.

Author now; leave unchecked (DEFERRED — needs a Postgres + Redis host).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

import pytest


def _live_services_reachable() -> bool:
    """Best-effort probe: True only if Postgres (app+auth roles) and Redis both work."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL or not settings.AUTH_DATABASE_URL or not settings.REDIS_URL:
        return False

    try:
        from sqlalchemy import text

        from app_shared.database import check_connection, get_auth_session
        from app_shared.redis_client import get_redis_client

        check_connection()
        with get_auth_session() as session:
            session.execute(text("SELECT 1"))
        get_redis_client().ping()
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_services_reachable(),
    reason="No reachable Postgres (app+auth roles) / Redis configured in this environment",
)


@pytest.fixture()
def seeded_workspace_and_admin() -> Iterator[dict[str, object]]:
    """A fresh ACTIVE workspace + WORKSPACE_ADMIN user with a known password."""
    from app_shared.database import get_session
    from app_shared.enums import UserRole, UserStatus, WorkspaceStatus
    from app_shared.models import User, Workspace
    from app_shared.security.passwords import hash_password

    email = f"status-cache-{uuid.uuid4().hex}@example.com"
    password = "correct horse battery staple 42"

    with get_session() as session:
        workspace = Workspace(
            name="Status Cache Test",
            slug=f"status-cache-{uuid.uuid4().hex[:8]}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()

        user = User(
            workspace_id=workspace.id,
            email=email,
            password_hash=hash_password(password),
            role=UserRole.WORKSPACE_ADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add(user)
        session.commit()
        ids = {"workspace_id": workspace.id, "user_id": user.id}

    yield {"email": email, "password": password, **ids}

    from sqlalchemy import text

    with get_session() as session:
        session.execute(text("DELETE FROM refresh_tokens WHERE user_id = :uid"), {"uid": ids["user_id"]})
        session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": ids["user_id"]})
        session.execute(text("DELETE FROM workspaces WHERE id = :wid"), {"wid": ids["workspace_id"]})
        session.commit()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _login_and_list_api_keys(client, email: str, password: str, workspace_id: object) -> int:
    login = client.post("/v1/auth/login", json={"email": email, "password": password})
    if login.status_code != 200:
        return login.status_code
    access_token = login.json()["access_token"]
    response = client.get(
        "/v1/api-keys",
        headers={
            "Authorization": f"Bearer {access_token}",
            "X-Workspace-Id": str(workspace_id),
        },
    )
    return response.status_code


def test_suspending_user_denies_immediately_after_invalidate(
    client, seeded_workspace_and_admin
) -> None:
    from app_shared.database import get_session
    from app_shared.redis_client import get_redis_client
    from app_shared.security.status_cache import invalidate_user
    from sqlalchemy import text

    email = seeded_workspace_and_admin["email"]
    password = seeded_workspace_and_admin["password"]
    user_id = seeded_workspace_and_admin["user_id"]
    workspace_id = seeded_workspace_and_admin["workspace_id"]

    assert _login_and_list_api_keys(client, email, password, workspace_id) == 200

    with get_session() as session:
        session.execute(text("UPDATE users SET status = 'suspended' WHERE id = :uid"), {"uid": user_id})
        session.commit()
    invalidate_user(get_redis_client(), user_id)

    # Suspension is now visible on the very next request -- no TTL wait needed.
    assert _login_and_list_api_keys(client, email, password, workspace_id) in (401, 403)


def test_suspending_workspace_denies_within_ttl_without_explicit_invalidate(
    client, seeded_workspace_and_admin
) -> None:
    from app_shared.config import get_settings
    from app_shared.database import get_session
    from sqlalchemy import text

    email = seeded_workspace_and_admin["email"]
    password = seeded_workspace_and_admin["password"]
    workspace_id = seeded_workspace_and_admin["workspace_id"]

    assert _login_and_list_api_keys(client, email, password, workspace_id) == 200

    with get_session() as session:
        session.execute(
            text("UPDATE workspaces SET status = 'suspended' WHERE id = :wid"), {"wid": workspace_id}
        )
        session.commit()

    # No explicit invalidate_workspace() call -- relies purely on the TTL
    # expiring (SC-007: "within the TTL", not necessarily instantaneous).
    ttl_seconds = get_settings().STATUS_CACHE_TTL_SECONDS
    time.sleep(ttl_seconds + 2)

    status_code = _login_and_list_api_keys(client, email, password, workspace_id)
    assert status_code in (401, 403)


def test_zero_per_request_status_db_reads_in_steady_state(seeded_workspace_and_admin) -> None:
    from app_shared.database import get_auth_session
    from app_shared.redis_client import get_redis_client
    from app_shared.security.status_cache import get_user_status

    redis_client = get_redis_client()
    user_id = seeded_workspace_and_admin["user_id"]

    read_count = 0
    real_get_auth_session = get_auth_session

    class _CountingSessionFactory:
        def __call__(self):
            nonlocal read_count
            read_count += 1
            return real_get_auth_session()

    counting_factory = _CountingSessionFactory()

    first = get_user_status(redis_client, counting_factory, user_id)
    assert first == "active"
    assert read_count == 1

    for _ in range(20):
        status = get_user_status(redis_client, counting_factory, user_id)
        assert status == "active"

    # Sustained load after the first (cold) read performs ZERO further
    # status DB reads -- every subsequent call is a cache hit (FR-022/SC-007).
    assert read_count == 1
