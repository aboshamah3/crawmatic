"""``app_shared/limiter/locks.py`` unit tests (SPEC-11 US2 T015,
`contracts/match-lock.md`, FR-010..FR-014, FR-023; SC-002).

No Docker daemon in this build env (project memory) -- these tests MUST
actually run and pass, not skip. Per the project convention (mirrors
`tests/unit/test_rate_limiter.py`'s `_FakeRedis`/`_BrokenRedis` pair and
`tests/unit/test_access_budget.py`'s), a small hand-rolled in-memory
double stands in for `redis.Redis` (no live Redis required; `fakeredis`
is not a dependency anywhere in this repo).

The fake recognizes the one production Lua script in `locks.py` by its
embedded marker comment (`-- SPEC-11 T019`, mirroring `bucket.py`'s
`-- SPEC-11 T0XX` convention) and runs a faithful **Python**
transliteration of the exact same compare-and-delete algorithm -- this
exercises `acquire_match_lock`/`release_match_lock`'s real call contract
(register-once, explicit `client=` override, `SET ... NX PX` semantics)
end-to-end.
"""

from __future__ import annotations

import uuid
from typing import Any

from app_shared.limiter import locks as locks_mod
from app_shared.limiter.keys import match_lock_key
from app_shared.limiter.locks import acquire_match_lock, new_fencing_token, release_match_lock


def _script_method_name(script_src: str) -> str:
    if "SPEC-11 T019" in script_src:
        return "_run_release"
    raise ValueError(f"unrecognized Lua script: {script_src[:60]!r}")


class _FakeScript:
    """Stand-in for `redis.commands.core.Script`: dispatches by *name* on
    whatever `client=` is passed at call time -- mirrors real redis-py's
    `Script.__call__` `client` override, exactly what `locks.py`'s
    module-level "register once" cache relies on to stay correct across
    multiple client instances (see `tests/unit/test_rate_limiter.py`'s
    identical convention)."""

    def __init__(self, method_name: str) -> None:
        self._method_name = method_name

    def __call__(self, keys: Any = None, args: Any = None, client: Any = None) -> Any:
        if client is None:
            raise AssertionError("test double invoked without an explicit client= override")
        method = getattr(client, self._method_name)
        return method(list(keys or []), list(args or []))


class _FakeRedis:
    """Minimal in-memory stand-in for the subset of `redis.Redis` used by
    `locks.py`: `set(..., nx=..., px=...)` (acquire) + `register_script`
    (release's Lua compare-and-delete). No live Redis required."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str, nx: bool = False, px: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def register_script(self, script_src: str) -> _FakeScript:
        return _FakeScript(_script_method_name(script_src))

    # -- script body, transliterated 1:1 from locks.py's Lua ------------

    def _run_release(self, keys: list[Any], args: list[Any]) -> int:
        key = keys[0]
        token = args[0]
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


class _BrokenRedis:
    """A client whose every call raises, simulating a Redis outage."""

    def set(self, key: str, value: str, nx: bool = False, px: int | None = None) -> bool:
        raise ConnectionError("redis unavailable")

    def register_script(self, script_src: str) -> _FakeScript:
        return _FakeScript(_script_method_name(script_src))

    def _run_release(self, keys: list[Any], args: list[Any]) -> Any:
        raise ConnectionError("redis unavailable")


# --- acquire_match_lock / single owner ---------------------------------------


def test_single_owner() -> None:
    """Two concurrent acquires on one key ⇒ exactly one True (SC-002)."""
    redis = _FakeRedis()
    key = match_lock_key(uuid.uuid4(), uuid.uuid4())
    token_a = new_fencing_token()
    token_b = new_fencing_token()

    first = acquire_match_lock(redis, key=key, token=token_a, ttl_seconds=600)
    second = acquire_match_lock(redis, key=key, token=token_b, ttl_seconds=600)

    assert first is True
    assert second is False


def test_new_fencing_token_is_unique_and_hex() -> None:
    token_a = new_fencing_token()
    token_b = new_fencing_token()

    assert token_a != token_b
    assert len(token_a) == 32  # secrets.token_hex(16) -> 32 hex chars
    int(token_a, 16)  # must be valid hex -- raises ValueError otherwise


# --- release_match_lock / fencing compare-and-delete -------------------------


def test_fencing_compare_and_delete() -> None:
    """A release with a foreign token is a no-op; the owner's token deletes."""
    redis = _FakeRedis()
    key = match_lock_key(uuid.uuid4(), uuid.uuid4())
    owner_token = new_fencing_token()
    other_token = new_fencing_token()

    assert acquire_match_lock(redis, key=key, token=owner_token, ttl_seconds=600) is True

    foreign_release = release_match_lock(redis, key=key, token=other_token)
    assert foreign_release is False
    assert redis.store[key] == owner_token  # untouched -- no delete

    owner_release = release_match_lock(redis, key=key, token=owner_token)
    assert owner_release is True
    assert key not in redis.store


def test_reacquire_after_release() -> None:
    """After the owner releases, the key is re-acquirable (US2 AS3)."""
    redis = _FakeRedis()
    key = match_lock_key(uuid.uuid4(), uuid.uuid4())
    first_token = new_fencing_token()

    assert acquire_match_lock(redis, key=key, token=first_token, ttl_seconds=600) is True
    assert release_match_lock(redis, key=key, token=first_token) is True

    second_token = new_fencing_token()
    assert acquire_match_lock(redis, key=key, token=second_token, ttl_seconds=600) is True


def test_release_swallows_redis_errors() -> None:
    """Release never raises even when Redis is down (D3)."""
    redis = _BrokenRedis()
    result = release_match_lock(redis, key="lock:scrape:ws:match", token="tok")  # must not raise
    assert result is False


# --- fail-closed on Redis error (FR-023) --------------------------------------


def test_fail_closed_on_redis_error() -> None:
    redis = _BrokenRedis()
    acquired = acquire_match_lock(redis, key="lock:scrape:ws:match", token="tok", ttl_seconds=600)
    assert acquired is False


# --- Lua source sanity check (exercised functionally above via the fake) -----


def test_release_lua_is_a_compare_and_delete() -> None:
    source = locks_mod._RELEASE_LUA
    assert "GET" in source
    assert "DEL" in source
    assert "ARGV[1]" in source
