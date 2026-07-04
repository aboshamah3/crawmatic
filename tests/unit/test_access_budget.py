"""`app_shared/access/budget.py` unit tests (SPEC-10 US2 T030,
`contracts/budget-ceilings.md` Acceptance, FR-010/FR-011).

Fake in-memory Redis (mirrors `tests/unit/test_rate_limit.py`'s
`_FakeRedis`/`_BrokenRedis` pattern, extended with `set(..., nx=...,
ex=...)` support for `check_domain_cooldown`'s `SET NX EX` gate) -- no
live Redis required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from app_shared.access.budget import (
    BudgetResult,
    RateDecision,
    check_domain_cooldown,
    check_rate_ceilings,
    incr_and_check_monthly_budget,
)


class _FakeRedis:
    """Minimal in-memory stand-in for the subset of redis.Redis used here."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.nx_keys: dict[str, int] = {}  # key -> "ttl" (not decremented, just presence)

    def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key: str, seconds: int) -> None:
        self.ttls[key] = seconds

    def ttl(self, key: str) -> int:
        return self.ttls.get(key, -2)

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.nx_keys:
            return False
        self.nx_keys[key] = ex or 0
        return True

    def expire_now(self, key: str) -> None:
        """Test helper: simulate the NX key expiring."""
        self.nx_keys.pop(key, None)


class _BrokenRedis:
    """A client whose every call raises, simulating a Redis outage."""

    def incr(self, key: str) -> int:
        raise ConnectionError("redis unavailable")

    def expire(self, key: str, seconds: int) -> None:  # pragma: no cover - unreachable
        raise ConnectionError("redis unavailable")

    def ttl(self, key: str) -> int:  # pragma: no cover - unreachable
        raise ConnectionError("redis unavailable")

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        raise ConnectionError("redis unavailable")


_NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


# --- incr_and_check_monthly_budget ------------------------------------------


def test_monthly_budget_allows_under_the_limit() -> None:
    redis = _FakeRedis()
    provider_id = uuid.uuid4()

    for _ in range(3):
        result = incr_and_check_monthly_budget(redis, provider_id=provider_id, limit=5, now=_NOW)
        assert result.allowed is True


def test_monthly_budget_limit_plus_one_increment_is_denied() -> None:
    redis = _FakeRedis()
    provider_id = uuid.uuid4()

    last: BudgetResult | None = None
    for _ in range(6):  # limit=5, 6th increment exceeds it
        last = incr_and_check_monthly_budget(redis, provider_id=provider_id, limit=5, now=_NOW)

    assert last is not None
    assert last.allowed is False
    assert last.used == 6
    assert last.limit == 5


def test_monthly_budget_ttl_set_only_on_first_hit() -> None:
    redis = _FakeRedis()
    provider_id = uuid.uuid4()
    key = f"proxybudget:{provider_id}:{_NOW:%Y_%m}"

    incr_and_check_monthly_budget(redis, provider_id=provider_id, limit=10, now=_NOW)
    assert key in redis.ttls
    first_ttl = redis.ttls[key]

    redis.ttls[key] = -999  # sentinel to prove expire() isn't called again
    incr_and_check_monthly_budget(redis, provider_id=provider_id, limit=10, now=_NOW)
    assert redis.ttls[key] == -999
    assert first_ttl > 0


def test_monthly_budget_resets_on_a_new_year_month() -> None:
    redis = _FakeRedis()
    provider_id = uuid.uuid4()

    for _ in range(6):
        incr_and_check_monthly_budget(redis, provider_id=provider_id, limit=5, now=_NOW)

    next_month = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)
    result = incr_and_check_monthly_budget(redis, provider_id=provider_id, limit=5, now=next_month)

    assert result.allowed is True
    assert result.used == 1


def test_monthly_budget_none_limit_always_allowed_and_not_incremented() -> None:
    redis = _FakeRedis()
    provider_id = uuid.uuid4()

    result = incr_and_check_monthly_budget(redis, provider_id=provider_id, limit=None, now=_NOW)

    assert result == BudgetResult(allowed=True, used=0, limit=None)
    assert redis.counters == {}


def test_monthly_budget_fail_open_on_redis_error() -> None:
    redis = _BrokenRedis()
    result = incr_and_check_monthly_budget(redis, provider_id=uuid.uuid4(), limit=5, now=_NOW)

    assert result.allowed is True


# --- check_rate_ceilings -----------------------------------------------------


def test_rate_ceilings_allows_under_every_ceiling() -> None:
    redis = _FakeRedis()
    for _ in range(3):
        decision = check_rate_ceilings(
            redis, policy_id=uuid.uuid4(), domain="shop.example.com", per_minute=5, per_hour=100, per_day=1000
        )
        assert decision.allowed is True


def test_rate_ceilings_per_minute_exceeded_denies_with_positive_retry_after() -> None:
    redis = _FakeRedis()
    policy_id = uuid.uuid4()
    domain = "shop.example.com"

    last: RateDecision | None = None
    for _ in range(4):
        last = check_rate_ceilings(redis, policy_id=policy_id, domain=domain, per_minute=3, per_hour=None, per_day=None)

    assert last is not None
    assert last.allowed is False
    assert last.retry_after_seconds > 0


def test_rate_ceilings_windows_are_tracked_independently() -> None:
    # A generous per-minute ceiling never trips; the per-hour ceiling is
    # what exceeds -- proving the two windows are independent counters,
    # not one shared counter.
    redis = _FakeRedis()
    policy_id = uuid.uuid4()
    domain = "shop.example.com"

    results = [
        check_rate_ceilings(redis, policy_id=policy_id, domain=domain, per_minute=1000, per_hour=2, per_day=1000)
        for _ in range(3)
    ]

    assert [r.allowed for r in results] == [True, True, False]

    minute_key = f"ratelimit:{policy_id}:{domain}:60"
    hour_key = f"ratelimit:{policy_id}:{domain}:3600"
    day_key = f"ratelimit:{policy_id}:{domain}:86400"
    # The minute window (checked first, never exceeded) incremented every
    # call; the day window is only reached while the hour window still
    # allows (calls 1-2), never on the denying 3rd call.
    assert redis.counters[minute_key] == 3
    assert redis.counters[hour_key] == 3
    assert redis.counters[day_key] == 2


def test_rate_ceilings_none_ceiling_is_skipped_entirely() -> None:
    redis = _FakeRedis()
    policy_id = uuid.uuid4()
    domain = "shop.example.com"

    for _ in range(50):
        decision = check_rate_ceilings(redis, policy_id=policy_id, domain=domain, per_minute=None, per_hour=None, per_day=None)
        assert decision.allowed is True

    assert redis.counters == {}


def test_rate_ceilings_fail_open_on_redis_error() -> None:
    redis = _BrokenRedis()
    decision = check_rate_ceilings(
        redis, policy_id=uuid.uuid4(), domain="shop.example.com", per_minute=1, per_hour=1, per_day=1
    )
    assert decision.allowed is True
    assert decision.retry_after_seconds == 0


# --- check_domain_cooldown ----------------------------------------------------


def test_domain_cooldown_second_call_within_window_is_denied() -> None:
    redis = _FakeRedis()
    domain = "shop.example.com"

    first = check_domain_cooldown(redis, domain=domain, cooldown_seconds=30)
    second = check_domain_cooldown(redis, domain=domain, cooldown_seconds=30)

    assert first is True
    assert second is False


def test_domain_cooldown_after_expiry_allows_again() -> None:
    redis = _FakeRedis()
    domain = "shop.example.com"

    assert check_domain_cooldown(redis, domain=domain, cooldown_seconds=30) is True
    redis.expire_now(f"cooldown:{domain}")
    assert check_domain_cooldown(redis, domain=domain, cooldown_seconds=30) is True


def test_domain_cooldown_non_positive_seconds_always_allows() -> None:
    redis = _FakeRedis()
    domain = "shop.example.com"

    assert check_domain_cooldown(redis, domain=domain, cooldown_seconds=0) is True
    assert check_domain_cooldown(redis, domain=domain, cooldown_seconds=-1) is True
    # Never even touched Redis for a non-positive cooldown.
    assert redis.nx_keys == {}


def test_domain_cooldown_fail_open_on_redis_error() -> None:
    redis = _BrokenRedis()
    assert check_domain_cooldown(redis, domain="shop.example.com", cooldown_seconds=30) is True


# --- FR-010/§22: budget.py must never query request_attempts ----------------


def test_budget_module_never_references_request_attempts_table() -> None:
    source_path = Path(__file__).resolve().parents[2] / "libs" / "shared" / "app_shared" / "access" / "budget.py"
    source = source_path.read_text(encoding="utf-8")

    assert "request_attempts" not in source
    assert "RequestAttempt" not in source
