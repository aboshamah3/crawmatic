"""Fail-secure abuse limits on the expensive engine surfaces.

EPA W5.5-L1 item 2. The two properties that matter are the ones a limiter is
usually missing: refused attempts still count, and a store failure REFUSES
rather than admits. Both are asserted directly, plus the capability probe
that keeps the whole thing inert until its migration has run.

No database: the store seam is one session object, so a fake exercises every
branch — including the ones a real Postgres will not produce on demand.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.abuse_limit import (
    AbuseLimitMiddleware,
    CounterUnavailable,
    SURFACES,
    record_attempt,
    surface_for,
    window_start_for,
)

BEARER = {"Authorization": "Bearer test-credential"}
OTHER_BEARER = {"Authorization": "Bearer a-different-credential"}


class FakeSession:
    """A session shaped like the one `get_session()` yields.

    `table_exists` drives the capability probe; `fail_on_increment` makes the
    counting statement raise the way an unreachable database would.
    """

    def __init__(self, *, table_exists: bool = True, fail_on_increment: bool = False):
        self.table_exists = table_exists
        self.fail_on_increment = fail_on_increment
        self.counts: dict[tuple[str, datetime], int] = {}
        self.commits = 0
        self.rollbacks = 0

    # -- context manager --------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # -- sqlalchemy surface ------------------------------------------------
    def execute(self, statement, params=None):
        sql = str(statement)
        if "to_regclass" in sql:
            return _Scalar(self.table_exists)
        if self.fail_on_increment:
            raise RuntimeError("counter store is unreachable")
        key = (params["bucket_key"], params["window_starts_at"])
        self.counts[key] = self.counts.get(key, 0) + 1
        return _Scalar(self.counts[key])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def scalar_one(self):
        return self._value


def _app(session: FakeSession, **kwargs) -> TestClient:
    app = FastAPI()

    @app.post("/v1/strategy/discovery-runs")
    def discovery() -> dict:
        return {"ok": True}

    @app.post("/v1/jobs/run/match/abc")
    def recheck() -> dict:
        return {"ok": True}

    @app.get("/v1/admin/usage")
    def export_usage() -> dict:
        return {"ok": True}

    @app.get("/v1/products")
    def unlimited() -> dict:
        return {"ok": True}

    app.add_middleware(
        AbuseLimitMiddleware, session_factory=lambda: session, **kwargs
    )
    return TestClient(app)


# --- surface matching -------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/v1/strategy/discovery-runs", "discovery"),
        ("POST", "/v1/jobs/run/match/abc", "recheck"),
        ("POST", "/v1/variants/abc/rescrape", "recheck"),
        ("GET", "/v1/admin/usage", "export"),
        ("POST", "/v1/admin/workspaces", "admin"),
        ("GET", "/v1/products", None),
        ("GET", "/health", None),
        # The discovery LIST is a read; only the trigger is limited here.
        ("GET", "/v1/strategy/discovery-runs", "admin" if False else None),
    ],
)
def test_surface_matching(method: str, path: str, expected: str | None) -> None:
    surface = surface_for(method, path)
    assert (surface.name if surface else None) == expected


def test_the_export_rule_precedes_the_admin_catch_all() -> None:
    """Order is load-bearing: a catch-all first would swallow the export."""
    names = [s.name for s in SURFACES]
    assert names.index("export") < names.index("admin")


def test_every_surface_has_a_positive_limit() -> None:
    """A limit of 0 would admit nothing; a negative one is a typo, not a policy."""
    assert all(s.limit > 0 and s.window_seconds > 0 for s in SURFACES)


# --- the window -------------------------------------------------------------


def test_window_start_truncates_and_is_utc() -> None:
    start = window_start_for(1_700_000_123.9, 60)
    assert start == datetime(2023, 11, 14, 22, 15, tzinfo=timezone.utc)
    assert start.tzinfo is timezone.utc


def test_the_same_window_is_shared_across_a_window_length() -> None:
    boundary = 1_699_999_200  # exactly 2023-11-14T22:00:00Z
    assert window_start_for(boundary, 3600) == window_start_for(boundary + 3599, 3600)
    assert window_start_for(boundary, 3600) != window_start_for(boundary + 3600, 3600)


# --- the store --------------------------------------------------------------


def test_record_attempt_counts_from_one() -> None:
    """The attempt that creates the row is itself an attempt."""
    session = FakeSession()
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert record_attempt(session, bucket_key="k", window_starts_at=at) == 1
    assert record_attempt(session, bucket_key="k", window_starts_at=at) == 2


def test_record_attempt_returns_none_when_the_table_is_absent() -> None:
    session = FakeSession(table_exists=False)
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert record_attempt(session, bucket_key="k", window_starts_at=at) is None


def test_record_attempt_raises_and_rolls_back_on_a_store_failure() -> None:
    session = FakeSession(fail_on_increment=True)
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(CounterUnavailable):
        record_attempt(session, bucket_key="k", window_starts_at=at)
    assert session.rollbacks == 1


# --- the middleware: admitting and refusing --------------------------------


def test_an_unlimited_path_is_never_counted() -> None:
    session = FakeSession()
    client = _app(session)
    assert client.get("/v1/products", headers=BEARER).status_code == 200
    assert session.counts == {}


def test_an_unauthenticated_request_is_not_counted() -> None:
    """Counting it would make unauthenticated traffic cheaper to amplify."""
    session = FakeSession()
    client = _app(session)
    assert client.post("/v1/strategy/discovery-runs").status_code == 200
    assert session.counts == {}


def test_a_limited_surface_is_counted_and_reports_its_budget() -> None:
    session = FakeSession()
    client = _app(session)
    response = client.post("/v1/strategy/discovery-runs", headers=BEARER)
    assert response.status_code == 200
    assert response.headers["X-AbuseLimit-Limit"] == "20"
    assert response.headers["X-AbuseLimit-Remaining"] == "19"
    assert len(session.counts) == 1


def test_the_bucket_key_never_contains_the_credential() -> None:
    """Bucket keys reach logs and metrics; a credential in one is a leak."""
    session = FakeSession()
    client = _app(session)
    client.post("/v1/strategy/discovery-runs", headers=BEARER)
    keys = [k for (k, _) in session.counts]
    assert keys and all("test-credential" not in k for k in keys)
    assert all(k.startswith("abuse:discovery:") for k in keys)


def test_two_credentials_get_two_buckets() -> None:
    session = FakeSession()
    client = _app(session)
    client.post("/v1/strategy/discovery-runs", headers=BEARER)
    client.post("/v1/strategy/discovery-runs", headers=OTHER_BEARER)
    assert len(session.counts) == 2


def test_the_limit_refuses_with_429_and_a_retry_after() -> None:
    session = FakeSession()
    client = _app(session)
    limit = next(s.limit for s in SURFACES if s.name == "discovery")
    for _ in range(limit):
        assert client.post("/v1/strategy/discovery-runs", headers=BEARER).status_code == 200
    refused = client.post("/v1/strategy/discovery-runs", headers=BEARER)
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "RATE_LIMITED"
    assert int(refused.headers["Retry-After"]) >= 1


def test_refused_attempts_still_count() -> None:
    """The bypass this closes: if a refusal did not increment, a caller at the
    ceiling would be refused, would not count, and would be admitted on the
    next request — one free request per window, forever."""
    session = FakeSession()
    client = _app(session)
    limit = next(s.limit for s in SURFACES if s.name == "discovery")
    for _ in range(limit + 5):
        client.post("/v1/strategy/discovery-runs", headers=BEARER)
    (count,) = session.counts.values()
    assert count == limit + 5
    # ...and it is still refused, not admitted again.
    assert client.post("/v1/strategy/discovery-runs", headers=BEARER).status_code == 429


# --- FAIL CLOSED ------------------------------------------------------------


def test_a_store_failure_refuses_rather_than_admits() -> None:
    """The whole point of this module. A limiter that disappears when its
    store does is not a limiter."""
    session = FakeSession(fail_on_increment=True)
    client = _app(session)
    response = client.post("/v1/strategy/discovery-runs", headers=BEARER)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


def test_a_session_that_cannot_even_be_opened_refuses() -> None:
    app = FastAPI()

    @app.post("/v1/strategy/discovery-runs")
    def discovery() -> dict:  # pragma: no cover - must never be reached
        return {"ok": True}

    def explode():
        raise RuntimeError("no database")

    app.add_middleware(AbuseLimitMiddleware, session_factory=explode)
    client = TestClient(app)
    assert client.post("/v1/strategy/discovery-runs", headers=BEARER).status_code == 429


def test_the_export_surface_fails_closed_too() -> None:
    session = FakeSession(fail_on_increment=True)
    client = _app(session)
    assert client.get("/v1/admin/usage", headers=BEARER).status_code == 429


# --- the capability probe ---------------------------------------------------


def test_the_limiter_is_inert_before_its_migration_has_run() -> None:
    """Failing closed on a table that was never created would 503 the whole
    API the moment this code deploys ahead of its migration."""
    session = FakeSession(table_exists=False)
    client = _app(session)
    response = client.post("/v1/strategy/discovery-runs", headers=BEARER)
    assert response.status_code == 200
    assert "X-AbuseLimit-Limit" not in response.headers


def test_a_failing_probe_assumes_the_table_is_present() -> None:
    """Guessing 'absent' would silently disable a safety control on a blip."""

    class ProbeExplodes(FakeSession):
        def execute(self, statement, params=None):
            if "to_regclass" in str(statement):
                raise RuntimeError("catalog read failed")
            return super().execute(statement, params)

    session = ProbeExplodes()
    client = _app(session)
    response = client.post("/v1/strategy/discovery-runs", headers=BEARER)
    assert response.status_code == 200
    assert response.headers["X-AbuseLimit-Limit"] == "20"


def test_an_unconfigured_process_disables_the_limiter_rather_than_429ing() -> None:
    """No `.env` in this process means no store to consult — not a refusal.

    Same position `app.rate_limit` takes, and not a production hole:
    `app.main` calls `assert_production_safe()` at import, so a production
    API that cannot build `Settings` never serves a request at all.
    """
    from app import abuse_limit as module

    assert module._database_is_configured() is False

    app = FastAPI()

    @app.post("/v1/strategy/discovery-runs")
    def discovery() -> dict:
        return {"ok": True}

    # No injected factory, so the default `get_session` path is taken — and
    # that is the path the configuration check guards.
    app.add_middleware(AbuseLimitMiddleware)
    client = TestClient(app)
    assert client.post("/v1/strategy/discovery-runs", headers=BEARER).status_code == 200


def test_the_middleware_can_be_switched_off_entirely() -> None:
    session = FakeSession(fail_on_increment=True)
    client = _app(session, enabled=False)
    assert client.post("/v1/strategy/discovery-runs", headers=BEARER).status_code == 200
