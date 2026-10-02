"""Login rate limiter (`contracts/security-cache.md`).

Framework-agnostic helper taking a sync ``redis.Redis``-shaped client (or
any object exposing ``incr``/``expire``/``ttl``/``set``/``get``/``delete``
with the same semantics — e.g. a fake in tests). All keys live on the
correctness-critical ``noeviction`` Redis instance (§4). This helper
**fails safe (deny)** on any Redis error — a rate limiter that fails open
under a Redis outage would defeat FR-007/SC-009 (§4 Edge Case).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of a login rate-limit check. Carries no factor disclosure."""

    allowed: bool
    retry_after_seconds: int = 0


def _account_key(email: str) -> str:
    digest = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
    return f"rl:login:acct:{digest}"


def _source_key(source_ip: str) -> str:
    return f"rl:login:src:{source_ip}"


def _lock_key(scope_key: str) -> str:
    return f"rl:login:lock:{scope_key}"


def _incr_and_check(redis: object, key: str, *, max_attempts: int, window_seconds: int) -> bool:
    """Increment ``key``'s counter, (re)set its TTL, and return whether it is over threshold."""
    count = redis.incr(key)  # type: ignore[attr-defined]
    if count == 1:
        redis.expire(key, window_seconds)  # type: ignore[attr-defined]
    return count > max_attempts


def _strike_key(scope_key: str) -> str:
    return f"rl:login:strikes:{scope_key}"


#: How long a scope's strike count is remembered, and the longest single
#: lock it can earn. A day: long enough that a slow, patient attacker keeps
#: meeting a doubled lock, short enough that a user who fat-fingered a
#: password last week starts clean.
_STRIKE_MEMORY_SECONDS = 86400


def _apply_progressive_backoff(redis: object, scope_key: str, *, window_seconds: int) -> None:
    """Lock ``scope_key``; each new lock is twice as long as the last one.

    Strikes are counted in their own key (remembered for a day), so the
    doubling survives the lock expiring -- the lock is enforced (attempts
    while locked are refused without counting), so the old "double the
    live lock's TTL on every over-threshold attempt" could never fire
    again. Lock = ``window_seconds * 2**(strikes-1)``, capped at a day.
    """
    strike_key = _strike_key(scope_key)
    strikes = redis.incr(strike_key)  # type: ignore[attr-defined]
    redis.expire(strike_key, _STRIKE_MEMORY_SECONDS)  # type: ignore[attr-defined]
    exponent = max(int(strikes) - 1, 0)
    new_ttl = min(window_seconds * (2 ** min(exponent, 20)), _STRIKE_MEMORY_SECONDS)
    redis.set(_lock_key(scope_key), 1, ex=new_ttl)  # type: ignore[attr-defined]


def _lock_ttl(redis: object, scope_key: str) -> int:
    """Seconds left on ``scope_key``'s lock (0 when unlocked)."""
    ttl = redis.ttl(_lock_key(scope_key))  # type: ignore[attr-defined]
    return int(ttl) if ttl is not None and ttl > 0 else 0


def check_and_increment_login(
    redis: object,
    *,
    email: str,
    source_ip: str,
    max_attempts: int,
    window_seconds: int,
) -> RateLimitResult:
    """Count this login attempt against per-account AND per-source limits.

    Refuses (``allowed=False``) if either the account or the source is
    over ``max_attempts`` within ``window_seconds``. Any Redis error is
    treated as fail-safe deny (never allow unlimited attempts). The
    result carries no indication of *which* factor (account vs source)
    tripped the limit — no factor disclosure (FR-006/FR-007/SC-009).
    """
    try:
        account_key = _account_key(email)
        source_key = _source_key(source_ip)

        # Security plan 2026-10-02 (E10): the progressive-backoff lock is
        # ENFORCED. While either scope's lock is live the attempt is
        # refused outright -- before any counter moves, so a locked scope
        # cannot simply wait out the (shorter) counter window and get a
        # fresh `max_attempts` while the doubled lock is still running.
        lock_remaining = max(
            _lock_ttl(redis, account_key), _lock_ttl(redis, source_key)
        )
        if lock_remaining > 0:
            return RateLimitResult(allowed=False, retry_after_seconds=lock_remaining)

        account_over = _incr_and_check(
            redis, account_key, max_attempts=max_attempts, window_seconds=window_seconds
        )
        source_over = _incr_and_check(
            redis, source_key, max_attempts=max_attempts, window_seconds=window_seconds
        )

        if account_over:
            _apply_progressive_backoff(redis, account_key, window_seconds=window_seconds)
        if source_over:
            _apply_progressive_backoff(redis, source_key, window_seconds=window_seconds)

        if account_over or source_over:
            return RateLimitResult(allowed=False, retry_after_seconds=window_seconds)
        return RateLimitResult(allowed=True)
    except Exception:
        # Fail-safe: any Redis error denies the attempt rather than
        # allowing unlimited login tries.
        return RateLimitResult(allowed=False, retry_after_seconds=window_seconds)
