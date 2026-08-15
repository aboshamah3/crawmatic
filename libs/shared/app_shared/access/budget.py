"""Redis budget & rate ceilings (`contracts/budget-ceilings.md`, SPEC-10 US2, FR-010/FR-011).

Framework-agnostic; takes a `redis.Redis`-shaped client (like
`security/rate_limit.py`), stdlib otherwise -- no SQLAlchemy/FastAPI/
Scrapy imports. All keys live on the correctness-critical `noeviction`
Redis. Enforces the proxy monthly budget and policy/domain ceilings with
cheap counters -- **never** a scan of the append-only per-attempt audit
table (FR-010, §22, Principle VIII; grep-enforced by the caller's
verification step, and self-asserted by `tests/unit/test_access_budget.py`).

## Fail posture (REVISED 2026-08-15, audit risk H3)

Previously *every* function here failed **open** (`allowed=True`) on a
Redis error. The audit found that unsafe for paid network access: a
Redis incident plus a scraper retry/rediscovery loop removes the cost
brake exactly when accounting is blind, and the 2026-08-12
hostname-normalisation loop showed a correctness bug can authorise
~$325/month of unattended proxy spend. The posture is now split by who
pays:

* :func:`incr_and_check_monthly_budget` -- called **only** for a PROXIED
  (paid) request -- now fails **CLOSED**. No ledger, no new paid work.
* :func:`check_rate_ceilings` / :func:`check_domain_cooldown_gate` --
  these run *before* the transport decision is made, so they cannot know
  yet whether the attempt would be paid. Failing them closed would kill
  DIRECT scraping too (the majority of traffic, and free). They
  therefore still return `allowed=True`, but now also set
  **`degraded=True`**, and the caller (the scrape path's
  `_prepare_dispatch`) applies the paid/unpaid split precisely: a
  degraded ledger downgrades a *proxied* plan through the existing
  `proxy_budget_exhausted` path (direct if the strategy has a direct
  step, else `LIMIT_REACHED`) and leaves a *direct* plan untouched.

`degraded=True` therefore means "the cost ledger is impaired", never
"denied" -- the denial decision belongs to the caller that knows whether
the attempt is paid. `fail_open_on_error=True` (wired to the
`PROXY_LEDGER_FAIL_OPEN` emergency env override, default off) restores
the old behaviour during an incident.

Note the deliberate contrast with `security/rate_limit.py` (login
limiter: fails closed/deny) and `apps/api/app/rate_limit.py` (API
request limiting: still fails **open**, an accepted availability
trade-off per the audit -- deliberately unchanged).
"""

from __future__ import annotations

import calendar
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

#: Structured-log event name emitted whenever the cost ledger could not
#: be reached. Counted by the alerting workstream (one line per
#: impaired gate) -- `contracts/observability.md` JSON convention, no
#: metrics client needed. Fields: `gate`, `failed_open`.
LEDGER_DEGRADED_EVENT = "proxy_ledger.degraded"


def _log_degraded(gate: str, *, failed_open: bool) -> None:
    """Emit the single-line JSON `proxy_ledger.degraded` counter. Never raises."""
    try:
        logger.error(
            json.dumps(
                {"event": LEDGER_DEGRADED_EVENT, "gate": gate, "failed_open": failed_open}
            )
        )
    except Exception:  # noqa: BLE001 - logging must never break the scrape path
        pass


@dataclass(frozen=True)
class BudgetResult:
    """Outcome of a monthly proxy-budget increment/check.

    ``degraded`` marks "the cost ledger was unreachable" -- distinct from
    ``allowed``, which is the decision. See the module docstring's fail
    posture section.
    """

    allowed: bool
    used: int
    limit: int | None
    degraded: bool = False


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
    redis: object,
    *,
    provider_id: uuid.UUID | str,
    limit: int | None,
    now: datetime,
    fail_open_on_error: bool = False,
) -> BudgetResult:
    """Increment and check a proxy provider's monthly-budget counter.

    Key `proxybudget:{provider_id}:{now:%Y_%m}`; `INCR`, and on the
    first hit of the month (`used == 1`) `EXPIRE` to the end of the
    month so a stale key never lingers into the next period. Called
    once per PROXIED request only (never for a direct attempt).

    `limit is None` -> always allowed, and the counter is not
    incremented at all (no cap to track against).

    **Any Redis error -> fail CLOSED** (`allowed=False`, `degraded=True`)
    as of 2026-08-15 (audit H3). This function is on the paid path by
    construction, so denying here denies exactly the requests that cost
    money; the caller degrades the attempt to a direct one where the
    strategy allows it. `fail_open_on_error=True` (the
    `PROXY_LEDGER_FAIL_OPEN` emergency override) restores the historical
    fail-open behaviour.

    Note `limit is None` still short-circuits before any Redis call: a
    provider with no configured cap has no ledger to be impaired, so a
    Redis outage cannot change its (already unlimited) answer. Bounding
    *that* case is the independent circuit breaker's job
    (`app_shared.access.breaker`), not this counter's.
    """
    if limit is None:
        return BudgetResult(allowed=True, used=0, limit=None)

    key = _monthly_budget_key(provider_id, now)
    try:
        used = redis.incr(key)  # type: ignore[attr-defined]
        if used == 1:
            redis.expire(key, _seconds_until_month_end(now))  # type: ignore[attr-defined]
    except Exception:
        _log_degraded("monthly_budget", failed_open=fail_open_on_error)
        return BudgetResult(
            allowed=fail_open_on_error, used=0, limit=limit, degraded=True
        )

    return BudgetResult(allowed=used <= limit, used=used, limit=limit)


@dataclass(frozen=True)
class RateDecision:
    """Outcome of a windowed per-policy/domain ceiling check.

    ``degraded`` marks "the ledger was unreachable, this answer is not
    backed by a counter". It never on its own denies -- this gate runs
    before the proxied/direct transport decision exists, so the caller
    owns the paid/unpaid split (module docstring, fail posture).
    """

    allowed: bool
    retry_after_seconds: int
    degraded: bool = False


@dataclass(frozen=True)
class CooldownDecision:
    """Outcome of the per-domain cooldown gate.

    ``allowed`` -> the request may proceed. ``degraded`` -> the gate
    could not be consulted (same semantics as :class:`RateDecision`).
    """

    allowed: bool
    degraded: bool = False


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
    remaining TTL (the caller maps this to `RATE_LIMITED`, FR-011).

    Any Redis error -> `allowed=True` **with `degraded=True`**. This gate
    still fails open because it runs before the transport decision and
    would otherwise stop free DIRECT scraping too; `degraded` is what
    carries the impairment forward so the caller denies only the paid
    half (module docstring, fail posture).
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
        _log_degraded("rate_ceilings", failed_open=True)
        return RateDecision(allowed=True, retry_after_seconds=0, degraded=True)


def check_domain_cooldown_gate(
    redis: object, *, domain: str, cooldown_seconds: int
) -> CooldownDecision:
    """`SET NX EX` gate: `cooldown:{domain}`, with ledger-health reporting.

    ``allowed=True`` if the request may proceed (gate acquired -- either
    `cooldown_seconds <= 0`, meaning no cooldown configured, or the key
    was not already set), ``allowed=False`` if still cooling down.

    Any Redis error -> `allowed=True` **with `degraded=True`**, for the
    same reason as :func:`check_rate_ceilings`: this gate runs before the
    proxied/direct decision, so failing it closed would stop free direct
    scraping. The caller applies the paid/unpaid split.

    This is the full-fidelity form of :func:`check_domain_cooldown`,
    which remains as a bool-only wrapper for existing call sites.
    """
    if cooldown_seconds <= 0:
        return CooldownDecision(allowed=True)
    key = f"cooldown:{domain}"
    try:
        acquired = redis.set(key, "1", nx=True, ex=cooldown_seconds)  # type: ignore[attr-defined]
    except Exception:
        _log_degraded("domain_cooldown", failed_open=True)
        return CooldownDecision(allowed=True, degraded=True)
    return CooldownDecision(allowed=bool(acquired))


def check_domain_cooldown(redis: object, *, domain: str, cooldown_seconds: int) -> bool:
    """Bool-only wrapper over :func:`check_domain_cooldown_gate`.

    Returns `True` if the request may proceed, `False` if still cooling
    down. Kept for call sites that do not need the ledger-health signal;
    the scrape path uses :func:`check_domain_cooldown_gate` so it can see
    `degraded`.
    """
    return check_domain_cooldown_gate(
        redis, domain=domain, cooldown_seconds=cooldown_seconds
    ).allowed
