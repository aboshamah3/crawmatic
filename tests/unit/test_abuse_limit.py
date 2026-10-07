"""Abuse limits on the expensive engine surfaces, counted on async Redis.

EPA W5.5-L1 item 2; store moved from a synchronous Postgres upsert to the
async Redis admission client by the 2026-10-07 risk review (B3/E8). The
properties asserted here: refused attempts still count; admin WRITES fail
CLOSED on a store error while READS fail OPEN; workspaces under
`/v1/admin/workspaces/{id}` get their own bucket; no SQLAlchemy session is
touched on the request path; the configuration probe is asked once, not per
request.

No Redis server: the store seam is the injected `redis_factory`, and a
hand-rolled async double (same protocol as tests/unit/test_rate_limit_async.py)
exercises every branch.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import abuse_limit as module
from app.abuse_limit import (
    AbuseLimitMiddleware,
    SURFACES,
    bucket_key_for,
    fails_open,
    surface_for,
    window_start_for,
    workspace_for,
)

BEARER = {"Authorization": "Bearer test-credential"}
OTHER_BEARER = {"Authorization": "Bearer a-different-credential"}
WS_A = str(uuid.uuid4())
WS_B = str(uuid.uuid4())


class _FakeScript:
    def __init__(self, redis: "FakeAsyncRedis") -> None:
        self._redis = redis

    async def __call__(self, keys=None, args=None):
        if self._redis.delay_seconds:
            await asyncio.sleep(self._redis.delay_seconds)
        if self._redis.fail:
            raise ConnectionError("redis unavailable")
        self._redis.script_calls += 1
        ttl = int(args[0])
        limits = [int(v) for v in args[1:]]
        counts = []
        for index, key in enumerate(keys):
            count = self._redis.counters.get(key, 0) + 1
            self._redis.counters[key] = count
            if count == 1:
                self._redis.expirations[key] = ttl
            counts.append(count)
            if index < len(limits) and count > limits[index]:
                break
        return counts


class FakeAsyncRedis:
    """Async double of `redis.asyncio.Redis` for the one call the limiter makes."""

    def __init__(self, *, fail: bool = False, delay_seconds: float = 0.0) -> None:
        self.counters: dict[str, int] = {}
        self.expirations: dict[str, int] = {}
        self.script_calls = 0
        self.fail = fail
        self.delay_seconds = delay_seconds

    def register_script(self, _src: str) -> _FakeScript:
        return _FakeScript(self)


def _app(redis: FakeAsyncRedis, **kwargs) -> TestClient:
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

    @app.get("/v1/admin/workspaces/{workspace_id}/control-plane/state")
    def admin_read(workspace_id: str) -> dict:
        return {"ok": True}

    @app.post("/v1/admin/workspaces/{workspace_id}/keys")
    def admin_write(workspace_id: str) -> dict:
        return {"ok": True}

    @app.post("/v1/admin/workspaces")
    def admin_create() -> dict:
        return {"ok": True}

    @app.get("/v1/products")
    def unlimited() -> dict:
        return {"ok": True}

    app.add_middleware(AbuseLimitMiddleware, redis_factory=lambda: redis, **kwargs)
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


# --- bucket keys ------------------------------------------------------------


def test_workspace_is_parsed_from_admin_workspace_paths_only() -> None:
    assert workspace_for(f"/v1/admin/workspaces/{WS_A}") == WS_A
    assert workspace_for(f"/v1/admin/workspaces/{WS_A}/keys") == WS_A
    assert workspace_for("/v1/admin/workspaces") is None
    assert workspace_for("/v1/admin/usage") is None
    # Arbitrary path text never becomes part of a key.
    assert workspace_for("/v1/admin/workspaces/a.b%2Fc/keys") is None
    assert workspace_for("/v1/admin/workspaces/" + "x" * 65) is None


def test_bucket_key_shapes() -> None:
    admin = next(s for s in SURFACES if s.name == "admin")
    assert bucket_key_for(admin, "idh", f"/v1/admin/workspaces/{WS_A}/keys", 7) == (
        f"abuse:admin:idh:ws:{WS_A}:7"
    )
    assert bucket_key_for(admin, "idh", "/v1/admin/workspaces", 7) == "abuse:admin:idh:7"


def test_only_reads_fail_open() -> None:
    assert fails_open("GET") and fails_open("head") and fails_open("OPTIONS")
    assert not any(fails_open(m) for m in ("POST", "PUT", "PATCH", "DELETE"))


# --- counting ---------------------------------------------------------------


def test_an_unlimited_path_is_never_counted() -> None:
    redis = FakeAsyncRedis()
    client = _app(redis)
    assert client.get("/v1/products", headers=BEARER).status_code == 200
    assert redis.counters == {} and redis.script_calls == 0


def test_an_unauthenticated_request_is_not_counted() -> None:
    """Counting it would make unauthenticated traffic cheaper to amplify."""
    redis = FakeAsyncRedis()
    client = _app(redis)
    assert client.post("/v1/strategy/discovery-runs").status_code == 200
    assert redis.counters == {}


def test_a_limited_surface_is_counted_once_and_reports_its_budget() -> None:
    redis = FakeAsyncRedis()
    client = _app(redis)
    response = client.post("/v1/strategy/discovery-runs", headers=BEARER)
    assert response.status_code == 200
    assert response.headers["X-AbuseLimit-Limit"] == "20"
    assert response.headers["X-AbuseLimit-Remaining"] == "19"
    assert redis.script_calls == 1
    (key,) = redis.counters
    # EXPIRE on first hit, two windows long.
    assert redis.expirations[key] == 2 * 3600


def test_the_bucket_key_never_contains_the_credential() -> None:
    """Bucket keys reach logs and metrics; a credential in one is a leak."""
    redis = FakeAsyncRedis()
    client = _app(redis)
    client.post("/v1/strategy/discovery-runs", headers=BEARER)
    keys = list(redis.counters)
    assert keys and all("test-credential" not in k for k in keys)
    assert all(k.startswith("abuse:discovery:") for k in keys)


def test_two_credentials_get_two_buckets() -> None:
    redis = FakeAsyncRedis()
    client = _app(redis)
    client.post("/v1/strategy/discovery-runs", headers=BEARER)
    client.post("/v1/strategy/discovery-runs", headers=OTHER_BEARER)
    assert len(redis.counters) == 2


def test_the_limit_refuses_with_429_and_a_retry_after() -> None:
    redis = FakeAsyncRedis()
    client = _app(redis)
    limit = next(s.limit for s in SURFACES if s.name == "discovery")
    for _ in range(limit):
        assert client.post("/v1/strategy/discovery-runs", headers=BEARER).status_code == 200
    refused = client.post("/v1/strategy/discovery-runs", headers=BEARER)
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "RATE_LIMITED"
    assert int(refused.headers["Retry-After"]) >= 1


def test_refused_attempts_still_count() -> None:
    """If a refusal did not increment, a caller at the ceiling would be
    admitted on the next request — one free request per window, forever."""
    redis = FakeAsyncRedis()
    client = _app(redis)
    limit = next(s.limit for s in SURFACES if s.name == "discovery")
    for _ in range(limit + 5):
        client.post("/v1/strategy/discovery-runs", headers=BEARER)
    (count,) = redis.counters.values()
    assert count == limit + 5
    assert client.post("/v1/strategy/discovery-runs", headers=BEARER).status_code == 429


# --- per-workspace buckets (B3) --------------------------------------------


def test_two_workspaces_do_not_share_an_admin_bucket(monkeypatch) -> None:
    """One SaaS service credential fronts every tenant; a busy workspace must
    not exhaust the admin budget of every other workspace."""
    monkeypatch.setattr(
        module, "SURFACES", tuple(
            module.Surface(s.name, s.method, s.path_prefix, 3, s.window_seconds)
            if s.name == "admin" else s
            for s in SURFACES
        )
    )
    redis = FakeAsyncRedis()
    client = _app(redis)
    for _ in range(3):
        assert client.post(f"/v1/admin/workspaces/{WS_A}/keys", headers=BEARER).status_code == 200
    assert client.post(f"/v1/admin/workspaces/{WS_A}/keys", headers=BEARER).status_code == 429
    # Same credential, other workspace: its own, untouched bucket.
    assert client.post(f"/v1/admin/workspaces/{WS_B}/keys", headers=BEARER).status_code == 200
    ws_keys = sorted(k for k in redis.counters if ":ws:" in k)
    assert len(ws_keys) == 2
    assert any(WS_A in k for k in ws_keys) and any(WS_B in k for k in ws_keys)


def test_non_workspace_admin_routes_keep_the_credential_bucket() -> None:
    redis = FakeAsyncRedis()
    client = _app(redis)
    client.post("/v1/admin/workspaces", headers=BEARER)
    (key,) = redis.counters
    assert key.startswith("abuse:admin:") and ":ws:" not in key


# --- store failure posture (HS4: writes stay fail-closed) -------------------


def test_a_store_failure_refuses_a_write() -> None:
    redis = FakeAsyncRedis(fail=True)
    client = _app(redis)
    for path in (
        "/v1/strategy/discovery-runs",
        "/v1/jobs/run/match/abc",
        f"/v1/admin/workspaces/{WS_A}/keys",
        "/v1/admin/workspaces",
    ):
        refused = client.post(path, headers=BEARER)
        assert refused.status_code == 429, path
        assert refused.json()["error"]["code"] == "RATE_LIMITED"


def test_a_store_failure_admits_an_admin_read(caplog) -> None:
    redis = FakeAsyncRedis(fail=True)
    client = _app(redis)
    with caplog.at_level("WARNING", logger="app.abuse_limit"):
        assert client.get("/v1/admin/usage", headers=BEARER).status_code == 200
        assert (
            client.get(
                f"/v1/admin/workspaces/{WS_A}/control-plane/state", headers=BEARER
            ).status_code
            == 200
        )
    assert any("fail-open for reads only" in r.getMessage() for r in caplog.records)


def test_a_store_timeout_is_a_store_failure(monkeypatch) -> None:
    """A hung Redis must not hold the request: the deadline turns it into the
    same failure posture (write refused, read admitted)."""
    monkeypatch.setattr(module, "_REDIS_ADMISSION_DEADLINE_SECONDS", 0.01)
    redis = FakeAsyncRedis(delay_seconds=0.5)
    client = _app(redis)
    assert client.post("/v1/strategy/discovery-runs", headers=BEARER).status_code == 429
    assert client.get("/v1/admin/usage", headers=BEARER).status_code == 200


# --- no blocking DB I/O, probe cached ---------------------------------------


def test_the_request_path_never_opens_a_database_session(monkeypatch) -> None:
    import app_shared.database as database

    def _boom(*_a, **_k):
        raise AssertionError("abuse limiter must not open a SQLAlchemy session")

    monkeypatch.setattr(database, "get_session", _boom)
    assert not hasattr(module, "get_session")
    redis = FakeAsyncRedis()
    client = _app(redis)
    assert client.post(f"/v1/admin/workspaces/{WS_A}/keys", headers=BEARER).status_code == 200
    assert client.get("/v1/admin/usage", headers=BEARER).status_code == 200


def test_the_configuration_probe_is_asked_once(monkeypatch) -> None:
    calls = []

    def _probe() -> bool:
        calls.append(1)
        return True

    redis = FakeAsyncRedis()
    monkeypatch.setattr(module, "_store_is_configured", _probe)
    monkeypatch.setattr(module, "get_async_admission_redis_client", lambda: redis)

    app = FastAPI()

    @app.post("/v1/strategy/discovery-runs")
    def discovery() -> dict:
        return {"ok": True}

    # Default (non-injected) factory path, the one the probe guards.
    app.add_middleware(AbuseLimitMiddleware, redis_factory=None)
    client = TestClient(app)
    for _ in range(5):
        assert client.post("/v1/strategy/discovery-runs", headers=BEARER).status_code == 200
    assert len(calls) == 1


def test_an_unconfigured_process_disables_the_limiter_rather_than_429ing(monkeypatch) -> None:
    """No `.env` in this process means no store to consult — not a refusal.

    Not a production hole: `app.main` calls `assert_production_safe()` at
    import, so a production API that cannot build `Settings` never serves.
    """
    monkeypatch.setattr(module, "_store_is_configured", lambda: False)

    app = FastAPI()

    @app.post("/v1/strategy/discovery-runs")
    def discovery() -> dict:
        return {"ok": True}

    app.add_middleware(AbuseLimitMiddleware)
    client = TestClient(app)
    assert client.post("/v1/strategy/discovery-runs", headers=BEARER).status_code == 200


def test_the_middleware_can_be_switched_off_entirely() -> None:
    redis = FakeAsyncRedis(fail=True)
    client = _app(redis, enabled=False)
    assert client.post("/v1/strategy/discovery-runs", headers=BEARER).status_code == 200
    assert redis.script_calls == 0
