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
from datetime import UTC, datetime, timedelta

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
    """Answers the `SELECT 1` connectivity probe with nothing, the
    `alembic_version` read (added to `/ready` by READY-001 / Task A5) with
    `head`, and the `proxy_circuit_breakers` read (EPA B1) with a row
    `breaker_age_seconds` old. Defaulting `head` to `_HEAD` and the
    breaker age to zero keeps every pre-existing test in this file meaning
    exactly what it always meant: an all-dependencies-up fixture now also
    has a schema its code agrees with and breaker evidence the cost gate
    accepts."""

    def __init__(
        self,
        *,
        raises: Exception | None = None,
        head: str | None = _HEAD,
        breaker_age_seconds: float = 0.0,
        breaker_row: bool = True,
    ) -> None:
        self._raises = raises
        self._head = head
        self._breaker_age_seconds = breaker_age_seconds
        self._breaker_row = breaker_row

    def execute(self, statement: object = None, *_args: object, **_kwargs: object) -> object:
        if self._raises is not None:
            raise self._raises
        rendered = str(statement)
        if "alembic_version" in rendered:
            return _FakeResult(_FakeRow(self._head) if self._head is not None else None)
        if "proxy_circuit_breakers" in rendered:
            if not self._breaker_row:
                return _FakeResult(None)
            evaluated_at = datetime.now(UTC) - timedelta(
                seconds=self._breaker_age_seconds
            )
            return _FakeResult((evaluated_at, "CLOSED"))
        return None


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
    assert body["ready"] is True
    # `checks` gained `migrations` and `heartbeats` in READY-001 / Task A5.
    # Asserted key-by-key rather than as one exact dict so the next check
    # this probe legitimately grows does not read as a regression here; what
    # matters is that every check is present, passing, and error-free.
    assert set(body["checks"]) == {
        "database",
        "redis",
        "migrations",
        "heartbeats",
        # EPA B1.
        "breaker_evidence",
    }
    for name, check in body["checks"].items():
        assert check["ok"] is True, name
        assert check["error"] is None, name
    assert body["checks"]["heartbeats"]["detail"] == "not-configured"


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


# --- breaker evidence (EPA B1, 2026-09-03) -----------------------------------
#
# The cost gate fails CLOSED on `proxy_circuit_breakers.evaluated_at`
# older than `DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS`: a stale row
# denies every paid scrape in the fleet. That is a total outage of the
# product's main job which, until this check existed, was invisible from
# outside — every dependency up, every probe green, nothing able to run.


def test_ready_reports_fresh_breaker_evidence(client: TestClient) -> None:
    _setup(session=_FakeSession(breaker_age_seconds=60), redis_client=_FakeRedis())

    resp = client.get("/ready")
    body = resp.json()

    assert resp.status_code == 200
    assert body["checks"]["breaker_evidence"]["ok"] is True
    assert body["checks"]["breaker_evidence"]["error"] is None
    assert "age_seconds=" in body["checks"]["breaker_evidence"]["detail"]


def test_ready_returns_503_when_breaker_evidence_is_stale(client: TestClient) -> None:
    """4000s > the 3600s the gate accepts, so the fleet is denying all paid
    work. Everything else is healthy — which is exactly the state this
    check exists to make visible."""
    _setup(session=_FakeSession(breaker_age_seconds=4000), redis_client=_FakeRedis())

    resp = client.get("/ready")
    body = resp.json()

    assert resp.status_code == 503
    assert body["ready"] is False
    assert body["checks"]["breaker_evidence"]["ok"] is False
    assert body["checks"]["breaker_evidence"]["error"] == "stale"
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["redis"]["ok"] is True


def test_ready_returns_503_when_the_breaker_row_does_not_exist(
    client: TestClient,
) -> None:
    """No row at all is the same outage with a different cause: the gate
    has no evidence to read, so it denies."""
    _setup(session=_FakeSession(breaker_row=False), redis_client=_FakeRedis())

    resp = client.get("/ready")
    body = resp.json()

    assert resp.status_code == 503
    assert body["checks"]["breaker_evidence"]["ok"] is False
    assert body["checks"]["breaker_evidence"]["error"] == "no-row"
    assert "denies all paid work" in body["checks"]["breaker_evidence"]["detail"]


def test_ready_breaker_check_is_skipped_when_the_database_is_down(
    client: TestClient,
) -> None:
    """One root cause, reported once — and never by touching a session a
    timed-out worker thread may still hold."""
    _setup(
        session=_FakeSession(raises=RuntimeError("connection refused")),
        redis_client=_FakeRedis(),
    )

    resp = client.get("/ready")
    body = resp.json()

    assert resp.status_code == 503
    assert body["checks"]["breaker_evidence"]["error"] == "DatabaseUnavailable"


def test_ready_reports_a_disabled_breaker_as_a_labelled_absence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PROXY_BREAKER_ENABLED=false` means there is no gate to deadlock, so
    there is no evidence to be stale — reported the way undeclared
    heartbeat services are, not as a pass and not as a failure."""
    monkeypatch.setattr(ready, "_breaker_enabled", lambda: False)
    _setup(session=_FakeSession(breaker_age_seconds=999_999), redis_client=_FakeRedis())

    resp = client.get("/ready")
    body = resp.json()

    assert resp.status_code == 200
    assert body["checks"]["breaker_evidence"]["ok"] is True
    assert body["checks"]["breaker_evidence"]["detail"] == "disabled"
