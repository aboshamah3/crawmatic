"""`GET /ready` — readiness probe (2026-08-20 prelaunch hardening audit).

Unauthenticated like `/health` and `/version`; unlike `/health` (which
`specs/001-monorepo-skeleton/contracts/health.md` forbids from touching
any dependency) it checks the database (`SELECT 1`) and Redis (`PING`).
These tests override the router's own `_get_db_session` and
`_get_redis_dependency` dependencies with tiny fakes rather than a real
engine/client — the same style `test_version_endpoint.py` uses for its
one DB dependency.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import ready


class _FakeSession:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises

    def execute(self, *_args: object, **_kwargs: object) -> None:
        if self._raises is not None:
            raise self._raises


class _FakeRedis:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises

    def ping(self) -> bool:
        if self._raises is not None:
            raise self._raises
        return True


class _HangingCallable:
    """A callable that blocks longer than the check budget — proves the
    timebox works without hanging the test process itself.

    Deliberately a *finite* sleep, not an unbounded block: a real
    `ThreadPoolExecutor` worker that never returns would still be alive
    (blocked inside the submitted call) when `pool.shutdown(wait=False)`
    returns — that's the whole point of using `wait=False` — but
    `concurrent.futures`' own `atexit` hook joins every outstanding
    worker thread at interpreter shutdown, so a call that never finishes
    would hang the whole pytest process at teardown, not just this test.
    A short, finite sleep proves the same thing (the response comes back
    before the call itself completes) without paying that cost.
    """

    def __init__(self, *, sleep_seconds: float) -> None:
        self._sleep_seconds = sleep_seconds

    def execute(self, *_args: object, **_kwargs: object) -> None:
        import time

        time.sleep(self._sleep_seconds)

    def ping(self) -> bool:
        import time

        time.sleep(self._sleep_seconds)
        return True


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _override_session(session: object) -> None:
    def _fake_dependency() -> Iterator[object]:
        yield session

    app.dependency_overrides[ready._get_db_session] = _fake_dependency


def _override_redis(client_obj: object) -> None:
    def _fake_dependency() -> object:
        return client_obj

    app.dependency_overrides[ready._get_redis_dependency] = _fake_dependency


def _setup(*, session: object, redis_client: object) -> None:
    _override_session(session)
    _override_redis(redis_client)


def test_ready_requires_no_auth_header(client: TestClient) -> None:
    _setup(session=_FakeSession(), redis_client=_FakeRedis())

    resp = client.get("/ready")

    assert resp.status_code == 200


def test_ready_all_deps_up_returns_200_and_ready_true(client: TestClient) -> None:
    _setup(session=_FakeSession(), redis_client=_FakeRedis())

    resp = client.get("/ready")
    body = resp.json()

    assert resp.status_code == 200
    assert body == {
        "ready": True,
        "checks": {
            "database": {"ok": True, "error": None},
            "redis": {"ok": True, "error": None},
        },
    }


def test_ready_database_down_returns_503(client: TestClient) -> None:
    _setup(
        session=_FakeSession(raises=RuntimeError("connection refused")),
        redis_client=_FakeRedis(),
    )

    resp = client.get("/ready")
    body = resp.json()

    assert resp.status_code == 503
    assert body["ready"] is False
    assert body["checks"]["database"]["ok"] is False
    # Class name ONLY — the exception's own message is never published, so
    # nothing the driver chose to put in it can reach this body.
    assert body["checks"]["database"]["error"] == "RuntimeError"
    assert "connection refused" not in resp.text
    assert body["checks"]["redis"]["ok"] is True


def test_ready_redis_down_returns_503(client: TestClient) -> None:
    _setup(
        session=_FakeSession(),
        redis_client=_FakeRedis(raises=ConnectionError("redis://user:pw@host:6379 refused")),
    )

    resp = client.get("/ready")
    body = resp.json()

    assert resp.status_code == 503
    assert body["ready"] is False
    assert body["checks"]["redis"]["ok"] is False
    assert body["checks"]["redis"]["error"] == "ConnectionError"
    # The fixture's message is a Redis URL with a password in it; none of it
    # may appear anywhere in the response.
    assert "redis://" not in resp.text
    assert "pw" not in resp.text
    assert body["checks"]["database"]["ok"] is True


def test_ready_both_deps_down_returns_503_with_both_reported(client: TestClient) -> None:
    _setup(
        session=_FakeSession(raises=RuntimeError("db gone")),
        redis_client=_FakeRedis(raises=ConnectionError("redis gone")),
    )

    resp = client.get("/ready")
    body = resp.json()

    assert resp.status_code == 503
    assert body["ready"] is False
    assert body["checks"]["database"]["ok"] is False
    assert body["checks"]["redis"]["ok"] is False


def test_ready_error_never_carries_the_connection_string(client: TestClient) -> None:
    """A DSN lives at the FRONT of a driver's error text, so truncating the
    message keeps exactly the part that must never be published on an
    unauthenticated probe. Only the class name is reported."""
    dsn = "postgresql://user:S3cr3tPassw0rd@10.0.0.5:5432/db"
    long_secret_looking_message = f"{dsn} could not connect " + ("x" * 500)
    _setup(
        session=_FakeSession(raises=RuntimeError(long_secret_looking_message)),
        redis_client=_FakeRedis(),
    )

    resp = client.get("/ready")
    body = resp.json()

    error = body["checks"]["database"]["error"]
    assert error == "RuntimeError"
    for secret in (dsn, "S3cr3tPassw0rd", "postgresql://", "10.0.0.5", "user:"):
        assert secret not in resp.text
    # Nothing at all from the message survives — not a prefix, not a suffix.
    assert "could not connect" not in resp.text
    assert "xxx" not in resp.text
    assert "Traceback" not in resp.text


def test_ready_database_hang_times_out_rather_than_blocking(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tiny budget + a call that sleeps a bit longer than it: proves the
    # probe reports a timeout instead of waiting out the slow call,
    # without making the test itself slow.
    monkeypatch.setattr(ready, "_CHECK_TIMEOUT_SECONDS", 0.05)
    hanging = _HangingCallable(sleep_seconds=0.3)
    _setup(session=hanging, redis_client=_FakeRedis())

    resp = client.get("/ready")
    body = resp.json()

    assert resp.status_code == 503
    assert body["checks"]["database"]["ok"] is False
    assert body["checks"]["database"]["error"].startswith("TimeoutError:")
    assert body["checks"]["redis"]["ok"] is True


def test_ready_path_excluded_from_admin_internal_tags() -> None:
    """Sanity: `/ready` isn't accidentally tagged as an internal surface —
    it's a platform probe like `/health`/`/version`, meant to stay public."""
    from app.openapi_public import INTERNAL_TAGS

    assert "ready" not in INTERNAL_TAGS
