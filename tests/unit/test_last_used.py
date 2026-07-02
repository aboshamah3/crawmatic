"""Unit tests for the API-key ``last_used_at`` write throttle (SPEC-03 T035, FR-015/SC-008).

`app_shared.security.last_used` — exercised with an in-memory fake Redis
client (no live Redis required).
"""

from __future__ import annotations

import uuid

from app_shared.security.last_used import should_write_last_used


class _FakeRedis:
    """Minimal in-memory stand-in for ``SET key 1 NX EX ttl`` semantics."""

    def __init__(self) -> None:
        self._store: dict[str, int] = {}

    def set(self, key: str, value: int, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self._store:
            return False
        self._store[key] = value
        return True


class _BrokenRedis:
    """A client whose every call raises, simulating a Redis outage."""

    def set(self, key: str, value: int, *, nx: bool = False, ex: int | None = None) -> bool:
        raise ConnectionError("redis unavailable")


def test_returns_true_once_then_false_within_the_window() -> None:
    redis = _FakeRedis()
    key_id = uuid.uuid4()

    first = should_write_last_used(redis, key_id=key_id, throttle_seconds=60)
    second = should_write_last_used(redis, key_id=key_id, throttle_seconds=60)
    third = should_write_last_used(redis, key_id=key_id, throttle_seconds=60)

    assert first is True
    assert second is False
    assert third is False


def test_different_keys_are_gated_independently() -> None:
    redis = _FakeRedis()
    key_a = uuid.uuid4()
    key_b = uuid.uuid4()

    assert should_write_last_used(redis, key_id=key_a, throttle_seconds=60) is True
    assert should_write_last_used(redis, key_id=key_b, throttle_seconds=60) is True
    assert should_write_last_used(redis, key_id=key_a, throttle_seconds=60) is False


def test_fail_safe_returns_false_on_redis_error() -> None:
    redis = _BrokenRedis()
    result = should_write_last_used(redis, key_id=uuid.uuid4(), throttle_seconds=60)
    assert result is False
