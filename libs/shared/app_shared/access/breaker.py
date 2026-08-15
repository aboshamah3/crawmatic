"""Independent spend/call circuit breaker (audit 2026-08-15 risk **H3**).

## The gap this closes

`access/budget.py`'s monthly counter now fails closed, which stops paid
work when Redis is *unreachable*. It cannot stop paid work when Redis is
*reachable but the number is wrong* -- a provider with
`monthly_budget_limit IS NULL` is never counted at all, and a
correctness bug upstream can authorise unlimited well-formed paid
probes that every Redis gate happily approves. The 2026-08-12
hostname-normalisation rediscovery loop was exactly that: no gate was
violated, every request was individually legitimate, and the run was on
course for ~$325/month of proxy spend.

So this breaker is deliberately **not** another Redis counter. Its
authoritative state is a Postgres row (`proxy_circuit_breakers`, see
`app_shared.models.proxy_breaker`) and its inputs are the durable
append-only audit tables (`request_attempts`, `strategy_discovery_runs`)
that record what actually happened -- not the counters that were
supposed to prevent it. It therefore survives a total Redis loss and
disagrees with a buggy caller.

## Layering

1. :func:`evaluate_thresholds` -- **pure**. Measurements + thresholds ->
   verdict. Stdlib only; every trip condition is unit-testable without a
   database (mirrors `access/engine.py`'s pure-decision discipline).
2. :func:`collect_observation` -- one read-only SQL pass over the audit
   tables producing a :class:`BreakerObservation`.
3. :func:`evaluate_and_persist` -- takes the evaluator lease, collects,
   evaluates, and writes the durable verdict. Any process may call it;
   at most one per `min_interval_seconds` does real work.
4. :func:`paid_requests_allowed` -- the **hot-path gate**. Reads the
   durable row through a short-lived per-process cache. This is what
   the scrape path's `_prepare_dispatch` consults.

## Trip conditions (audit §7 "Financial recommendation")

| Condition | Signal |
|---|---|
| `MONTHLY_SPEND` | month-to-date proxied requests vs absolute ceiling |
| `VELOCITY_1H` / `VELOCITY_24H` | trailing rate extrapolated to month end |
| `REQUESTS_PER_URL` | proxied requests / DISTINCT url (runaway-loop shape) |
| `DISCOVERY_RUNS_PER_DOMAIN` | discovery runs per domain per day |

Proxied requests are the spend proxy rather than dollars because bytes
are only known to the provider, not to us. The mapping is stable and
measured (2026-08-10: ~$1.96 of scraping across ~15.5k proxied
requests), and the provider-side prepaid balance is the true backstop --
see the report accompanying this change.

## Tripping never corrupts in-flight work

The gate is consulted only while deciding the **next** attempt, in
`_prepare_dispatch`, before any request is built. An already-dispatched
fetch runs to completion, its result persists normally, and its match
lock releases normally. An OPEN breaker degrades a proxied plan exactly
the way an exhausted budget does (`proxy_budget_exhausted=True`): direct
if the strategy has a direct step, else a clean `LIMIT_REACHED` skip
that the job can finalize on.

## Recovery

Deliberately **manual**. An auto-closing spend breaker re-arms the same
runaway it just stopped; whatever caused a month's budget to evaporate
in an hour needs a human. :func:`close_breaker` is the reset, and the
runbook command is in the accompanying report.
"""

from __future__ import annotations

import calendar
import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app_shared.models.proxy_breaker import (
    GLOBAL_BREAKER_SCOPE,
    ProxyBreakerState,
    ProxyBreakerTrip,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BreakerObservation",
    "BreakerThresholds",
    "BreakerVerdict",
    "close_breaker",
    "collect_observation",
    "evaluate_and_persist",
    "evaluate_thresholds",
    "paid_requests_allowed",
    "reset_gate_cache",
    "thresholds_from_settings",
    "trip_breaker",
]

#: Structured-log events (contracts/observability.md JSON convention).
BREAKER_TRIPPED_EVENT = "proxy_breaker.tripped"
BREAKER_DENIED_EVENT = "proxy_breaker.denied"
BREAKER_UNAVAILABLE_EVENT = "proxy_breaker.unavailable"


# --------------------------------------------------------------------------
# 1. Pure evaluation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakerThresholds:
    """Trip thresholds. ``None`` disables that single condition."""

    monthly_proxied_requests: int | None = None
    velocity_factor: float = 1.5
    velocity_min_sample: int = 200
    max_requests_per_url: float | None = None
    requests_per_url_min_sample: int = 500
    max_discovery_runs_per_domain_per_day: int | None = None


@dataclass(frozen=True)
class BreakerObservation:
    """What the durable audit tables say actually happened.

    ``month_started_at``/``now`` bound the month-to-date window so
    velocity extrapolation uses real remaining time, not an assumed
    30-day month.
    """

    now: datetime
    month_started_at: datetime
    #: Month-to-date PROXIED request count.
    proxied_requests_month: int = 0
    #: Trailing-1h / trailing-24h proxied request counts.
    proxied_requests_1h: int = 0
    proxied_requests_24h: int = 0
    #: Trailing-24h proxied requests and the DISTINCT urls behind them.
    proxied_requests_24h_for_ratio: int = 0
    distinct_urls_24h: int = 0
    #: Worst (domain, count) discovery-run pair in the trailing day.
    max_discovery_runs_domain: str | None = None
    max_discovery_runs_per_domain_day: int = 0

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot for the durable ``observed`` column."""
        return {
            "now": self.now.isoformat(),
            "month_started_at": self.month_started_at.isoformat(),
            "proxied_requests_month": self.proxied_requests_month,
            "proxied_requests_1h": self.proxied_requests_1h,
            "proxied_requests_24h": self.proxied_requests_24h,
            "proxied_requests_24h_for_ratio": self.proxied_requests_24h_for_ratio,
            "distinct_urls_24h": self.distinct_urls_24h,
            "max_discovery_runs_domain": self.max_discovery_runs_domain,
            "max_discovery_runs_per_domain_day": self.max_discovery_runs_per_domain_day,
        }


@dataclass(frozen=True)
class BreakerVerdict:
    """Outcome of :func:`evaluate_thresholds`."""

    tripped: bool
    reason: ProxyBreakerTrip | None = None
    detail: str | None = None


def _seconds_remaining_in_month(now: datetime) -> float:
    """Seconds from ``now`` to the first instant of next month (>= 0)."""
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    month_end = now.replace(
        day=days_in_month, hour=23, minute=59, second=59, microsecond=999_999
    )
    return max(0.0, (month_end - now).total_seconds())


def _velocity_forecast(
    observed_count: int, window_seconds: float, now: datetime, month_to_date: int
) -> float:
    """Month-end forecast = month-to-date + (rate x seconds remaining)."""
    if window_seconds <= 0:
        return float(month_to_date)
    rate_per_second = observed_count / window_seconds
    return month_to_date + rate_per_second * _seconds_remaining_in_month(now)


def evaluate_thresholds(
    observation: BreakerObservation, thresholds: BreakerThresholds
) -> BreakerVerdict:
    """Pure: measurements + thresholds -> trip verdict. No I/O.

    Conditions are checked most-severe-first so the recorded
    ``trip_reason`` is the strongest statement true of the data
    (absolute overrun beats a forecast; a forecast beats a shape
    heuristic). Every threshold is independently disable-able with
    ``None``, and the two ratio/velocity conditions additionally require
    a minimum sample so a quiet hour cannot extrapolate a tiny count
    into a trip.
    """
    limit = thresholds.monthly_proxied_requests

    # 1. Absolute month-to-date spend.
    if limit is not None and observation.proxied_requests_month >= limit:
        return BreakerVerdict(
            tripped=True,
            reason=ProxyBreakerTrip.MONTHLY_SPEND,
            detail=(
                f"month-to-date proxied requests {observation.proxied_requests_month} "
                f">= ceiling {limit}"
            ),
        )

    # 2/3. Velocity: extrapolate the trailing window to month end.
    if limit is not None:
        forecast_ceiling = limit * thresholds.velocity_factor
        for reason, count, window_seconds, label in (
            (ProxyBreakerTrip.VELOCITY_1H, observation.proxied_requests_1h, 3600.0, "1h"),
            (
                ProxyBreakerTrip.VELOCITY_24H,
                observation.proxied_requests_24h,
                86400.0,
                "24h",
            ),
        ):
            if count < thresholds.velocity_min_sample:
                continue
            forecast = _velocity_forecast(
                count, window_seconds, observation.now, observation.proxied_requests_month
            )
            if forecast > forecast_ceiling:
                return BreakerVerdict(
                    tripped=True,
                    reason=reason,
                    detail=(
                        f"{label} velocity ({count} proxied requests) forecasts "
                        f"{forecast:.0f} by month end, over {forecast_ceiling:.0f} "
                        f"({limit} x {thresholds.velocity_factor})"
                    ),
                )

    # 4. Proxied requests per unique URL — the runaway-loop shape.
    max_per_url = thresholds.max_requests_per_url
    if (
        max_per_url is not None
        and observation.proxied_requests_24h_for_ratio
        >= thresholds.requests_per_url_min_sample
        and observation.distinct_urls_24h > 0
    ):
        ratio = observation.proxied_requests_24h_for_ratio / observation.distinct_urls_24h
        if ratio >= max_per_url:
            return BreakerVerdict(
                tripped=True,
                reason=ProxyBreakerTrip.REQUESTS_PER_URL,
                detail=(
                    f"{observation.proxied_requests_24h_for_ratio} proxied requests over "
                    f"{observation.distinct_urls_24h} distinct urls in 24h = "
                    f"{ratio:.2f}/url, >= {max_per_url}"
                ),
            )

    # 5. Discovery runs per domain per day.
    max_discovery = thresholds.max_discovery_runs_per_domain_per_day
    if (
        max_discovery is not None
        and observation.max_discovery_runs_per_domain_day >= max_discovery
    ):
        return BreakerVerdict(
            tripped=True,
            reason=ProxyBreakerTrip.DISCOVERY_RUNS_PER_DOMAIN,
            detail=(
                f"domain {observation.max_discovery_runs_domain!r} ran "
                f"{observation.max_discovery_runs_per_domain_day} discovery runs in 24h, "
                f">= {max_discovery}"
            ),
        )

    return BreakerVerdict(tripped=False)


def thresholds_from_settings(settings: Any) -> BreakerThresholds:
    """Build :class:`BreakerThresholds` from a ``Settings``-shaped object."""
    return BreakerThresholds(
        monthly_proxied_requests=settings.PROXY_BREAKER_MONTHLY_PROXIED_REQUESTS,
        velocity_factor=settings.PROXY_BREAKER_VELOCITY_FACTOR,
        velocity_min_sample=settings.PROXY_BREAKER_VELOCITY_MIN_SAMPLE,
        max_requests_per_url=settings.PROXY_BREAKER_MAX_REQUESTS_PER_URL,
        requests_per_url_min_sample=settings.PROXY_BREAKER_REQUESTS_PER_URL_MIN_SAMPLE,
        max_discovery_runs_per_domain_per_day=(
            settings.PROXY_BREAKER_MAX_DISCOVERY_RUNS_PER_DOMAIN_PER_DAY
        ),
    )


# --------------------------------------------------------------------------
# 2. Durable measurement
# --------------------------------------------------------------------------


def collect_observation(session: Any, *, now: datetime | None = None) -> BreakerObservation:
    """Aggregate the durable audit tables into a :class:`BreakerObservation`.

    Read-only. Counts only rows whose ``access_method`` is a **proxied**
    one -- the same `PROXY_HTTP`/`PLAYWRIGHT_PROXY` set
    `access/engine._proxy_implied` uses, so "paid" means exactly what it
    means everywhere else in this codebase.

    This is the expensive call in the module (three aggregates, one of
    them a ``COUNT(DISTINCT url)``), which is why it runs behind the
    :func:`evaluate_and_persist` lease and never on the per-request
    path.
    """
    from sqlalchemy import distinct, func, select

    from app_shared.enums import AccessMethod
    from app_shared.models.observations import RequestAttempt
    from app_shared.models.strategy import StrategyDiscoveryRun

    now = now or datetime.now(UTC)
    month_started_at = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    since_1h = now - timedelta(hours=1)
    since_24h = now - timedelta(hours=24)

    paid_methods = [AccessMethod.PROXY_HTTP, AccessMethod.PLAYWRIGHT_PROXY]
    paid = RequestAttempt.access_method.in_(paid_methods)

    month_count = session.execute(
        select(func.count())
        .select_from(RequestAttempt)
        .where(paid, RequestAttempt.created_at >= month_started_at)
    ).scalar_one()

    count_1h = session.execute(
        select(func.count())
        .select_from(RequestAttempt)
        .where(paid, RequestAttempt.created_at >= since_1h)
    ).scalar_one()

    # One pass yields both the 24h count and its distinct-url denominator,
    # so the ratio can never be computed from two inconsistent snapshots.
    count_24h, distinct_urls_24h = session.execute(
        select(func.count(), func.count(distinct(RequestAttempt.url)))
        .select_from(RequestAttempt)
        .where(paid, RequestAttempt.created_at >= since_24h)
    ).one()

    discovery_row = session.execute(
        select(
            StrategyDiscoveryRun.domain,
            func.count().label("runs"),
        )
        .where(StrategyDiscoveryRun.created_at >= since_24h)
        .group_by(StrategyDiscoveryRun.domain)
        .order_by(func.count().desc())
        .limit(1)
    ).first()

    return BreakerObservation(
        now=now,
        month_started_at=month_started_at,
        proxied_requests_month=int(month_count or 0),
        proxied_requests_1h=int(count_1h or 0),
        proxied_requests_24h=int(count_24h or 0),
        proxied_requests_24h_for_ratio=int(count_24h or 0),
        distinct_urls_24h=int(distinct_urls_24h or 0),
        max_discovery_runs_domain=discovery_row[0] if discovery_row else None,
        max_discovery_runs_per_domain_day=int(discovery_row[1]) if discovery_row else 0,
    )


# --------------------------------------------------------------------------
# 3. Durable state read/write
# --------------------------------------------------------------------------


def _get_or_create_row(session: Any, scope_key: str) -> Any:
    from sqlalchemy import select

    from app_shared.models.proxy_breaker import ProxyCircuitBreaker

    row = session.execute(
        select(ProxyCircuitBreaker).where(ProxyCircuitBreaker.scope_key == scope_key)
    ).scalar_one_or_none()
    if row is None:
        row = ProxyCircuitBreaker(scope_key=scope_key, state=ProxyBreakerState.CLOSED)
        session.add(row)
        session.flush()
    return row


def trip_breaker(
    session: Any,
    *,
    reason: ProxyBreakerTrip,
    detail: str,
    observed: dict[str, Any] | None = None,
    scope_key: str = GLOBAL_BREAKER_SCOPE,
    now: datetime | None = None,
) -> None:
    """Open the breaker durably. Idempotent -- re-tripping an already-OPEN
    breaker refreshes the reason/detail but does not bump ``trip_count``
    (so the counter measures distinct incidents, not evaluation passes)."""
    now = now or datetime.now(UTC)
    row = _get_or_create_row(session, scope_key)
    was_closed = row.state is not ProxyBreakerState.OPEN and row.state != "OPEN"
    row.state = ProxyBreakerState.OPEN
    row.trip_reason = reason
    row.detail = detail
    row.observed = observed
    if was_closed:
        row.tripped_at = now
        row.cleared_at = None
        row.trip_count = (row.trip_count or 0) + 1
        logger.error(
            json.dumps(
                {
                    "event": BREAKER_TRIPPED_EVENT,
                    "scope_key": scope_key,
                    "reason": str(reason),
                    "detail": detail,
                    "observed": observed,
                },
                default=str,
            )
        )


def close_breaker(
    session: Any, *, scope_key: str = GLOBAL_BREAKER_SCOPE, now: datetime | None = None
) -> None:
    """Reset the breaker to CLOSED (the manual recovery action)."""
    now = now or datetime.now(UTC)
    row = _get_or_create_row(session, scope_key)
    row.state = ProxyBreakerState.CLOSED
    row.trip_reason = None
    row.detail = None
    row.cleared_at = now


def evaluate_and_persist(
    session: Any,
    *,
    thresholds: BreakerThresholds,
    min_interval_seconds: int = 300,
    scope_key: str = GLOBAL_BREAKER_SCOPE,
    now: datetime | None = None,
) -> BreakerVerdict | None:
    """Take the evaluator lease, measure, evaluate, persist.

    Returns the verdict, or ``None`` when another process holds a fresh
    lease (i.e. this call did no work). The lease is an atomic
    ``UPDATE ... WHERE evaluated_at < cutoff`` -- exactly one caller's
    update matches, so N spiders sharing a database do not all run the
    aggregates.

    **Only ever trips, never auto-closes** (see the module docstring's
    recovery note): a passing evaluation on an OPEN breaker leaves it
    OPEN for a human to clear with :func:`close_breaker`.
    """
    from sqlalchemy import update

    from app_shared.models.proxy_breaker import ProxyCircuitBreaker

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=min_interval_seconds)

    _get_or_create_row(session, scope_key)
    claimed = session.execute(
        update(ProxyCircuitBreaker)
        .where(
            ProxyCircuitBreaker.scope_key == scope_key,
            ProxyCircuitBreaker.evaluated_at < cutoff,
        )
        .values(evaluated_at=now)
        .returning(ProxyCircuitBreaker.id)
    ).first()
    if claimed is None:
        return None

    observation = collect_observation(session, now=now)
    verdict = evaluate_thresholds(observation, thresholds)
    if verdict.tripped and verdict.reason is not None:
        trip_breaker(
            session,
            reason=verdict.reason,
            detail=verdict.detail or "",
            observed=observation.as_dict(),
            scope_key=scope_key,
            now=now,
        )
    return verdict


# --------------------------------------------------------------------------
# 4. Hot-path gate
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _GateCache:
    allowed: bool
    reason: str | None
    fetched_at: float


_gate_cache: _GateCache | None = None
_gate_lock = threading.Lock()


def reset_gate_cache() -> None:
    """Drop the per-process gate cache (tests, fork-safety)."""
    global _gate_cache
    with _gate_lock:
        _gate_cache = None


def paid_requests_allowed(
    session_factory: Any,
    *,
    cache_seconds: int = 30,
    scope_key: str = GLOBAL_BREAKER_SCOPE,
    monotonic: Any = None,
) -> tuple[bool, str | None]:
    """``(allowed, reason)`` for NEW paid (proxied) requests.

    Reads the durable row through a per-process cache no older than
    ``cache_seconds``, so a trip stops paid work fleet-wide within one
    cache generation while costing at most one tiny indexed SELECT per
    generation per process.

    **If the durable state cannot be read at all, paid work is DENIED**
    (`proxy_breaker.unavailable`). That is the whole point of the
    breaker: with Redis blind *and* Postgres unreadable there is no
    surviving accounting anywhere, so authorising more paid probes is
    exactly the unbounded-spend scenario H3 describes. Direct requests
    are unaffected -- this function is only ever consulted for a proxied
    plan.
    """
    import time as _time

    from sqlalchemy import select

    from app_shared.models.proxy_breaker import ProxyCircuitBreaker

    global _gate_cache
    clock = monotonic or _time.monotonic
    nowm = clock()

    cached = _gate_cache
    if cached is not None and (nowm - cached.fetched_at) < cache_seconds:
        return cached.allowed, cached.reason

    try:
        with session_factory() as session:
            row = session.execute(
                select(
                    ProxyCircuitBreaker.state,
                    ProxyCircuitBreaker.trip_reason,
                    ProxyCircuitBreaker.detail,
                ).where(ProxyCircuitBreaker.scope_key == scope_key)
            ).first()
    except Exception:  # noqa: BLE001 - unreadable ledger => deny paid work
        logger.error(
            json.dumps(
                {"event": BREAKER_UNAVAILABLE_EVENT, "scope_key": scope_key},
            )
        )
        # Not cached: a transient DB blip must not pin the fleet closed
        # for a whole cache generation once Postgres recovers.
        return False, "breaker state unreadable"

    if row is None:
        # No row yet = never evaluated = nothing has tripped. Allow.
        result = _GateCache(allowed=True, reason=None, fetched_at=nowm)
    elif row[0] == ProxyBreakerState.OPEN or row[0] == "OPEN":
        result = _GateCache(
            allowed=False,
            reason=f"circuit breaker OPEN ({row[1]}): {row[2]}",
            fetched_at=nowm,
        )
    else:
        result = _GateCache(allowed=True, reason=None, fetched_at=nowm)

    with _gate_lock:
        _gate_cache = result
    return result.allowed, result.reason


def log_denied(*, domain: str, reason: str | None, match_id: Any = None) -> None:
    """Emit the `proxy_breaker.denied` counter for a blocked paid attempt."""
    try:
        logger.error(
            json.dumps(
                {
                    "event": BREAKER_DENIED_EVENT,
                    "domain": domain,
                    "match_id": match_id,
                    "reason": reason,
                },
                default=str,
            )
        )
    except Exception:  # noqa: BLE001 - logging must never break the scrape path
        pass
