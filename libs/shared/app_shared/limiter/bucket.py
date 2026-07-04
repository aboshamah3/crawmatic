"""Distributed token-bucket + concurrency-semaphore Redis primitives
(contracts/rate-limiter.md; FR-001..FR-009, FR-023).

Pure Redis logic — stdlib + an injected ``redis.Redis``-shaped client;
**no** Scrapy/Twisted/FastAPI import (mirrors
``app_shared/access/budget.py``). Both primitives are atomic
single-round-trip Lua ``EVAL``s (registered once via
``register_script``) so concurrent workers across the whole cluster can
never collectively exceed a limit (FR-004). ``now`` is always computed
from the **Redis server clock** (``redis.call('TIME')``) inside the
script — never a worker's wall clock — so acquisition is clock-skew-safe.
Every acquire path ``PEXPIRE``s its key so a crashed writer's state
always self-evicts (FR-005).

**Fail-closed** (FR-023 — deliberately the opposite of
``app_shared/access/budget.py``'s fail-open): any Redis error during an
acquire is treated as *not granted*. Release errors are logged and
swallowed (a TTL always reclaims the slot/token, D3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["AcquireResult", "acquire_slot", "acquire_token", "release_slot"]

#: Wait hint (seconds) returned on a Redis error during a token-bucket
#: acquire (fail-closed, FR-023) — always positive so callers always
#: back off rather than busy-loop.
_DEFAULT_BACKOFF_SECONDS = 5

# Marker comments identify each script to a test double's fake
# `register_script`; real Redis treats `--` as a plain Lua comment.
_TOKEN_BUCKET_LUA = """
-- rate-limiter.md token bucket (SPEC-11 T010)
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local ttl_ms = tonumber(ARGV[2])

local time_parts = redis.call('TIME')
local now = tonumber(time_parts[1]) + (tonumber(time_parts[2]) / 1000000)

local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])
if tokens == nil then
    tokens = capacity
    ts = now
end

local refill = (now - ts) * capacity / 60
tokens = math.min(capacity, tokens + refill)

if tokens >= 1 then
    tokens = tokens - 1
    redis.call('HMSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
    redis.call('PEXPIRE', key, ttl_ms)
    return {1, 0}
else
    redis.call('HMSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
    redis.call('PEXPIRE', key, ttl_ms)
    local wait_hint = math.ceil((1 - tokens) * 60 / capacity)
    return {0, wait_hint}
end
"""

_SEMAPHORE_ACQUIRE_LUA = """
-- rate-limiter.md semaphore acquire (SPEC-11 T011)
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local token = ARGV[2]
local slot_ttl_seconds = tonumber(ARGV[3])
local key_ttl_ms = tonumber(ARGV[4])

local time_parts = redis.call('TIME')
local now = tonumber(time_parts[1])

redis.call('ZREMRANGEBYSCORE', key, '-inf', now)

if redis.call('ZCARD', key) < limit then
    redis.call('ZADD', key, now + slot_ttl_seconds, token)
    redis.call('PEXPIRE', key, key_ttl_ms)
    return 1
else
    return 0
end
"""

#: Module-level script cache — "register the script once" (T010/T011).
#: The registered ``Script`` is content-addressed (SHA1 of the Lua
#: source) inside real Redis, so it is safe to register once against
#: the first client seen and then invoke against any later client via
#: an explicit ``client=`` override (see ``_call_script``).
_token_bucket_script: Any = None
_semaphore_acquire_script: Any = None


def _call_script(redis: Any, script_holder: str, source: str, *, keys: list[Any], args: list[Any]) -> Any:
    """Register ``source`` once (module-level cache keyed by
    ``script_holder``) and invoke it against ``redis`` (explicit
    ``client=`` override so a script registered against an earlier
    client still runs correctly against a different one, e.g. across
    tests)."""
    script = globals()[script_holder]
    if script is None:
        script = redis.register_script(source)
        globals()[script_holder] = script
    return script(keys=keys, args=args, client=redis)


@dataclass(frozen=True)
class AcquireResult:
    """Outcome of a token-bucket :func:`acquire_token` call."""

    granted: bool
    wait_hint_seconds: int


def acquire_token(redis: Any, *, key: str, capacity: int, ttl_seconds: int) -> AcquireResult:
    """Atomically grant/deny one token from the ``key`` bucket.

    ``capacity`` tokens/minute burst, refilled continuously on the
    **Redis server clock** (FR-004). Every path ``PEXPIRE``s the key to
    ``ttl_seconds`` (FR-005). Any Redis error -> fail-closed
    (``AcquireResult(granted=False, wait_hint_seconds=_DEFAULT_BACKOFF_SECONDS)``,
    FR-023).
    """
    try:
        granted, wait_hint = _call_script(
            redis,
            "_token_bucket_script",
            _TOKEN_BUCKET_LUA,
            keys=[key],
            args=[capacity, ttl_seconds * 1000],
        )
    except Exception:  # noqa: BLE001 - fail-closed on any Redis error (FR-023)
        return AcquireResult(granted=False, wait_hint_seconds=_DEFAULT_BACKOFF_SECONDS)
    return AcquireResult(granted=bool(granted), wait_hint_seconds=int(wait_hint))


def acquire_slot(
    redis: Any,
    *,
    key: str,
    limit: int,
    token: str,
    slot_ttl_seconds: int,
    key_ttl_seconds: int,
) -> bool:
    """Atomically grant/deny one concurrency slot in the ``key`` sorted set.

    Purges expired holders (``ZREMRANGEBYSCORE key -inf now``, server
    clock) before checking ``ZCARD key < limit`` — a crashed holder's
    slot is reclaimed on the very next acquire, no reaper needed
    (SC-004). Grant -> ``ZADD`` the caller's ``token`` scored
    ``now + slot_ttl_seconds`` and ``PEXPIRE`` the key to
    ``key_ttl_seconds``. Any Redis error -> fail-closed (``False``,
    FR-023).
    """
    try:
        granted = _call_script(
            redis,
            "_semaphore_acquire_script",
            _SEMAPHORE_ACQUIRE_LUA,
            keys=[key],
            args=[limit, token, slot_ttl_seconds, key_ttl_seconds * 1000],
        )
    except Exception:  # noqa: BLE001 - fail-closed on any Redis error (FR-023)
        return False
    return bool(granted)


def release_slot(redis: Any, *, key: str, token: str) -> None:
    """Release a previously-acquired concurrency slot (``ZREM key token``).

    Idempotent — releasing a missing/already-expired member is a no-op.
    Redis errors are logged and swallowed (the slot's TTL reclaims it
    regardless, D3) — release never raises.
    """
    try:
        redis.zrem(key, token)
    except Exception:  # noqa: BLE001 - logged + swallowed, TTL reclaims (D3)
        logger.warning("app_shared.limiter.bucket: release_slot failed for key=%s", key, exc_info=True)
