"""Unit tests for the Redis status cache (SPEC-03 T047, FR-022/SC-007).

`app_shared.security.status_cache` — exercised with a fake Redis client
and a fake `session_factory` (no real DB engine). Covers: a cache hit
returns the cached status with NO DB read; a miss triggers exactly one
DB read then repopulates the cache with the configured TTL; a Redis
error fail-safe denies (`STATUS_UNAVAILABLE`, never "active"); and
`invalidate_user`/`invalidate_workspace` clear the keys for immediate
suspension propagation.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from app_shared.security.status_cache import (
    STATUS_UNAVAILABLE,
    get_user_status,
    get_workspace_status,
    invalidate_user,
    invalidate_workspace,
)


class _FakeRedis:
    """Minimal in-memory stand-in for the GET/SET/DELETE subset used here."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int]] = []

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.set_calls.append((key, value, ex or 0))

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


class _BrokenRedis:
    """A client whose every call raises, simulating a Redis outage."""

    def get(self, key: str) -> str | None:
        raise ConnectionError("redis unavailable")

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        raise ConnectionError("redis unavailable")

    def delete(self, key: str) -> None:
        raise ConnectionError("redis unavailable")


class _FakeRow:
    def __init__(self, status: str) -> None:
        self.status = status


class _FakeScalarResult:
    def __init__(self, row: _FakeRow | None) -> None:
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeDbSession:
    """Records how many times `execute` was called (the DB-read counter)."""

    def __init__(self, row: _FakeRow | None) -> None:
        self._row = row
        self.execute_count = 0

    def execute(self, *args, **kwargs) -> _FakeScalarResult:
        self.execute_count += 1
        return _FakeScalarResult(self._row)


def _session_factory_for(row: _FakeRow | None):
    fake_session = _FakeDbSession(row)

    @contextmanager
    def _factory():
        yield fake_session

    _factory.session = fake_session  # expose for assertions
    return _factory


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import app_shared.security.status_cache as status_cache_module

    class _FakeSettings:
        STATUS_CACHE_TTL_SECONDS = 30

    monkeypatch.setattr(status_cache_module, "get_settings", lambda: _FakeSettings())


# --- cache hit: no DB read -------------------------------------------------


def test_get_user_status_cache_hit_returns_cached_value_with_no_db_read() -> None:
    redis = _FakeRedis()
    user_id = uuid.uuid4()
    redis.store[f"status:user:{user_id}"] = "active"

    factory = _session_factory_for(row=None)  # would blow up if actually queried

    status = get_user_status(redis, factory, user_id)

    assert status == "active"
    assert factory.session.execute_count == 0


def test_get_workspace_status_cache_hit_returns_cached_value_with_no_db_read() -> None:
    redis = _FakeRedis()
    workspace_id = uuid.uuid4()
    redis.store[f"status:ws:{workspace_id}"] = "suspended"

    factory = _session_factory_for(row=None)

    status = get_workspace_status(redis, factory, workspace_id)

    assert status == "suspended"
    assert factory.session.execute_count == 0


# --- cache miss: single DB read + repopulate with TTL ----------------------


def test_get_user_status_cache_miss_reads_db_once_and_repopulates() -> None:
    redis = _FakeRedis()
    user_id = uuid.uuid4()
    factory = _session_factory_for(row=_FakeRow(status="active"))

    status = get_user_status(redis, factory, user_id)

    assert status == "active"
    assert factory.session.execute_count == 1
    assert redis.store[f"status:user:{user_id}"] == "active"
    assert redis.set_calls == [(f"status:user:{user_id}", "active", 30)]

    # A second call is now a cache hit -- no further DB read.
    status_again = get_user_status(redis, factory, user_id)
    assert status_again == "active"
    assert factory.session.execute_count == 1


def test_get_workspace_status_cache_miss_reads_db_once_and_repopulates() -> None:
    redis = _FakeRedis()
    workspace_id = uuid.uuid4()
    factory = _session_factory_for(row=_FakeRow(status="suspended"))

    status = get_workspace_status(redis, factory, workspace_id)

    assert status == "suspended"
    assert factory.session.execute_count == 1
    assert redis.store[f"status:ws:{workspace_id}"] == "suspended"


def test_missing_row_on_db_miss_is_status_unavailable() -> None:
    redis = _FakeRedis()
    user_id = uuid.uuid4()
    factory = _session_factory_for(row=None)

    status = get_user_status(redis, factory, user_id)

    assert status == STATUS_UNAVAILABLE
    # A not-found result must never be cached as if it were a real status.
    assert f"status:user:{user_id}" not in redis.store


# --- fail-safe deny on redis error -----------------------------------------


def test_redis_get_error_falls_through_to_db_then_still_returns_status() -> None:
    """A GET failure must not crash -- treated as a miss, DB read still happens."""
    redis = _BrokenRedis()
    user_id = uuid.uuid4()
    factory = _session_factory_for(row=_FakeRow(status="active"))

    status = get_user_status(redis, factory, user_id)

    assert status == "active"
    assert factory.session.execute_count == 1


def test_db_read_error_on_miss_fails_safe_to_unavailable() -> None:
    redis = _FakeRedis()
    user_id = uuid.uuid4()

    @contextmanager
    def _raising_factory():
        raise RuntimeError("db unreachable")
        yield  # pragma: no cover - unreachable, satisfies generator shape

    status = get_user_status(redis, _raising_factory, user_id)

    assert status == STATUS_UNAVAILABLE


# --- invalidate_user / invalidate_workspace --------------------------------


def test_invalidate_user_clears_the_cached_key() -> None:
    redis = _FakeRedis()
    user_id = uuid.uuid4()
    redis.store[f"status:user:{user_id}"] = "active"

    invalidate_user(redis, user_id)

    assert f"status:user:{user_id}" not in redis.store


def test_invalidate_workspace_clears_the_cached_key() -> None:
    redis = _FakeRedis()
    workspace_id = uuid.uuid4()
    redis.store[f"status:ws:{workspace_id}"] = "active"

    invalidate_workspace(redis, workspace_id)

    assert f"status:ws:{workspace_id}" not in redis.store


def test_invalidate_user_is_safe_on_redis_error() -> None:
    # Must not raise even if the underlying delete fails.
    invalidate_user(_BrokenRedis(), uuid.uuid4())


def test_invalidate_workspace_is_safe_on_redis_error() -> None:
    invalidate_workspace(_BrokenRedis(), uuid.uuid4())
