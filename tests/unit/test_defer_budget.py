"""`scrape_core.defer_budget` — the bound on DEFERRED cycling.

DEFERRED is non-terminal, so without a budget a persistently blocked
domain (noon while re-blocked, S-Tech while it throttles the Railway IP)
cycles defer -> re-dispatch -> defer forever and its job never finalizes.
These tests pin the budget's three properties: it allows exactly
`max_cycles` genuine re-attempts, it is per-(job, match) so one bad target
never spends another's budget, and a Redis outage fails OPEN (keep
deferring) — a counter we cannot read must never harden a transient
rate-limit into a permanent failure.
"""

from __future__ import annotations

import uuid

from scrape_core.defer_budget import consume_defer_budget, defer_budget_key


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key: str, seconds: int) -> None:
        self.expires[key] = seconds


class _BrokenRedis:
    def incr(self, key: str) -> int:
        raise ConnectionError("redis down")

    def expire(self, key: str, seconds: int) -> None:  # pragma: no cover - never reached
        raise ConnectionError("redis down")


JOB = uuid.uuid4()
MATCH = uuid.uuid4()


def _consume(redis: object, *, job: uuid.UUID = JOB, match: uuid.UUID = MATCH, max_cycles: int = 3) -> bool:
    return consume_defer_budget(redis, scrape_job_id=job, match_id=match, max_cycles=max_cycles)


def test_allows_exactly_max_cycles_then_fails() -> None:
    redis = _FakeRedis()
    assert [_consume(redis) for _ in range(3)] == [True, True, True]
    assert _consume(redis) is False, "4th defer must exhaust a budget of 3"
    assert _consume(redis) is False, "and stay exhausted"


def test_budget_is_per_target() -> None:
    """One hopeless target must not spend a sibling's budget."""
    redis = _FakeRedis()
    other = uuid.uuid4()
    for _ in range(4):
        _consume(redis)
    assert _consume(redis, match=other) is True


def test_budget_is_per_job() -> None:
    """A later job re-attempts the same match with a fresh budget."""
    redis = _FakeRedis()
    for _ in range(4):
        _consume(redis)
    assert _consume(redis, job=uuid.uuid4()) is True


def test_ttl_set_once_on_first_defer() -> None:
    redis = _FakeRedis()
    _consume(redis)
    _consume(redis)
    assert redis.expires[defer_budget_key(JOB, MATCH)] > 0


def test_redis_failure_fails_open() -> None:
    """Never turn an unreadable counter into a permanent failure."""
    assert _consume(_BrokenRedis()) is True


def test_zero_budget_never_defers() -> None:
    assert _consume(_FakeRedis(), max_cycles=0) is False
