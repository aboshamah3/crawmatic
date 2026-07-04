"""``app_shared/limiter/bucket.py`` + ``limits.py`` unit tests (SPEC-11 US1
T008, `contracts/rate-limiter.md`, FR-001..FR-009, FR-023; SC-001, SC-004).

No Docker daemon in this build env (project memory) -- these tests MUST
actually run and pass, not skip. Per the project convention (mirrors
`tests/unit/test_access_budget.py`'s `_FakeRedis`/`_BrokenRedis` pair),
against a real ephemeral Redis when ``REDIS_URL`` is set, else a small
hand-rolled in-memory double (`fakeredis` is not a dependency anywhere in
this repo -- see ``pyproject.toml``/``uv.lock`` -- so a hand-rolled fake
is used per the task's own fallback guidance, not a new dependency).

The fake does not interpret Lua text; it recognizes the two production
scripts in ``bucket.py`` by their embedded marker comments (``-- SPEC-11
T010`` / ``-- SPEC-11 T011``) and runs a faithful **Python** transliteration
of the exact same algorithm against real Redis command semantics (hash
fields for the token bucket, a sorted set for the semaphore) -- this
exercises `acquire_token`/`acquire_slot`/`release_slot`'s real call
contract (register-once, explicit `client=` override, KEYS/ARGV shape)
end-to-end, while the Lua source itself is separately sanity-checked by
`test_lua_scripts_use_server_clock_and_pexpire_every_path` below (FR-004/
FR-005).
"""

from __future__ import annotations

import math
import os
import uuid
from typing import Any

import pytest

from app_shared.enums import AccessMethod
from app_shared.limiter import bucket as bucket_mod
from app_shared.limiter.bucket import AcquireResult, acquire_slot, acquire_token, release_slot
from app_shared.limiter.keys import rate_key, semaphore_key
from app_shared.limiter.limits import EffectiveLimits, resolve_limits


# --- test doubles ------------------------------------------------------------


def _script_method_name(script_src: str) -> str:
    if "SPEC-11 T010" in script_src:
        return "_run_token_bucket"
    if "SPEC-11 T011" in script_src:
        return "_run_semaphore_acquire"
    raise ValueError(f"unrecognized Lua script: {script_src[:60]!r}")


class _FakeScript:
    """Stand-in for `redis.commands.core.Script`: dispatches by *name* on
    whatever `client=` is passed at call time (never the instance that
    originally registered it) -- mirrors real redis-py's `Script.__call__`
    `client` override (confirmed against `redis==5.3.1` source), which is
    exactly what `bucket.py._call_script`'s module-level "register once"
    cache relies on to stay correct across multiple client instances."""

    def __init__(self, method_name: str) -> None:
        self._method_name = method_name

    def __call__(self, keys: Any = None, args: Any = None, client: Any = None) -> Any:
        if client is None:
            raise AssertionError("test double invoked without an explicit client= override")
        method = getattr(client, self._method_name)
        return method(list(keys or []), list(args or []))


class _FakeRedis:
    """Minimal in-memory stand-in for the subset of `redis.Redis` used by
    `bucket.py`: `register_script` + the two scripts' underlying data
    shapes (a hash for the token bucket, a sorted set for the semaphore)
    + `zrem` (release). No live Redis required. A controllable clock
    (`advance`) stands in for the Redis server's `TIME` (FR-004 -- the
    real Lua never reads the *worker's* wall clock either)."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self._now = now
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.pexpire_ms: dict[str, int] = {}

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def register_script(self, script_src: str) -> _FakeScript:
        return _FakeScript(_script_method_name(script_src))

    def zrem(self, key: str, token: str) -> None:
        zset = self.zsets.get(key)
        if zset is not None:
            zset.pop(token, None)

    # -- script bodies, transliterated 1:1 from bucket.py's Lua ----------

    def _run_token_bucket(self, keys: list[Any], args: list[Any]) -> list[int]:
        key = keys[0]
        capacity = float(args[0])
        ttl_ms = int(args[1])
        now = self._now

        bucket = self.hashes.get(key)
        if bucket is None:
            tokens = capacity
            ts = now
        else:
            tokens = float(bucket["tokens"])
            ts = float(bucket["ts"])

        refill = (now - ts) * capacity / 60
        tokens = min(capacity, tokens + refill)

        if tokens >= 1:
            tokens -= 1
            self.hashes[key] = {"tokens": str(tokens), "ts": str(now)}
            self.pexpire_ms[key] = ttl_ms
            return [1, 0]

        self.hashes[key] = {"tokens": str(tokens), "ts": str(now)}
        self.pexpire_ms[key] = ttl_ms
        wait_hint = math.ceil((1 - tokens) * 60 / capacity)
        return [0, wait_hint]

    def _run_semaphore_acquire(self, keys: list[Any], args: list[Any]) -> int:
        key = keys[0]
        limit = int(args[0])
        token = args[1]
        slot_ttl_seconds = float(args[2])
        key_ttl_ms = int(args[3])
        now = self._now

        zset = self.zsets.setdefault(key, {})
        for member in [m for m, score in zset.items() if score <= now]:
            del zset[member]

        if len(zset) < limit:
            zset[token] = now + slot_ttl_seconds
            self.pexpire_ms[key] = key_ttl_ms
            return 1
        return 0


class _BrokenRedis:
    """A client whose every script execution raises, simulating a Redis
    outage (register_script itself never talks to the network in real
    redis-py -- the outage surfaces at the actual EVAL/EVALSHA call, so
    that's where this double raises)."""

    def register_script(self, script_src: str) -> _FakeScript:
        return _FakeScript(_script_method_name(script_src))

    def zrem(self, key: str, token: str) -> None:
        raise ConnectionError("redis unavailable")

    def _run_token_bucket(self, keys: list[Any], args: list[Any]) -> Any:
        raise ConnectionError("redis unavailable")

    def _run_semaphore_acquire(self, keys: list[Any], args: list[Any]) -> Any:
        raise ConnectionError("redis unavailable")


def _redis_client() -> Any:
    """Real ephemeral Redis if `REDIS_URL` is set (per project convention),
    else the in-memory fake -- these tests must always actually run."""
    url = os.environ.get("REDIS_URL")
    if url:
        import redis as redis_pkg

        return redis_pkg.Redis.from_url(url)
    return _FakeRedis()


# --- acquire_token / token bucket ---------------------------------------------


def test_bucket_bounds_grants() -> None:
    redis = _redis_client()
    key = rate_key(uuid.uuid4(), "shop.example.com", AccessMethod.DIRECT_HTTP)

    results = [acquire_token(redis, key=key, capacity=5, ttl_seconds=180) for _ in range(20)]

    granted_count = sum(1 for r in results if r.granted)
    assert granted_count <= 5
    assert granted_count >= 1


def test_wait_hint_positive_on_denial() -> None:
    redis = _redis_client()
    key = rate_key(uuid.uuid4(), "shop.example.com", AccessMethod.DIRECT_HTTP)

    last: AcquireResult | None = None
    for _ in range(10):
        last = acquire_token(redis, key=key, capacity=3, ttl_seconds=180)

    assert last is not None
    assert last.granted is False
    assert last.wait_hint_seconds > 0


def test_workspace_namespacing() -> None:
    """Two workspace_ids on the same domain+access_method never share a
    bucket (FR-009, US1 AS5) -- exhausting one leaves the other untouched."""
    redis = _redis_client()
    domain = "shop.example.com"
    key_a = rate_key(uuid.uuid4(), domain, AccessMethod.DIRECT_HTTP)
    key_b = rate_key(uuid.uuid4(), domain, AccessMethod.DIRECT_HTTP)
    assert key_a != key_b

    for _ in range(5):
        acquire_token(redis, key=key_a, capacity=5, ttl_seconds=180)
    exhausted_a = acquire_token(redis, key=key_a, capacity=5, ttl_seconds=180)
    fresh_b = acquire_token(redis, key=key_b, capacity=5, ttl_seconds=180)

    assert exhausted_a.granted is False
    assert fresh_b.granted is True


def test_bucket_refills_over_time() -> None:
    """A denied bucket grants again once enough server-clock time has
    elapsed for a refill (bonus coverage of the refill math, FR-004)."""
    redis = _redis_client()
    if not isinstance(redis, _FakeRedis):
        pytest.skip("refill-timing test requires the controllable fake clock")
    key = rate_key(uuid.uuid4(), "shop.example.com", AccessMethod.DIRECT_HTTP)

    for _ in range(3):
        acquire_token(redis, key=key, capacity=3, ttl_seconds=180)
    denied = acquire_token(redis, key=key, capacity=3, ttl_seconds=180)
    assert denied.granted is False

    redis.advance(60)  # a full minute -> a full refill at capacity=3/min
    granted_after_refill = acquire_token(redis, key=key, capacity=3, ttl_seconds=180)
    assert granted_after_refill.granted is True


# --- acquire_slot / release_slot / semaphore ----------------------------------


def test_semaphore_cap() -> None:
    redis = _redis_client()
    key = semaphore_key(uuid.uuid4(), "shop.example.com", AccessMethod.DIRECT_HTTP)

    grants = [
        acquire_slot(
            redis, key=key, limit=2, token=f"tok-{i}", slot_ttl_seconds=600, key_ttl_seconds=720
        )
        for i in range(4)
    ]

    assert grants == [True, True, False, False]


def test_semaphore_ttl_reclaim() -> None:
    """A holder whose slot's score is now in the past is purged on the
    very next acquire -- no reaper process needed (SC-004)."""
    redis = _redis_client()
    if not isinstance(redis, _FakeRedis):
        pytest.skip("TTL-reclaim test requires the controllable fake clock")
    key = semaphore_key(uuid.uuid4(), "shop.example.com", AccessMethod.DIRECT_HTTP)

    first = acquire_slot(redis, key=key, limit=1, token="crashed-holder", slot_ttl_seconds=5, key_ttl_seconds=60)
    assert first is True

    second_before_expiry = acquire_slot(
        redis, key=key, limit=1, token="new-holder", slot_ttl_seconds=5, key_ttl_seconds=60
    )
    assert second_before_expiry is False  # crashed-holder's slot hasn't expired yet

    redis.advance(10)  # past crashed-holder's slot_ttl -- it never released
    reclaimed = acquire_slot(redis, key=key, limit=1, token="new-holder", slot_ttl_seconds=5, key_ttl_seconds=60)
    assert reclaimed is True


def test_semaphore_release_frees_a_slot() -> None:
    redis = _redis_client()
    key = semaphore_key(uuid.uuid4(), "shop.example.com", AccessMethod.DIRECT_HTTP)

    assert acquire_slot(redis, key=key, limit=1, token="tok-1", slot_ttl_seconds=600, key_ttl_seconds=720) is True
    assert acquire_slot(redis, key=key, limit=1, token="tok-2", slot_ttl_seconds=600, key_ttl_seconds=720) is False

    release_slot(redis, key=key, token="tok-1")

    assert acquire_slot(redis, key=key, limit=1, token="tok-2", slot_ttl_seconds=600, key_ttl_seconds=720) is True


def test_release_slot_swallows_redis_errors() -> None:
    """Release never raises even when Redis is down (D3)."""
    redis = _BrokenRedis()
    release_slot(redis, key="semaphore:ws:shop.example.com:DIRECT_HTTP", token="tok")  # must not raise


# --- fail-closed on Redis error (FR-023) --------------------------------------


def test_fail_closed_on_redis_error() -> None:
    redis = _BrokenRedis()

    bucket_result = acquire_token(
        redis, key="rate:ws:shop.example.com:DIRECT_HTTP", capacity=10, ttl_seconds=180
    )
    assert bucket_result == AcquireResult(granted=False, wait_hint_seconds=bucket_result.wait_hint_seconds)
    assert bucket_result.granted is False
    assert bucket_result.wait_hint_seconds > 0

    slot_granted = acquire_slot(
        redis,
        key="semaphore:ws:shop.example.com:DIRECT_HTTP",
        limit=4,
        token="tok",
        slot_ttl_seconds=600,
        key_ttl_seconds=720,
    )
    assert slot_granted is False


# --- Lua source sanity checks (FR-004/FR-005 -- exercised functionally above
#     via the fake, and textually here since the fake never parses Lua) ------


def test_lua_scripts_use_server_clock_and_pexpire_every_path() -> None:
    token_bucket_src = bucket_mod._TOKEN_BUCKET_LUA
    semaphore_src = bucket_mod._SEMAPHORE_ACQUIRE_LUA

    assert "redis.call('TIME')" in token_bucket_src
    assert "redis.call('TIME')" in semaphore_src
    # Every path of the token bucket PEXPIREs (grant branch and deny branch).
    assert token_bucket_src.count("PEXPIRE") >= 2


# --- resolve_limits (limits.py, T009) -----------------------------------------


class _FakeSettings:
    RATE_LIMIT_DEFAULT_PER_MINUTE = 60
    RATE_LIMIT_DEFAULT_CONCURRENCY = 4


class _FakeDomainRule:
    def __init__(
        self,
        *,
        max_requests_per_minute: int,
        max_concurrent_requests: int,
        cooldown_seconds: int,
        enabled: bool = True,
    ) -> None:
        self.max_requests_per_minute = max_requests_per_minute
        self.max_concurrent_requests = max_concurrent_requests
        self.cooldown_seconds = cooldown_seconds
        self.enabled = enabled


class _FakeAccessPolicy:
    def __init__(self, *, max_requests_per_minute: int | None) -> None:
        self.max_requests_per_minute = max_requests_per_minute


def test_resolve_limits_domain_rule_overrides_everything() -> None:
    domain_rule = _FakeDomainRule(max_requests_per_minute=10, max_concurrent_requests=2, cooldown_seconds=30)
    policy = _FakeAccessPolicy(max_requests_per_minute=999)

    limits = resolve_limits(domain_rule=domain_rule, access_policy=policy, settings=_FakeSettings())

    assert limits == EffectiveLimits(per_minute=10, concurrency=2, cooldown_seconds=30)


def test_resolve_limits_falls_back_to_access_policy() -> None:
    policy = _FakeAccessPolicy(max_requests_per_minute=42)

    limits = resolve_limits(domain_rule=None, access_policy=policy, settings=_FakeSettings())

    assert limits.per_minute == 42
    assert limits.concurrency == _FakeSettings.RATE_LIMIT_DEFAULT_CONCURRENCY
    assert limits.cooldown_seconds == 0


def test_resolve_limits_falls_back_to_settings_defaults() -> None:
    limits = resolve_limits(domain_rule=None, access_policy=None, settings=_FakeSettings())

    assert limits.per_minute == _FakeSettings.RATE_LIMIT_DEFAULT_PER_MINUTE
    assert limits.concurrency == _FakeSettings.RATE_LIMIT_DEFAULT_CONCURRENCY
    assert limits.cooldown_seconds == 0


def test_resolve_limits_floors_to_at_least_one() -> None:
    domain_rule = _FakeDomainRule(max_requests_per_minute=0, max_concurrent_requests=0, cooldown_seconds=-5)

    limits = resolve_limits(domain_rule=domain_rule, access_policy=None, settings=_FakeSettings())

    assert limits.per_minute >= 1
    assert limits.concurrency >= 1
    assert limits.cooldown_seconds == 0


def test_resolve_limits_disabled_domain_rule_is_ignored() -> None:
    domain_rule = _FakeDomainRule(
        max_requests_per_minute=10, max_concurrent_requests=2, cooldown_seconds=30, enabled=False
    )
    policy = _FakeAccessPolicy(max_requests_per_minute=42)

    limits = resolve_limits(domain_rule=domain_rule, access_policy=policy, settings=_FakeSettings())

    assert limits.per_minute == 42
    assert limits.cooldown_seconds == 0
