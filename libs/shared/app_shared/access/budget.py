"""Redis budget & rate ceilings (`contracts/budget-ceilings.md`, SPEC-10 US2, FR-010/FR-011).

Framework-agnostic; takes a `redis.Redis`-shaped client (like
`security/rate_limit.py`), stdlib otherwise -- no SQLAlchemy/FastAPI/
Scrapy imports. All keys live on the correctness-critical `noeviction`
Redis. Enforces the proxy monthly budget and policy/domain ceilings with
cheap counters -- **never** a scan of the append-only per-attempt audit
table (FR-010, §22, Principle VIII; grep-enforced by the caller's
verification step, and self-asserted by `tests/unit/test_access_budget.py`).

Unlike `security/rate_limit.py` (the login rate limiter, which **fails
closed/deny** on a Redis error per its own contract), every function
here **fails open** (`allowed=True`) on a Redis error -- a Redis outage
must not wedge scraping; the target is still fetched direct per
strategy, and the cluster-wide hard limiter (SPEC-11) owns strict
enforcement. This divergence is deliberate and documented in
`contracts/budget-ceilings.md`.
"""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class BudgetResult:
    """Outcome of a monthly proxy-budget increment/check."""

    allowed: bool
    used: int
    limit: int | None


def _monthly_budget_key(provider_id: uuid.UUID | str, now: datetime) -> str:
    return f"proxybudget:{provider_id}:{now:%Y_%m}"


def _seconds_until_month_end(now: datetime) -> int:
    """Seconds remaining until the start of next month (>= 1)."""
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    month_end = now.replace(
        day=days_in_month, hour=23, minute=59, second=59, microsecond=999999
    )
    remaining = (month_end - now).total_seconds() + 1
    return max(1, int(remaining))


def incr_and_check_monthly_budget(
    redis: object, *, provider_id: uuid.UUID | str, limit: int | None, now: datetime
) -> BudgetResult:
    """Increment and check a proxy provider's monthly-budget counter.

    Key `proxybudget:{provider_id}:{now:%Y_%m}`; `INCR`, and on the
    first hit of the month (`used == 1`) `EXPIRE` to the end of the
    month so a stale key never lingers into the next period. Called
    once per PROXIED request only (never for a direct attempt).
    `limit is None` -> always allowed, and the counter is not
    incremented at all (no cap to track against). Any Redis error ->
    fail-open (`allowed=True`) -- see module docstring.
    """
    if limit is None:
        return BudgetResult(allowed=True, used=0, limit=None)

    key = _monthly_budget_key(provider_id, now)
    try:
        used = redis.incr(key)  # type: ignore[attr-defined]
        if used == 1:
            redis.expire(key, _seconds_until_month_end(now))  # type: ignore[attr-defined]
    except Exception:
        return BudgetResult(allowed=True, used=0, limit=limit)

    return BudgetResult(allowed=used <= limit, used=used, limit=limit)


@dataclass(frozen=True)
class RateDecision:
    """Outcome of a windowed per-policy/domain ceiling check."""

    allowed: bool
    retry_after_seconds: int


#: (window_name, window_seconds) -- also the Redis key suffix / TTL.
_CEILING_WINDOWS: tuple[tuple[str, int], ...] = (
    ("minute", 60),
    ("hour", 3600),
    ("day", 86400),
)


def check_rate_ceilings(
    redis: object,
    *,
    policy_id: uuid.UUID | str,
    domain: str,
    per_minute: int | None,
    per_hour: int | None,
    per_day: int | None,
) -> RateDecision:
    """Up to three windowed `INCR`+`EXPIRE` counters (60/3600/86400s).

    Keyed `ratelimit:{policy_id}:{domain}:{window_seconds}`. Any `None`
    ceiling is skipped entirely (not incremented). Exceeding any ceiling
    -> `allowed=False` with `retry_after_seconds` = that window's
    remaining TTL (the caller maps this to `RATE_LIMITED`, FR-011). Any
    Redis error -> fail-open (`allowed=True`, `retry_after_seconds=0`)
    -- see module docstring (documented divergence from the fail-closed
    login rate limiter).
    """
    ceilings = {"minute": per_minute, "hour": per_hour, "day": per_day}
    try:
        for window_name, window_seconds in _CEILING_WINDOWS:
            ceiling = ceilings[window_name]
            if ceiling is None:
                continue
            key = f"ratelimit:{policy_id}:{domain}:{window_seconds}"
            count = redis.incr(key)  # type: ignore[attr-defined]
            if count == 1:
                redis.expire(key, window_seconds)  # type: ignore[attr-defined]
            if count > ceiling:
                ttl = redis.ttl(key)  # type: ignore[attr-defined]
                retry_after = ttl if isinstance(ttl, int) and ttl > 0 else window_seconds
                return RateDecision(allowed=False, retry_after_seconds=retry_after)
        return RateDecision(allowed=True, retry_after_seconds=0)
    except Exception:
        return RateDecision(allowed=True, retry_after_seconds=0)


def check_domain_cooldown(redis: object, *, domain: str, cooldown_seconds: int) -> bool:
    """`SET NX EX` gate: `cooldown:{domain}`.

    Returns `True` if the request may proceed (gate acquired -- either
    `cooldown_seconds <= 0`, meaning no cooldown configured, or the key
    was not already set), `False` if still cooling down. Any Redis
    error -> fail-open (`True`) -- see module docstring.
    """
    if cooldown_seconds <= 0:
        return True
    key = f"cooldown:{domain}"
    try:
        acquired = redis.set(key, "1", nx=True, ex=cooldown_seconds)  # type: ignore[attr-defined]
    except Exception:
        return True
    return bool(acquired)
