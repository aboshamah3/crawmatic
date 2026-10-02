"""Unit tests for the login rate limiter (SPEC-03 T025, FR-007/SC-009).

`app_shared.security.rate_limit` — exercised with an in-memory fake Redis
client (no live Redis required).
"""

from __future__ import annotations

from app_shared.security.rate_limit import check_and_increment_login


class _FakeRedis:
    """Minimal in-memory stand-in for the subset of redis.Redis used here."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.locks: dict[str, tuple[int, int]] = {}

    def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key: str, seconds: int) -> None:
        self.ttls[key] = seconds

    def ttl(self, key: str) -> int:
        # -2 == key does not exist (redis convention).
        return self.locks.get(key, (None, -2))[1]

    def set(self, key: str, value: int, ex: int) -> None:
        self.locks[key] = (value, ex)


class _BrokenRedis:
    """A client whose every call raises, simulating a Redis outage."""

    def incr(self, key: str) -> int:
        raise ConnectionError("redis unavailable")

    def expire(self, key: str, seconds: int) -> None:  # pragma: no cover - unreachable
        raise ConnectionError("redis unavailable")

    def ttl(self, key: str) -> int:  # pragma: no cover - unreachable
        raise ConnectionError("redis unavailable")

    def set(self, key: str, value: int, ex: int) -> None:  # pragma: no cover - unreachable
        raise ConnectionError("redis unavailable")


def test_allows_attempts_under_the_threshold() -> None:
    redis = _FakeRedis()
    for _ in range(3):
        result = check_and_increment_login(
            redis, email="a@example.com", source_ip="1.2.3.4", max_attempts=5, window_seconds=60
        )
        assert result.allowed is True


def test_refuses_once_the_per_account_threshold_is_exceeded() -> None:
    redis = _FakeRedis()
    last_result = None
    for _ in range(6):
        last_result = check_and_increment_login(
            redis,
            email="repeat@example.com",
            source_ip="10.0.0.1",
            max_attempts=5,
            window_seconds=60,
        )
    assert last_result is not None
    assert last_result.allowed is False


def test_refuses_once_the_per_source_threshold_is_exceeded_independently() -> None:
    redis = _FakeRedis()
    last_result = None
    # Different email every time, same source IP -- the source counter
    # alone should trip the limit.
    for i in range(6):
        last_result = check_and_increment_login(
            redis,
            email=f"user{i}@example.com",
            source_ip="10.0.0.9",
            max_attempts=5,
            window_seconds=60,
        )
    assert last_result is not None
    assert last_result.allowed is False


def test_result_carries_no_factor_disclosure() -> None:
    redis = _FakeRedis()
    for _ in range(6):
        result = check_and_increment_login(
            redis, email="x@example.com", source_ip="9.9.9.9", max_attempts=5, window_seconds=60
        )
    # The result type only exposes allowed/retry_after -- no field naming
    # which counter (account vs source) actually tripped.
    assert set(vars(result).keys()) == {"allowed", "retry_after_seconds"}


def test_fail_safe_denies_on_redis_error() -> None:
    redis = _BrokenRedis()
    result = check_and_increment_login(
        redis, email="a@example.com", source_ip="1.2.3.4", max_attempts=5, window_seconds=60
    )
    assert result.allowed is False


# --- security plan 2026-10-02 E10: the lock is enforced ------------------


def _attempt(redis, *, email="locked@example.com", ip="10.1.1.1"):
    return check_and_increment_login(
        redis, email=email, source_ip=ip, max_attempts=5, window_seconds=60
    )


def test_lockout_is_enforced_after_progressive_failures() -> None:
    redis = _FakeRedis()
    results = [_attempt(redis) for _ in range(6)]
    assert [r.allowed for r in results] == [True] * 5 + [False]

    # The counter window resets (as when its 60s TTL lapses) but the lock
    # is still live: the attempt is refused and nothing is counted.
    for key in [k for k in redis.counters if ":strikes:" not in k]:
        del redis.counters[key]
    before = dict(redis.counters)
    locked = _attempt(redis)
    assert locked.allowed is False
    assert locked.retry_after_seconds == 60
    assert redis.counters == before


def test_lock_on_the_source_refuses_a_fresh_account() -> None:
    redis = _FakeRedis()
    for i in range(6):
        _attempt(redis, email=f"spray{i}@example.com", ip="10.2.2.2")
    redis.counters.clear()
    assert _attempt(redis, email="brand-new@example.com", ip="10.2.2.2").allowed is False


def test_each_new_lock_doubles() -> None:
    redis = _FakeRedis()
    for _ in range(6):
        _attempt(redis)
    first = max(ttl for _, ttl in redis.locks.values())
    # Lock lapses, counter window lapses (the day-long strike memory does
    # not), the scope trips again.
    redis.locks.clear()
    for key in [k for k in redis.counters if ":strikes:" not in k]:
        del redis.counters[key]
    for _ in range(6):
        _attempt(redis)
    second = max(ttl for _, ttl in redis.locks.values())
    assert (first, second) == (60, 120)


def test_unlocked_scope_is_not_affected_by_another_scopes_lock() -> None:
    redis = _FakeRedis()
    for _ in range(6):
        _attempt(redis)
    assert _attempt(redis, email="other@example.com", ip="10.9.9.9").allowed is True
