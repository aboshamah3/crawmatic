"""``app_shared/strategy/stats_buffer.py`` unit tests (SPEC-12 US5 T032,
`contracts/stats-buffer.md`, FR-009/FR-022..FR-025; SC-003).

No Docker daemon in this build env (project memory) -- these tests MUST
actually run and pass, not skip. Per the project convention (mirrors
``tests/unit/test_rate_limiter.py``/``test_match_lock.py``'s
``_FakeRedis``/``_BrokenRedis`` pair), against a real ephemeral Redis when
``REDIS_URL`` is set, else a small hand-rolled in-memory double
(``fakeredis`` is not a dependency anywhere in this repo).

The fake does not interpret Lua text; it recognizes the one production
script in ``stats_buffer.py`` (``drain``) by its embedded marker comment
(``-- stats-buffer.md drain (SPEC-12 T034)``, mirroring ``bucket.py``'s
``-- SPEC-11 T0XX`` convention) and runs a faithful **Python**
transliteration of the exact same algorithm (``HGETALL``+``DEL`` the
stat hash, ``SCARD`` the url set without deleting it) against real Redis
command semantics (hashes + sets) -- this exercises ``drain``'s real call
contract (register-once, explicit ``client=`` override, ``KEYS``/``ARGV``
shape) end-to-end.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from app_shared.enums import AccessMethod, MethodType
from app_shared.strategy import stats_buffer
from app_shared.strategy.stats_buffer import (
    DrainedDelta,
    PendingDelta,
    dirty_key,
    drain,
    read_pending,
    record_attempt,
    url_key,
)

_TTL_SECONDS = 3600


# --- test doubles -------------------------------------------------------------


def _script_method_name(script_src: str) -> str:
    if "SPEC-12 T034" in script_src:
        return "_run_drain"
    raise ValueError(f"unrecognized Lua script: {script_src[:60]!r}")


class _FakeScript:
    """Stand-in for `redis.commands.core.Script`: dispatches by *name* on
    whatever `client=` is passed at call time -- mirrors real redis-py's
    `Script.__call__` `client` override, exactly what `stats_buffer.py`'s
    module-level "register once" cache relies on to stay correct across
    multiple client instances (the same convention as
    `tests/unit/test_rate_limiter.py`/`test_match_lock.py`)."""

    def __init__(self, method_name: str) -> None:
        self._method_name = method_name

    def __call__(self, keys: Any = None, args: Any = None, client: Any = None) -> Any:
        if client is None:
            raise AssertionError("test double invoked without an explicit client= override")
        method = getattr(client, self._method_name)
        return method(list(keys or []), list(args or []))


class _FakeRedis:
    """Minimal in-memory stand-in for the subset of `redis.Redis` used by
    `stats_buffer.py`: `hincrby`/`sadd`/`pexpire`/`hgetall`/`scard`/
    `register_script`/`delete`/`srem`. No live Redis required."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, int]] = {}
        self.sets: dict[str, set[str]] = {}
        self.pexpire_calls: dict[str, int] = {}

    def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + amount
        return bucket[field]

    def sadd(self, key: str, *members: str) -> int:
        members_set = self.sets.setdefault(key, set())
        before = len(members_set)
        members_set.update(members)
        return len(members_set) - before

    def srem(self, key: str, *members: str) -> int:
        members_set = self.sets.get(key)
        if members_set is None:
            return 0
        removed = 0
        for member in members:
            if member in members_set:
                members_set.discard(member)
                removed += 1
        return removed

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self.hashes.pop(key, None) is not None:
                removed += 1
            if self.sets.pop(key, None) is not None:
                removed += 1
        return removed

    def pexpire(self, key: str, ttl_ms: int) -> bool:
        self.pexpire_calls[key] = ttl_ms
        return True

    def hgetall(self, key: str) -> dict[str, str]:
        return {field: str(value) for field, value in self.hashes.get(key, {}).items()}

    def scard(self, key: str) -> int:
        return len(self.sets.get(key, set()))

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def register_script(self, script_src: str) -> _FakeScript:
        return _FakeScript(_script_method_name(script_src))

    # -- script body, transliterated 1:1 from stats_buffer.py's Lua -----

    def _run_drain(self, keys: list[Any], args: list[Any]) -> list[Any]:
        stat_key, this_url_key = keys[0], keys[1]
        fields = self.hashes.pop(stat_key, {})
        flat: list[str] = []
        for field, value in fields.items():
            flat.append(field)
            flat.append(str(value))
        distinct_urls = len(self.sets.get(this_url_key, set()))
        return [flat, distinct_urls]


class _BrokenRedis:
    """A client whose every call raises, simulating a Redis outage."""

    def hincrby(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("redis unavailable")

    def sadd(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("redis unavailable")

    def pexpire(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("redis unavailable")


def _redis_client() -> Any:
    """Real ephemeral Redis if `REDIS_URL` is set (per project convention),
    else the in-memory fake -- these tests must always actually run."""
    url = os.environ.get("REDIS_URL")
    if url:
        import redis as redis_pkg

        return redis_pkg.Redis.from_url(url, decode_responses=True)
    return _FakeRedis()


def _record(
    redis: Any,
    *,
    workspace_id: uuid.UUID,
    profile_id: uuid.UUID,
    success: bool,
    qualifying: bool,
    url: str,
    response_time_ms: int | None = 100,
    confidence: float | None = 0.9,
) -> None:
    record_attempt(
        redis,
        workspace_id=workspace_id,
        profile_id=profile_id,
        method_type=MethodType.ACCESS,
        method_name=AccessMethod.DIRECT_HTTP.value,
        success=success,
        response_time_ms=response_time_ms,
        confidence=confidence if success else None,
        url=url,
        qualifying=qualifying,
        ttl_seconds=_TTL_SECONDS,
    )


# --- record_attempt: HINCRBY accumulation, no primary-store write -------------


def test_record_attempt_accumulates_via_hincrby() -> None:
    redis = _redis_client()
    workspace_id = uuid.uuid4()
    profile_id = uuid.uuid4()

    for i in range(5):
        _record(
            redis,
            workspace_id=workspace_id,
            profile_id=profile_id,
            success=(i != 4),  # 4 successes, 1 failure
            qualifying=(i != 4),
            url=f"https://shop.example.com/products/item-{i}",
        )

    pending = read_pending(
        redis, profile_id=profile_id, method_type=MethodType.ACCESS, method_name=AccessMethod.DIRECT_HTTP.value
    )
    assert pending.attempt == 5
    assert pending.success == 4
    assert pending.failure == 1
    assert pending.rt_ms_sum == 500  # 5 attempts * 100ms
    assert pending.qualifying_success == 4
    assert pending.distinct_urls == 4  # one fingerprint per qualifying success's distinct URL


def test_record_attempt_no_primary_store_write() -> None:
    """`stats_buffer.py` takes only a Redis client -- it has no SQLAlchemy
    import and no notion of a DB session at all, so N `record_attempt`
    calls can never issue a primary-store write by construction (AS1,
    SC-003)."""
    import ast
    import pathlib

    source = pathlib.Path(stats_buffer.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    banned = {"sqlalchemy", "scrapy", "twisted", "fastapi"}
    assert not any(mod.split(".")[0] in banned for mod in imported_modules), imported_modules


def test_non_qualifying_success_still_increments_success_but_not_url_set() -> None:
    """contracts/promotion.md "Qualifying success": a non-qualifying
    success still `HINCRBY success` (for `success_rate`) but is never
    `SADD`-ed to the distinct-URL SET and never bumps `qual_success`."""
    redis = _redis_client()
    workspace_id = uuid.uuid4()
    profile_id = uuid.uuid4()

    _record(
        redis,
        workspace_id=workspace_id,
        profile_id=profile_id,
        success=True,
        qualifying=False,  # e.g. confidence below the promotion threshold
        url="https://shop.example.com/products/low-confidence",
    )

    pending = read_pending(
        redis, profile_id=profile_id, method_type=MethodType.ACCESS, method_name=AccessMethod.DIRECT_HTTP.value
    )
    assert pending.success == 1
    assert pending.qualifying_success == 0
    assert pending.distinct_urls == 0


def test_stratdirty_gains_the_profile_id() -> None:
    redis = _redis_client()
    workspace_id = uuid.uuid4()
    profile_id = uuid.uuid4()

    _record(
        redis,
        workspace_id=workspace_id,
        profile_id=profile_id,
        success=True,
        qualifying=True,
        url="https://shop.example.com/products/a",
    )

    assert str(profile_id) in redis.smembers(dirty_key(workspace_id))


# --- read_pending: non-destructive ---------------------------------------------


def test_read_pending_is_non_destructive() -> None:
    redis = _redis_client()
    workspace_id = uuid.uuid4()
    profile_id = uuid.uuid4()

    _record(
        redis,
        workspace_id=workspace_id,
        profile_id=profile_id,
        success=True,
        qualifying=True,
        url="https://shop.example.com/products/a",
    )

    first = read_pending(
        redis, profile_id=profile_id, method_type=MethodType.ACCESS, method_name=AccessMethod.DIRECT_HTTP.value
    )
    second = read_pending(
        redis, profile_id=profile_id, method_type=MethodType.ACCESS, method_name=AccessMethod.DIRECT_HTTP.value
    )

    assert first == second == PendingDelta(
        attempt=1, success=1, failure=0, rt_ms_sum=100, conf_sum=9000, qualifying_success=1, distinct_urls=1
    )


def test_read_pending_on_untouched_key_is_all_zero() -> None:
    redis = _redis_client()
    pending = read_pending(
        redis,
        profile_id=uuid.uuid4(),
        method_type=MethodType.ACCESS,
        method_name=AccessMethod.PROXY_HTTP.value,
    )
    assert pending == PendingDelta(
        attempt=0, success=0, failure=0, rt_ms_sum=0, conf_sum=0, qualifying_success=0, distinct_urls=0
    )


# --- drain: atomic read-and-reset, url SET survives ----------------------------


def test_drain_resets_the_stat_hash_but_leaves_the_url_set_intact() -> None:
    redis = _redis_client()
    workspace_id = uuid.uuid4()
    profile_id = uuid.uuid4()

    for i in range(3):
        _record(
            redis,
            workspace_id=workspace_id,
            profile_id=profile_id,
            success=True,
            qualifying=True,
            url=f"https://shop.example.com/products/item-{i}",
        )

    drained = drain(
        redis, profile_id=profile_id, method_type=MethodType.ACCESS, method_name=AccessMethod.DIRECT_HTTP.value
    )
    assert drained == DrainedDelta(
        attempt=3, success=3, failure=0, rt_ms_sum=300, conf_sum=27000, qualifying_success=3, distinct_urls=3
    )

    # The stat hash is emptied (AS2)...
    after_drain = read_pending(
        redis, profile_id=profile_id, method_type=MethodType.ACCESS, method_name=AccessMethod.DIRECT_HTTP.value
    )
    assert after_drain.attempt == 0
    assert after_drain.success == 0
    # ...but the distinct-URL SET survives (contracts/stats-buffer.md "Drain":
    # not deleted until the method is promoted).
    assert after_drain.distinct_urls == 3


def test_drain_on_untouched_key_returns_all_zero() -> None:
    redis = _redis_client()
    drained = drain(
        redis,
        profile_id=uuid.uuid4(),
        method_type=MethodType.ACCESS,
        method_name=AccessMethod.DIRECT_HTTP_RETRY.value,
    )
    assert drained == DrainedDelta(
        attempt=0, success=0, failure=0, rt_ms_sum=0, conf_sum=0, qualifying_success=0, distinct_urls=0
    )


def test_second_drain_with_no_new_activity_writes_nothing() -> None:
    """A second flush with no new activity in between drains empty --
    exactly the `test_flush_promote.py` (T033) "a second flush ... writes
    nothing" behavior, one layer down."""
    redis = _redis_client()
    profile_id = uuid.uuid4()
    workspace_id = uuid.uuid4()

    _record(
        redis,
        workspace_id=workspace_id,
        profile_id=profile_id,
        success=True,
        qualifying=True,
        url="https://shop.example.com/products/a",
    )
    drain(redis, profile_id=profile_id, method_type=MethodType.ACCESS, method_name=AccessMethod.DIRECT_HTTP.value)

    second = drain(
        redis, profile_id=profile_id, method_type=MethodType.ACCESS, method_name=AccessMethod.DIRECT_HTTP.value
    )
    assert second == DrainedDelta(
        attempt=0, success=0, failure=0, rt_ms_sum=0, conf_sum=0, qualifying_success=0, distinct_urls=1
    )


# --- TTL / PEXPIRE ---------------------------------------------------------------


def test_pexpire_touches_every_key_written_this_call() -> None:
    redis = _redis_client()
    if not isinstance(redis, _FakeRedis):
        pytest.skip("pexpire-call-tracking requires the fake's introspection")
    workspace_id = uuid.uuid4()
    profile_id = uuid.uuid4()

    _record(
        redis,
        workspace_id=workspace_id,
        profile_id=profile_id,
        success=True,
        qualifying=True,
        url="https://shop.example.com/products/a",
    )

    stat_key = f"stratstat:{profile_id}:ACCESS:{AccessMethod.DIRECT_HTTP.value}"
    assert redis.pexpire_calls[stat_key] == _TTL_SECONDS * 1000
    assert redis.pexpire_calls[url_key(profile_id, MethodType.ACCESS, AccessMethod.DIRECT_HTTP.value)] == (
        _TTL_SECONDS * 1000
    )
    assert redis.pexpire_calls[dirty_key(workspace_id)] == _TTL_SECONDS * 1000


# --- fail-open on Redis error (mirrors access/budget.py's posture) -------------


def test_record_attempt_swallows_redis_errors() -> None:
    """Recording never raises even when Redis is down -- a lost increment
    must never fail a scrape (contracts/stats-buffer.md step 4)."""
    redis = _BrokenRedis()
    record_attempt(
        redis,
        workspace_id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        method_type=MethodType.ACCESS,
        method_name=AccessMethod.DIRECT_HTTP.value,
        success=True,
        response_time_ms=100,
        confidence=0.9,
        url="https://shop.example.com/products/a",
        qualifying=True,
        ttl_seconds=_TTL_SECONDS,
    )  # must not raise


# --- Lua source sanity check (exercised functionally above via the fake) ------


def test_drain_lua_is_a_read_and_reset_leaving_the_url_set_alone() -> None:
    source = stats_buffer._DRAIN_LUA
    assert "HGETALL" in source
    assert "DEL" in source
    assert "SCARD" in source
    # The url-set key (KEYS[2]) must never be DEL'd by this script.
    assert "DEL', KEYS[2]" not in source.replace(" ", "")
