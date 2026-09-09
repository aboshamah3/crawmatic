"""Per-key rate limiting (PLAN §7.4, risk P5; F15 async rework, B7)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.rate_limit import RateLimitMiddleware, is_write, rate_limit_identity


class FakeAsyncRedis:
    """Minimal async stand-in for `redis.asyncio.Redis.register_script`.

    `fakeredis` is not a dependency anywhere in this repo (see
    `tests/unit/test_rate_limiter.py`'s docstring) -- this hand-rolled
    double mirrors that project convention for the async client.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.counters: dict[str, int] = {}
        self.fail = fail
        self.script_calls = 0

    def register_script(self, _script_src: str):
        redis = self

        class _Script:
            async def __call__(self, keys=None, args=None):
                if redis.fail:
                    raise ConnectionError("redis down")
                redis.script_calls += 1
                key = (keys or [""])[0]
                count = redis.counters.get(key, 0) + 1
                redis.counters[key] = count
                return count

        return _Script()


def _app(redis, read: int = 3, write: int = 2) -> FastAPI:
    application = FastAPI()
    application.add_middleware(
        RateLimitMiddleware,
        redis_factory=lambda: redis,
        read_per_minute=read,
        write_per_minute=write,
        enabled=True,
    )

    @application.get("/v1/things")
    def _read():
        return {"ok": True}

    @application.post("/v1/things")
    def _write():
        return {"ok": True}

    @application.get("/health")
    def _health():
        return {"status": "ok"}

    return application


HEADERS = {"Authorization": "Bearer ck_abcdef0123456789"}


def test_reads_under_the_limit_pass():
    client = TestClient(_app(FakeAsyncRedis()))
    for _ in range(3):
        assert client.get("/v1/things", headers=HEADERS).status_code == 200


def test_read_over_the_limit_is_429_with_retry_after():
    client = TestClient(_app(FakeAsyncRedis()))
    for _ in range(3):
        client.get("/v1/things", headers=HEADERS)
    resp = client.get("/v1/things", headers=HEADERS)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMITED"
    assert int(resp.headers["Retry-After"]) > 0


def test_writes_have_their_own_lower_budget():
    client = TestClient(_app(FakeAsyncRedis()))
    assert client.post("/v1/things", headers=HEADERS).status_code == 200
    assert client.post("/v1/things", headers=HEADERS).status_code == 200
    assert client.post("/v1/things", headers=HEADERS).status_code == 429


def test_reads_and_writes_do_not_share_a_budget():
    client = TestClient(_app(FakeAsyncRedis()))
    for _ in range(3):
        client.get("/v1/things", headers=HEADERS)
    assert client.post("/v1/things", headers=HEADERS).status_code == 200


def test_two_keys_have_independent_budgets():
    client = TestClient(_app(FakeAsyncRedis()))
    other = {"Authorization": "Bearer ck_zzzzzzzzzzzzzzzz"}
    for _ in range(3):
        client.get("/v1/things", headers=HEADERS)
    assert client.get("/v1/things", headers=other).status_code == 200


def test_health_is_never_limited():
    client = TestClient(_app(FakeAsyncRedis(), read=1))
    for _ in range(5):
        assert client.get("/health").status_code == 200


def test_admin_surface_is_never_limited():
    """`/v1/admin/*` is gated by the shared service-token secret, not a
    customer credential -- it must never be throttled by the tenant
    limiter (PLAN §7.4). Otherwise a billing backfill or a busy month
    429s the SaaS metering feed -- a silent revenue-loss path."""
    application = _app(FakeAsyncRedis(), read=1)

    @application.get("/v1/admin/usage")
    def _admin_read():
        return {"ok": True}

    client = TestClient(application)
    for _ in range(2):
        resp = client.get("/v1/admin/usage", headers=HEADERS)
        assert resp.status_code != 429


def test_unauthenticated_requests_are_ip_admission_controlled():
    """F15 (B7): unauthenticated traffic is no longer exempt outright --
    it is still about to 401, but is now admission-controlled by a
    bounded-cardinality client-IP bucket rather than given a free pass
    (PLAN F15). TestClient's default test peer IP is stable across
    requests in the same client, so this behaves like one caller."""
    client = TestClient(_app(FakeAsyncRedis(), read=2))
    statuses = [client.get("/v1/things").status_code for _ in range(4)]
    assert statuses.count(429) >= 1
    assert all(s in (200, 429) for s in statuses)


def test_redis_outage_fails_open():
    client = TestClient(_app(FakeAsyncRedis(fail=True), read=1))
    for _ in range(5):
        assert client.get("/v1/things", headers=HEADERS).status_code == 200


def test_identity_never_contains_the_raw_secret():
    class _Req:
        headers = {"Authorization": "Bearer ck_supersecretvalue"}

    identity = rate_limit_identity(_Req())
    assert identity is not None
    assert "supersecretvalue" not in identity


def test_write_classification():
    assert is_write("POST") and is_write("PATCH") and is_write("PUT")
    assert is_write("DELETE")
    assert not is_write("GET")
    assert not is_write("HEAD")
    assert not is_write("OPTIONS")
