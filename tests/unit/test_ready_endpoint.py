"""`GET /ready` — readiness probe (2026-08-20 prelaunch hardening audit;
EPA B8/F16 rewrite, 2026-09-07).

Unauthenticated like `/health` and `/version`; unlike `/health` (which
`specs/001-monorepo-skeleton/contracts/health.md` forbids from touching
any dependency) it checks the database (`SELECT 1`), Redis (`PING`),
the migration head, and required heartbeats.

EPA B8 moved breaker-evidence and freshness checks OUT of `/ready` and
into `GET /health/scraping` (`apps.app.routers.health`,
`tests/unit/test_health_scraping_signal.py`) — see `ready.py`'s module
docstring. It also moved every probe from a shared, request-scoped
session (a FastAPI dependency tests overrode via
`app.dependency_overrides`) to each probe opening its OWN session via
`get_session()`, so these tests now monkeypatch the module-level
`ready.get_session`/`ready.get_redis_client` names directly instead —
the same style `test_ready_no_overlap.py` uses.
"""

from __future__ import annotations

import contextlib
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import ready

#: The migration head both the fake database and the fake running code
#: report, so "every dependency is up" also means "the schema matches the
#: code" — see `_pin_code_migration_head` below.
_HEAD = "test_head_0001"


class _FakeRow:
    def __init__(self, value: str) -> None:
        self._value = value

    def __getitem__(self, index: int) -> str:
        assert index == 0
        return self._value


class _FakeResult:
    def __init__(self, row: _FakeRow | None) -> None:
        self._row = row

    def first(self) -> _FakeRow | None:
        return self._row


class _FakeSession:
    """Answers the `SELECT 1` connectivity probe with nothing, and the
    `alembic_version` read (Task A5) with `head`. Defaulting `head` to
    `_HEAD` keeps every pre-existing test in this file meaning exactly
    what it always meant: an all-dependencies-up fixture also has a
    schema its code agrees with."""

    def __init__(
        self,
        *,
        raises: Exception | None = None,
        head: str | None = _HEAD,
    ) -> None:
        self._raises = raises
        self._head = head

    def execute(self, statement: object = None, *_args: object, **_kwargs: object) -> object:
        if self._raises is not None:
            raise self._raises
        rendered = str(statement)
        if "alembic_version" in rendered:
            return _FakeResult(_FakeRow(self._head) if self._head is not None else None)
        return None


class _FakeRedis:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises

    def ping(self) -> bool:
        if self._raises is not None:
            raise self._raises
        return True


class _HangingSession:
    """A session whose `execute` blocks longer than the check budget —
    proves the timebox works without hanging the test process itself.

    Deliberately a *finite* sleep, not an unbounded block: a genuinely
    stuck worker thread in the shared, process-wide `_PROBE_EXECUTOR`
    would otherwise still be running when the interpreter's own
    `concurrent.futures` `atexit` hook tries to join every outstanding
    worker at shutdown. A short, finite sleep proves the same thing (the
    response comes back before the call itself completes) without
    paying that cost.
    """

    def __init__(self, *, sleep_seconds: float) -> None:
        self._sleep_seconds = sleep_seconds

    def execute(self, *_args: object, **_kwargs: object) -> None:
        time.sleep(self._sleep_seconds)


@pytest.fixture(autouse=True)
def _pin_code_migration_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decouple these tests from the repo's real, moving migration head — the
    same decoupling `test_version_endpoint.py` does by monkeypatching
    `version._code_migration_head`. Also ensures no heartbeat services are
    declared, so `checks.heartbeats` reports its "not-configured" absence
    rather than depending on this machine's environment."""
    from app_shared import heartbeat as heartbeat_mod
    from app_shared import release as release_mod

    monkeypatch.setattr(release_mod, "code_migration_head", lambda: _HEAD)
    monkeypatch.delenv(heartbeat_mod.REQUIRED_SERVICES_ENV, raising=False)
    release_mod.reset_release_identity_cache()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: object) -> None:
    monkeypatch.setattr(ready, "get_session", lambda: contextlib.nullcontext(session))


def _patch_redis(monkeypatch: pytest.MonkeyPatch, redis_client: object) -> None:
    monkeypatch.setattr(ready, "get_redis_client", lambda: redis_client)


def _setup(
    monkeypatch: pytest.MonkeyPatch, *, session: object, redis_client: object
) -> None:
    _patch_session(monkeypatch, session)
    _patch_redis(monkeypatch, redis_client)


def test_ready_requires_no_auth_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(monkeypatch, session=_FakeSession(), redis_client=_FakeRedis())

    resp = client.get("/ready")

    assert resp.status_code == 200


def test_ready_all_deps_up_returns_200_and_ready_true(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(monkeypatch, session=_FakeSession(), redis_client=_FakeRedis())

    resp = client.get("/ready")
    body = resp.json()

    assert resp.status_code == 200
    assert body["ready"] is True
    assert body["stale"] is False
    # `checks` gained `migrations` and `heartbeats` in READY-001 / Task A5.
    # `breaker_evidence` moved OUT to `/health/scraping` in EPA B8 (F16).
    # Asserted key-by-key rather than as one exact dict so the next check
    # this probe legitimately grows does not read as a regression here; what
    # matters is that every check is present, passing, and error-free.
    assert set(body["checks"]) == {"database", "redis", "migrations", "heartbeats"}
    for name, check in body["checks"].items():
        assert check["ok"] is True, name
        assert check["error"] is None, name
    assert body["checks"]["heartbeats"]["detail"] == "not-configured"


def test_ready_database_down_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(
        monkeypatch,
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


def test_ready_redis_down_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(
        monkeypatch,
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


def test_ready_both_deps_down_returns_503_with_both_reported(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(
        monkeypatch,
        session=_FakeSession(raises=RuntimeError("db gone")),
        redis_client=_FakeRedis(raises=ConnectionError("redis gone")),
    )

    resp = client.get("/ready")
    body = resp.json()

    assert resp.status_code == 503
    assert body["ready"] is False
    assert body["checks"]["database"]["ok"] is False
    assert body["checks"]["redis"]["ok"] is False


def test_ready_error_never_carries_the_connection_string(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DSN lives at the FRONT of a driver's error text, so truncating the
    message keeps exactly the part that must never be published on an
    unauthenticated probe. Only the class name is reported."""
    dsn = "postgresql://user:S3cr3tPassw0rd@10.0.0.5:5432/db"
    long_secret_looking_message = f"{dsn} could not connect " + ("x" * 500)
    _setup(
        monkeypatch,
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
    _setup(
        monkeypatch,
        session=_HangingSession(sleep_seconds=0.3),
        redis_client=_FakeRedis(),
    )

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
