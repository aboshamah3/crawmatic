"""Live auth flow: login -> refresh (rotate) -> reuse rejected -> concurrent
rotation (exactly one wins) -> logout revokes (SPEC-03 T026) — ⏸ DEFERRED.

FR-006/FR-009/FR-010/FR-011/SC-001/SC-002/SC-003.

Needs a reachable Postgres (with the SPEC-03 migration applied, RLS roles
provisioned per quickstart.md) AND a reachable Redis (login rate-limit
gate runs first on every ``/v1/auth/login`` call). Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever ``Settings``/``DATABASE_URL``/``AUTH_DATABASE_URL``/
``REDIS_URL`` aren't usable, or a real connection attempt fails.

Exercises the endpoints through FastAPI's ``TestClient`` against
``app.main.app`` (no running server/container required) — only the
database + Redis need to be live.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

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
        from app_shared.database import check_connection, get_auth_session
        from app_shared.redis_client import get_redis_client
        from sqlalchemy import text

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
def seeded_user():
    """Create a fresh ACTIVE user (no workspace) with a known password, then clean up."""
    from app_shared.database import get_session
    from app_shared.enums import UserRole, UserStatus
    from app_shared.models import User
    from app_shared.security.passwords import hash_password

    email = f"auth-flow-{uuid.uuid4().hex}@example.com"
    password = "correct horse battery staple 42"

    with get_session() as session:
        user = User(
            workspace_id=None,
            email=email,
            password_hash=hash_password(password),
            role=UserRole.SUPER_ADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add(user)
        session.commit()
        user_id = user.id

    yield {"email": email, "password": password, "user_id": user_id}

    with get_session() as session:
        from sqlalchemy import text

        session.execute(text("DELETE FROM refresh_tokens WHERE user_id = :uid"), {"uid": user_id})
        session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})
        session.commit()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_login_returns_access_and_refresh_pair(client, seeded_user) -> None:
    response = client.post(
        "/v1/auth/login", json={"email": seeded_user["email"], "password": seeded_user["password"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"


def test_refresh_rotates_and_rejects_reuse(client, seeded_user) -> None:
    login = client.post(
        "/v1/auth/login", json={"email": seeded_user["email"], "password": seeded_user["password"]}
    )
    refresh_token = login.json()["refresh_token"]

    first = client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert first.status_code == 200
    new_refresh_token = first.json()["refresh_token"]
    assert new_refresh_token != refresh_token

    # SC-002: the token works exactly once -- reuse is rejected.
    reuse = client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert reuse.status_code == 401
    assert reuse.json()["detail"]["code"] == "AUTH_FAILED"

    # The rotated pair still works.
    second = client.post("/v1/auth/refresh", json={"refresh_token": new_refresh_token})
    assert second.status_code == 200


def test_concurrent_refresh_exactly_one_wins(client, seeded_user) -> None:
    login = client.post(
        "/v1/auth/login", json={"email": seeded_user["email"], "password": seeded_user["password"]}
    )
    refresh_token = login.json()["refresh_token"]

    def _attempt() -> int:
        return client.post("/v1/auth/refresh", json={"refresh_token": refresh_token}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _attempt(), range(2)))

    # FR-010/SC-002: of two concurrent exchanges, at most one succeeds.
    assert sorted(results) == [200, 401]


def test_logout_revokes_refresh_token(client, seeded_user) -> None:
    login = client.post(
        "/v1/auth/login", json={"email": seeded_user["email"], "password": seeded_user["password"]}
    )
    refresh_token = login.json()["refresh_token"]

    logout = client.post("/v1/auth/logout", json={"refresh_token": refresh_token})
    assert logout.status_code == 204

    # SC-003: after logout the token authenticates 0 subsequent requests.
    after = client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert after.status_code == 401

    # Idempotent -- logging out again is still 204.
    logout_again = client.post("/v1/auth/logout", json={"refresh_token": refresh_token})
    assert logout_again.status_code == 204


def test_repeated_bad_logins_are_rate_limited(client) -> None:
    """FR-007/SC-009: repeated bad logins throttle to a uniform 429."""
    email = f"rl-endpoint-{uuid.uuid4().hex}@example.com"

    last_status = None
    for _ in range(10):
        response = client.post("/v1/auth/login", json={"email": email, "password": "wrong"})
        last_status = response.status_code
        if last_status == 429:
            break

    assert last_status == 429


def test_unknown_email_and_wrong_password_are_byte_identical(client, seeded_user) -> None:
    unknown = client.post(
        "/v1/auth/login", json={"email": "no-such-user@example.com", "password": "whatever"}
    )
    wrong_password = client.post(
        "/v1/auth/login", json={"email": seeded_user["email"], "password": "wrong-password"}
    )

    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json() == wrong_password.json()
