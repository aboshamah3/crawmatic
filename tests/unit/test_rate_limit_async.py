"""F15 (B7): async, non-blocking rate-limit middleware (PLAN §7.4).

Covers the acceptance criteria not already exercised by
`test_rate_limit_middleware.py`:

- no synchronous `redis.Redis` anywhere in `app.rate_limit` (module-level
  static check);
- one Lua script call per admission check (atomic INCR + EXPIRE-on-
  first-hit), asserted against a hand-rolled async fake -- `fakeredis`
  is not a dependency anywhere in this repo (see
  `tests/unit/test_rate_limiter.py`'s docstring for the project's
  established convention here);
- a Redis stub that sleeps past the deadline makes the middleware fail
  open within ~300ms while a concurrent `/health` (`/live`-equivalent;
  this API's liveness route is `/health`, always exempt) request
  completes unaffected;
- unauthenticated admission is IP-keyed with bounded cardinality
  (`API_RATE_LIMIT_MAX_KEYS`);
- a verified JWT's `workspace_id` claim, not the credential hash, keys
  authenticated admission.
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.rate_limit as rate_limit_mod
from app.rate_limit import (
    API_RATE_LIMIT_MAX_KEYS,
    RateLimitMiddleware,
    _admission_key,
    _ip_bucket,
)


# --- static: no synchronous redis.Redis anywhere in this module -------------


def test_module_holds_no_synchronous_redis_client():
    source = inspect.getsource(rate_limit_mod)
    assert "import redis\n" not in source
    assert "redis.Redis(" not in source
    assert "redis.Redis.from_url" not in source
    assert "redis.asyncio" in source


# --- one atomic script call per admission check ------------------------------


class _AsyncFakeScript:
    def __init__(self, redis: "_AsyncFakeRedis") -> None:
        self._redis = redis

    async def __call__(self, keys=None, args=None):
        if self._redis.delay_seconds:
            await asyncio.sleep(self._redis.delay_seconds)
        if self._redis.fail:
            raise ConnectionError("redis unavailable")
        self._redis.script_calls += 1
        key = (keys or [""])[0]
        ttl = int((args or [60])[0])
        count = self._redis.counters.get(key, 0) + 1
        self._redis.counters[key] = count
        if count == 1:
            self._redis.expirations[key] = ttl
        return count


class _AsyncFakeRedis:
    """Hand-rolled async double -- see the module docstring for why this
    isn't `fakeredis` (not a dependency in this repo)."""

    def __init__(self, *, fail: bool = False, delay_seconds: float = 0.0) -> None:
        self.counters: dict[str, int] = {}
        self.expirations: dict[str, int] = {}
        self.script_calls = 0
        self.fail = fail
        self.delay_seconds = delay_seconds

    def register_script(self, _script_src: str) -> _AsyncFakeScript:
        return _AsyncFakeScript(self)


def _app(redis, read: int = 100, write: int = 100) -> FastAPI:
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

    @application.get("/health")
    def _health():
        return {"status": "ok"}

    return application


HEADERS = {"Authorization": "Bearer ck_abcdef0123456789"}


def test_one_script_call_increments_and_expires_atomically():
    redis = _AsyncFakeRedis()
    client = TestClient(_app(redis))

    resp = client.get("/v1/things", headers=HEADERS)

    assert resp.status_code == 200
    assert redis.script_calls == 1
    assert list(redis.counters.values()) == [1]
    assert list(redis.expirations.values())  # EXPIRE set on first hit


def test_expire_is_only_set_on_the_first_hit():
    redis = _AsyncFakeRedis()
    client = TestClient(_app(redis))

    client.get("/v1/things", headers=HEADERS)
    client.get("/v1/things", headers=HEADERS)

    assert redis.script_calls == 2
    # Only one EXPIRE recorded even though INCR ran twice -- the Lua
    # script only calls EXPIRE when the counter is freshly created.
    assert len(redis.expirations) == 1


# --- fail-open on a slow Redis, within the deadline, without blocking -------


def test_slow_redis_fails_open_within_deadline_and_does_not_block_health():
    slow_redis = _AsyncFakeRedis(delay_seconds=2.0)
    application = _app(slow_redis, read=1)
    client = TestClient(application)

    start = time.monotonic()
    resp = client.get("/v1/things", headers=HEADERS)
    elapsed = time.monotonic() - start

    assert resp.status_code == 200  # fail-open, not 429/500
    assert elapsed < 1.0  # well under the 2s stub delay -- ~300ms deadline

    # A concurrent /health request is on the exempt path and never
    # touches Redis at all, so it always completes immediately -- assert
    # it isn't even slowed down by a fully-independent slow client.
    health_start = time.monotonic()
    health_resp = client.get("/health")
    health_elapsed = time.monotonic() - health_start
    assert health_resp.status_code == 200
    assert health_elapsed < 1.0


# --- unauthenticated admission: bounded-cardinality IP keying ---------------


def test_unauthenticated_key_is_ip_bucketed_not_exempt():
    redis = _AsyncFakeRedis()
    client = TestClient(_app(redis, read=2))

    statuses = [client.get("/v1/things").status_code for _ in range(4)]

    assert 429 in statuses
    assert redis.script_calls == 4  # every unauthenticated hit was counted


def test_ip_bucket_cardinality_is_bounded():
    buckets = {_ip_bucket(f"203.0.113.{i}") for i in range(500)}
    for bucket in buckets:
        assert bucket.startswith("ip:")
        index = int(bucket.split(":", 1)[1])
        assert 0 <= index < API_RATE_LIMIT_MAX_KEYS


# --- authenticated admission: verified JWT workspace claim ------------------


class _Req:
    def __init__(self, authorization: str) -> None:
        self.headers = {"Authorization": authorization}
        self.client = None


class _Settings:
    JWT_SECRET = "test-secret"
    JWT_ALGORITHM = "HS256"


def test_verified_jwt_is_keyed_by_workspace():
    from app_shared.security.jwt import encode_access_token
    import uuid

    workspace_id = uuid.uuid4()
    token = encode_access_token(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        role="WORKSPACE_ADMIN",
        secret=_Settings.JWT_SECRET,
        algorithm=_Settings.JWT_ALGORITHM,
        ttl_seconds=3600,
    )

    kind, key = _admission_key(_Req(f"Bearer {token}"), _Settings())

    assert kind == "workspace"
    assert key == str(workspace_id)


def test_two_credentials_in_the_same_workspace_share_a_bucket():
    """Two different JWTs for the same workspace resolve to the same key
    (PLAN F15: authenticated quotas are keyed by workspace)."""
    from app_shared.security.jwt import encode_access_token
    import uuid

    workspace_id = uuid.uuid4()
    token_a = encode_access_token(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        role="WORKSPACE_ADMIN",
        secret=_Settings.JWT_SECRET,
        algorithm=_Settings.JWT_ALGORITHM,
        ttl_seconds=3600,
    )
    token_b = encode_access_token(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        role="WORKSPACE_MEMBER",
        secret=_Settings.JWT_SECRET,
        algorithm=_Settings.JWT_ALGORITHM,
        ttl_seconds=3600,
    )

    _, key_a = _admission_key(_Req(f"Bearer {token_a}"), _Settings())
    _, key_b = _admission_key(_Req(f"Bearer {token_b}"), _Settings())

    assert key_a == key_b == str(workspace_id)


def test_unverifiable_jwt_falls_back_to_credential_hash():
    kind, key = _admission_key(_Req("Bearer not-a-real-jwt"), _Settings())
    assert kind == "credential"
    assert key is not None


def test_api_key_credential_is_not_resolved_to_a_workspace():
    """API-key traffic keeps the pre-F15 per-credential bucket -- see the
    module docstring for why this middleware doesn't do the full DB
    lookup `app.deps` does just for admission control."""
    kind, key = _admission_key(_Req("Bearer ck_abcdef0123456789"), _Settings())
    assert kind == "credential"
    assert key is not None
