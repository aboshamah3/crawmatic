"""EPA B8 (F16): `GET /health/scraping` never gates `GET /ready`.

`apps.api.app.routers.health` reports the scraping pipeline's own
health (breaker evaluation age, 24h price-freshness fraction, oldest
PENDING scrape target) as an independent signal from
`apps.api.app.routers.ready`'s dependency-readiness verdict. This test
proves the split holds: `/ready` can be a clean 200 (every dependency
up) at the exact same moment `/health/scraping` reports a degraded
scraping pipeline — two different questions, two different answers,
neither one able to flip the other's status code.
"""

from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import health, ready


class _AlembicRow:
    def __init__(self, head: str) -> None:
        self._head = head

    def __getitem__(self, index: int) -> str:
        assert index == 0
        return self._head


class _AlembicResult:
    def __init__(self, row: _AlembicRow | None) -> None:
        self._row = row

    def first(self) -> _AlembicRow | None:
        return self._row


class _ReadySession:
    """Every dependency `/ready` checks reports healthy."""

    def __init__(self, *, head: str = "test_head_0001") -> None:
        self._head = head

    def execute(self, statement: object = None, *_a: object, **_k: object) -> object:
        rendered = str(statement)
        if "alembic_version" in rendered:
            return _AlembicResult(_AlembicRow(self._head))
        return None


class _ReadyRedis:
    def ping(self) -> bool:
        return True


class _OneResult:
    def __init__(self, *values: object) -> None:
        self._values = values

    def one(self) -> tuple[object, ...]:
        return tuple(self._values)


class _FirstResult:
    def __init__(self, row: object) -> None:
        self._row = row

    def first(self) -> object:
        return self._row


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _ScrapingHealthSession:
    """Answers `/health/scraping`'s three queries by dispatching on the
    rendered statement's table name — same style the `/ready` fakes in
    `test_ready_endpoint.py` use."""

    def __init__(
        self,
        *,
        breaker_row: tuple[object, str] | None = None,
        freshness_counts: tuple[int, int] = (0, 0),
        oldest_pending: object | None = None,
    ) -> None:
        self._breaker_row = breaker_row
        self._freshness_counts = freshness_counts
        self._oldest_pending = oldest_pending

    def execute(self, statement: object = None, *_a: object, **_k: object) -> object:
        rendered = str(statement)
        if "proxy_circuit_breakers" in rendered:
            return _FirstResult(self._breaker_row)
        if "competitor_product_matches" in rendered:
            return _OneResult(*self._freshness_counts)
        if "scrape_job_targets" in rendered:
            return _ScalarResult(self._oldest_pending)
        raise AssertionError(f"unexpected statement in _ScrapingHealthSession: {rendered!r}")

    def __enter__(self) -> "_ScrapingHealthSession":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def _pin_code_migration_head(monkeypatch: pytest.MonkeyPatch) -> None:
    from app_shared import heartbeat as heartbeat_mod
    from app_shared import release as release_mod

    monkeypatch.setattr(release_mod, "code_migration_head", lambda: "test_head_0001")
    monkeypatch.delenv(heartbeat_mod.REQUIRED_SERVICES_ENV, raising=False)
    release_mod.reset_release_identity_cache()


@pytest.fixture(autouse=True)
def _reset_ready_generation_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/ready`'s module-level cache/lock is process-wide; give each test a
    clean, uncontended one so it always computes a fresh (non-stale)
    result here."""
    import threading

    monkeypatch.setattr(ready, "_last_response", None)
    monkeypatch.setattr(ready, "_last_status_code", 503)
    monkeypatch.setattr(ready, "_generation_lock", threading.Lock())


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_ready_returns_200_while_health_scraping_reports_degraded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `/ready`: every dependency healthy.
    monkeypatch.setattr(ready, "get_session", lambda: contextlib.nullcontext(_ReadySession()))
    monkeypatch.setattr(ready, "get_redis_client", lambda: _ReadyRedis())

    # `/health/scraping`: 10 ACTIVE matches, zero fresh in the last 24h —
    # a degraded scraping pipeline that has nothing to do with `/ready`'s
    # dependencies, all of which are healthy above.
    scraping_session = _ScrapingHealthSession(
        breaker_row=(_utcnow(), "CLOSED"),
        freshness_counts=(10, 0),
        oldest_pending=None,
    )
    monkeypatch.setattr(health, "get_session", lambda: scraping_session)

    ready_resp = client.get("/ready")
    scraping_resp = client.get("/health/scraping")

    assert ready_resp.status_code == 200
    assert ready_resp.json()["ready"] is True

    scraping_body = scraping_resp.json()
    assert scraping_resp.status_code == 200, "scraping health never gates /ready's status code"
    assert scraping_body["status"] == "degraded"
    assert scraping_body["freshness_fraction_24h"] == 0.0


def test_health_scraping_reports_ok_when_freshness_is_healthy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    scraping_session = _ScrapingHealthSession(
        breaker_row=(_utcnow(), "CLOSED"),
        freshness_counts=(10, 10),
        oldest_pending=None,
    )
    monkeypatch.setattr(health, "get_session", lambda: scraping_session)

    resp = client.get("/health/scraping")
    body = resp.json()

    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["freshness_fraction_24h"] == 1.0


def test_live_never_touches_a_dependency(client: TestClient) -> None:
    """No monkeypatching at all — `/live` must not need a database or
    Redis to answer 200."""
    resp = client.get("/live")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)
