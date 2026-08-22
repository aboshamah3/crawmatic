"""Point-in-time operational snapshot (audit 2026-08-15 risk **H5**).

One read-only pass over the durable tables that already exist, producing
a plain, JSON-serialisable :class:`OpsSnapshot`. This is the *measurement*
half of H5; :mod:`app_shared.opsmetrics.rules` is the *judgement* half and
:mod:`app_shared.opsmetrics.emit` is the *delivery* half.

Design constraints, and why they are what they are
--------------------------------------------------

**Read-only, always.** Nothing here writes, creates, or locks. It can be
run against production by an operator with a psql prompt and against the
live database from the API process without any risk of interfering with
a scrape.

**Every section degrades independently.** Production is currently on
migration head ``b7d02a41c9e3`` while the code head is newer, so
``outbox_messages`` and ``proxy_circuit_breakers`` *do not exist in the
deployed database*. A snapshot collector that 500s on a missing table is
useless exactly when the deployment is in the drifted state you most
need to see. Each section is therefore wrapped: a failure yields
``available=False`` plus the exception class name, never an exception
into the caller and never a partial/dishonest number.

**Rate-of-change is computed statelessly.** The audit is explicit that
absolute values alone would not have caught the extra.com incident --
the *rate* was the tell. Rather than introduce a metrics-history table
(a new write path, new retention, new partition), every rate signal is
measured as **current window vs. an immediately preceding window of the
same length** (1h vs previous 1h, 24h vs previous 24h) or **today vs the
trailing 7-day daily mean**. Both comparands come out of the same SQL
pass over the same immutable append-only table, so the ratio can never
be built from two inconsistent snapshots and no state has to survive a
process restart.

**Cross-workspace by construction.** These are fleet aggregates (spend,
per-domain success, queue depth). They contain no tenant rows, no URLs
belonging to an identifiable customer, no credentials -- only counts,
rates and public competitor hostnames. The API surface that exposes them
is guarded by the same cross-workspace service token as ``/v1/admin/*``
(see ``apps/api/app/routers/ops_metrics.py``).

What each section exists to catch
---------------------------------

Four production failures in this cycle were silent. Each has a section
here that would have made it loud:

============================== =============================================
Silent failure                 Section that surfaces it
============================== =============================================
No ``2026_09`` partitions      :class:`PartitionHealth` (``next_month_present``)
Daily rollup has never run     :class:`RollupHealth` (``rows``/``lag_days``)
extra.com discovery runaway    :class:`DomainDiscovery` (``runs_24h`` vs 7d mean)
RLS inert (superuser conns)    :class:`DatabaseRoleHealth`
============================== =============================================

A fifth, found while validating this module against production on
2026-08-15, is covered by :class:`Freshness`: the pipeline had been
completely silent for 2.6 days (last attempt 2026-08-12 21:04Z, zero
``refresh_rules``) and nothing anywhere reported it.
"""

from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass, field
from datetime import UTC, date as date_type, datetime, timedelta
from typing import Any

from app_shared.opsmetrics import cost

#: ``access_method`` values that cost money. Kept identical to
#: ``access/breaker.collect_observation`` and ``access/engine._proxy_implied``
#: so "paid" means one thing across the whole codebase.
PAID_ACCESS_METHODS: tuple[str, ...] = ("PROXY_HTTP", "PLAYWRIGHT_PROXY")

#: SQL expression deriving a comparable hostname from ``request_attempts.url``.
#: Strips scheme, path and a leading ``www.`` -- the same normalisation the
#: 2026-08-12 discovery-leak post-mortem showed was missing upstream (the
#: ``www``-prefix mismatch in rediscovery condition 8, commit ``36fd624``),
#: which is precisely why the dashboard must not repeat that mistake.
_DOMAIN_SQL = (
    "regexp_replace(split_part(split_part(url, '://', 2), '/', 1), '^www\\.', '')"
)


# --------------------------------------------------------------------------
# Section value objects
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PartitionHealth:
    """Whether a partitioned parent has the child partitions it needs.

    ``next_month_present=False`` is a **dated write outage**: every
    INSERT into that table fails the instant the clock rolls into the
    missing month. ``days_until_next_month`` is the fuse length.
    """

    table: str
    exists: bool
    current_month_present: bool
    next_month_present: bool
    partition_count: int
    days_until_next_month: int


@dataclass(frozen=True)
class RollupHealth:
    """Daily-rollup coverage vs. the observations it is meant to summarise.

    ``rows == 0`` with a non-zero ``observation_rows`` is the exact
    2026-08 production state: the rollup has never run, so
    retention-by-drop can never satisfy its verify-before-drop gate and
    no partition will ever be released.
    """

    available: bool
    unavailable_reason: str | None = None
    rows: int = 0
    max_rollup_date: date_type | None = None
    observation_rows: int = 0
    max_observation_at: datetime | None = None
    #: Days between the newest observation's date and the newest rollup
    #: date. ``None`` when either side is empty.
    lag_days: int | None = None
    #: Observations newer than the newest rollup date -- the un-summarised
    #: backlog.
    unrolled_observations: int = 0


@dataclass(frozen=True)
class OutboxHealth:
    """Transactional-outbox backlog (audit H1, durable async delivery)."""

    available: bool
    unavailable_reason: str | None = None
    pending: int = 0
    oldest_pending_age_seconds: float | None = None
    dead: int = 0
    published: int = 0


@dataclass(frozen=True)
class BreakerHealth:
    """Durable proxy circuit-breaker position (audit H3)."""

    available: bool
    unavailable_reason: str | None = None
    scope_key: str | None = None
    state: str | None = None
    trip_reason: str | None = None
    detail: str | None = None
    tripped_at: datetime | None = None
    trip_count: int = 0
    #: Seconds since the evaluator last took its lease. A breaker whose
    #: evaluator has stopped running is a breaker that cannot trip.
    seconds_since_evaluation: float | None = None


@dataclass(frozen=True)
class QueueHealth:
    """Job/target queue depth and age (audit H5's "queue depth and age")."""

    available: bool
    unavailable_reason: str | None = None
    jobs_by_status: dict[str, int] = field(default_factory=dict)
    jobs_pending: int = 0
    jobs_running: int = 0
    oldest_pending_job_age_seconds: float | None = None
    oldest_running_job_age_seconds: float | None = None
    targets_by_status: dict[str, int] = field(default_factory=dict)
    targets_pending: int = 0
    targets_started: int = 0
    targets_deferred: int = 0
    oldest_pending_target_age_seconds: float | None = None
    oldest_started_target_age_seconds: float | None = None
    oldest_deferred_target_age_seconds: float | None = None


@dataclass(frozen=True)
class DomainStats:
    """Per-domain fetch economics over one window.

    ``requests_per_url`` and ``proxied_per_url`` are the audit's two
    named primary cost alarms. ``failed_paid`` is "percent of spend
    producing no observation" in absolute form -- the metric that rises
    long before raw spend looks alarming.

    ``successful_prices`` (Task 2.4, proxy-cost-reduction plan §2.4) is
    the audit's core financial insight made a first-class denominator:
    "cost per attempt is low, but cost per USABLE price can be unbounded
    -- a domain can spend money and produce zero prices." It counts
    ``price_observations`` rows with ``success=True`` attributed to this
    domain via ``match_id -> competitor_product_matches -> competitors
    .domain`` (``price_observations`` carries no ``url``/domain of its
    own -- see ``_collect_domains``). ``proxied`` already IS "proxied
    requests" (paid ``request_attempts``, both origins -- see the module
    docstring and ``PAID_ACCESS_METHODS``); ``proxied_requests`` below is
    an explicit-name alias for readers of this metric specifically.
    """

    domain: str
    attempts: int
    successes: int
    distinct_urls: int
    proxied: int
    failed_paid: int
    successful_prices: int = 0

    @property
    def success_rate(self) -> float | None:
        return None if self.attempts == 0 else self.successes / self.attempts

    @property
    def requests_per_url(self) -> float | None:
        return None if self.distinct_urls == 0 else self.attempts / self.distinct_urls

    @property
    def proxied_per_url(self) -> float | None:
        return None if self.distinct_urls == 0 else self.proxied / self.distinct_urls

    @property
    def wasted_paid_rate(self) -> float | None:
        """Fraction of this domain's paid attempts that produced nothing."""
        return None if self.proxied == 0 else self.failed_paid / self.proxied

    @property
    def estimated_usd(self) -> float:
        return cost.usd(self.proxied)

    @property
    def proxied_requests(self) -> int:
        """Alias for ``proxied``. Paid request attempts, BOTH
        ``RequestOrigin`` values counted (Task 2.3): a discovery probe's
        proxy request costs exactly the same money as a scrape one, so
        spend/volume accounting deliberately does not filter by origin
        (see the module docstring's "Cross-workspace by construction"
        section and ``RequestOrigin``'s own docstring). Named explicitly
        for Task 2.4's cost-per-successful-price metric.
        """
        return self.proxied

    @property
    def estimated_usd_domain_rate(self) -> float:
        """Spend using THIS domain's own $/req (``cost.usd_for_domain``),
        not the fleet-wide average ``estimated_usd`` uses."""
        return cost.usd_for_domain(self.domain, self.proxied_requests)

    @property
    def cost_per_successful_price(self) -> float | None:
        """USD of proxy spend per USABLE price, at this domain's own rate.

        ``None`` when there is no spend at all (nothing to divide --
        this domain never touched the paid proxy in the window).
        ``float('inf')`` when there IS spend but zero successful prices
        -- the audit's named "cost per usable price can be unbounded"
        case. That is a real, alertable state, not a divide error (same
        semantics as ``_ratio`` elsewhere in this module).
        """
        if self.proxied_requests == 0:
            return None
        if self.successful_prices == 0:
            return float("inf")
        return self.estimated_usd_domain_rate / self.successful_prices

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.update(
            success_rate=_round(self.success_rate, 4),
            requests_per_url=_round(self.requests_per_url, 3),
            proxied_per_url=_round(self.proxied_per_url, 3),
            wasted_paid_rate=_round(self.wasted_paid_rate, 4),
            estimated_usd=self.estimated_usd,
            proxied_requests=self.proxied_requests,
            estimated_usd_domain_rate=self.estimated_usd_domain_rate,
            cost_per_successful_price=_round(self.cost_per_successful_price, 6),
        )
        return out


@dataclass(frozen=True)
class DomainDiscovery:
    """Strategy-discovery volume per domain, with its own baseline.

    The rate-of-change comparand is ``mean_daily_runs_7d``: a domain
    doing 7,186 runs today against a 7-day mean of 1,300 is a runaway
    even if 7,186 is under any absolute ceiling you happened to pick.
    """

    domain: str
    runs_24h: int
    runs_7d: int

    @property
    def runs_prev_6d(self) -> int:
        """Runs in the 6 days *before* the trailing 24h."""
        return max(0, self.runs_7d - self.runs_24h)

    @property
    def mean_daily_runs_7d(self) -> float:
        """Baseline: the daily mean over the 6 days preceding today.

        Deliberately **excludes** the trailing 24h. Dividing by the full
        7-day mean would put today's runs inside their own baseline and
        cap ``surge_factor`` at exactly 7.0 (reached when the entire
        week's activity happened today) -- so a 100x runaway and a 7x
        drift would report the same number, which is precisely the
        signal a rate-of-change rule exists to distinguish. Found by
        backtesting this rule against the 2026-08-12 extra.com incident,
        where every surging domain reported an identical 7.0x.
        """
        return self.runs_prev_6d / 6.0

    @property
    def surge_factor(self) -> float | None:
        """``runs_24h`` over the preceding 6 days' daily mean.

        ``None`` when there is no activity at all; ``inf`` when a domain
        that ran nothing for six days suddenly runs discovery today --
        which is a real, alertable transition, not a divide error.
        """
        mean = self.mean_daily_runs_7d
        if mean <= 0:
            return None if self.runs_24h == 0 else float("inf")
        return self.runs_24h / mean

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.update(
            runs_prev_6d=self.runs_prev_6d,
            mean_daily_runs_7d=_round(self.mean_daily_runs_7d, 2),
            surge_factor=_round(self.surge_factor, 2),
        )
        return out


@dataclass(frozen=True)
class SpendVelocity:
    """Proxy spend, its trailing rates, and the month-end forecast.

    Counted in *proxied request attempts* (the only unit this codebase
    can measure itself) and converted with
    :data:`app_shared.opsmetrics.cost.USD_PER_PROXIED_REQUEST`.

    ``forecast_month_end_*`` extrapolates the trailing window's rate over
    the remaining seconds in the calendar month and adds month-to-date --
    the same arithmetic ``access/breaker._velocity_forecast`` trips on,
    reproduced here so the dashboard and the breaker never disagree
    about what "on course to blow the budget" means.
    """

    available: bool
    unavailable_reason: str | None = None
    proxied_month_to_date: int = 0
    proxied_1h: int = 0
    proxied_prev_1h: int = 0
    proxied_24h: int = 0
    proxied_prev_24h: int = 0
    attempts_month_to_date: int = 0
    seconds_remaining_in_month: float = 0.0
    #: The circuit breaker's configured monthly ceiling
    #: (``PROXY_BREAKER_MONTHLY_PROXIED_REQUESTS``).
    ceiling_proxied_requests: int | None = None
    #: Sum of ``monthly_budget_limit`` over ACTIVE proxy providers -- the
    #: ceiling the Redis budget ledger *actually enforces* per request.
    #: Reported separately because the two are configured independently
    #: and production had them an order of magnitude apart (breaker
    #: 250,000 vs provider 60,000 on 2026-08-15): reporting only the
    #: larger one would have shown 8% of budget consumed when the
    #: enforced figure was 35%.
    provider_budget_limit: int | None = None
    providers_without_budget: int = 0

    @property
    def pct_of_provider_budget(self) -> float | None:
        if not self.provider_budget_limit:
            return None
        return self.proxied_month_to_date / self.provider_budget_limit

    @property
    def effective_ceiling(self) -> int | None:
        """The lower of the two ceilings — the one that binds first."""
        candidates = [
            c
            for c in (self.ceiling_proxied_requests, self.provider_budget_limit)
            if c
        ]
        return min(candidates) if candidates else None

    @property
    def usd_month_to_date(self) -> float:
        return cost.usd(self.proxied_month_to_date)

    @property
    def forecast_month_end_1h(self) -> float:
        return self._forecast(self.proxied_1h, 3600.0)

    @property
    def forecast_month_end_24h(self) -> float:
        return self._forecast(self.proxied_24h, 86400.0)

    def _forecast(self, count: int, window_seconds: float) -> float:
        rate = count / window_seconds
        return self.proxied_month_to_date + rate * self.seconds_remaining_in_month

    @property
    def acceleration_1h(self) -> float | None:
        """Trailing hour vs the hour before it. ``None`` if both idle."""
        return _ratio(self.proxied_1h, self.proxied_prev_1h)

    @property
    def acceleration_24h(self) -> float | None:
        return _ratio(self.proxied_24h, self.proxied_prev_24h)

    @property
    def pct_of_ceiling(self) -> float | None:
        if not self.ceiling_proxied_requests:
            return None
        return self.proxied_month_to_date / self.ceiling_proxied_requests

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.update(
            usd_month_to_date=self.usd_month_to_date,
            usd_fixed_monthly=cost.USD_FIXED_MONTHLY,
            forecast_month_end_proxied_1h=_round(self.forecast_month_end_1h, 0),
            forecast_month_end_proxied_24h=_round(self.forecast_month_end_24h, 0),
            forecast_month_end_usd_24h=cost.usd(self.forecast_month_end_24h),
            acceleration_1h=_round(self.acceleration_1h, 2),
            acceleration_24h=_round(self.acceleration_24h, 2),
            pct_of_ceiling=_round(self.pct_of_ceiling, 4),
            pct_of_provider_budget=_round(self.pct_of_provider_budget, 4),
            effective_ceiling=self.effective_ceiling,
        )
        return out


@dataclass(frozen=True)
class Freshness:
    """When the pipeline last did anything at all.

    Added after this module's own production validation found the
    pipeline silent for 2.6 days with zero ``refresh_rules`` rows and no
    alarm anywhere. A monitoring system that only watches for *bad*
    numbers cannot see a system that has stopped producing numbers.
    """

    available: bool
    unavailable_reason: str | None = None
    last_attempt_at: datetime | None = None
    last_observation_at: datetime | None = None
    seconds_since_attempt: float | None = None
    seconds_since_observation: float | None = None
    #: Scheduled-refresh rules that exist at all. Zero means nothing is
    #: ever scraped on a schedule, whatever else the dashboard says.
    refresh_rules_total: int = 0
    refresh_rules_enabled: int = 0


@dataclass(frozen=True)
class OptimizerChurn:
    """Strategy-profile rewrite rate (audit H5's "optimizer churn").

    A profile set that rewrites itself constantly is either chasing noise
    or fighting an operator's manual edit -- the documented
    ``learned-profile-overrides-policy`` failure mode.
    """

    available: bool
    unavailable_reason: str | None = None
    profiles_total: int = 0
    changed_24h: int = 0
    changed_7d: int = 0

    @property
    def churn_rate_24h(self) -> float | None:
        if self.profiles_total == 0:
            return None
        return self.changed_24h / self.profiles_total


@dataclass(frozen=True)
class RedisHealth:
    """Redis posture (audit H2).

    ``policy_status``/``policy`` come from
    :func:`app_shared.redis_policy.last_report` -- the cached result of
    the boot-time probe, exported for exactly this purpose, so the
    endpoint never re-probes. ``used_memory``/``evicted_keys`` come from
    an optional ``INFO`` call when a client is supplied.
    """

    available: bool
    unavailable_reason: str | None = None
    policy_status: str | None = None
    policy: str | None = None
    maxmemory: int | None = None
    policy_detail: str | None = None
    used_memory: int | None = None
    maxmemory_from_info: int | None = None
    evicted_keys: int | None = None
    connected_clients: int | None = None

    @property
    def memory_utilisation(self) -> float | None:
        limit = self.maxmemory_from_info or self.maxmemory
        if not limit or self.used_memory is None:
            return None
        return self.used_memory / limit


@dataclass(frozen=True)
class ScrapydNodeHealth:
    """One node's own ``daemonstatus.json`` (T8 — queued vs. stalled per node).

    ``available=False`` + ``unavailable_reason`` is what a probe returning
    ``None`` becomes — ``ScrapydDispatchClient.daemon_status`` already
    collapses every transport/HTTP/parse error into ``None`` (T6), and this
    module refuses to turn that ``None`` into fake zeros: an unreachable
    node must never read as an idle, healthy one.
    """

    node_url: str
    available: bool
    unavailable_reason: str | None = None
    status: str | None = None
    pending: int | None = None
    running: int | None = None
    finished: int | None = None


@dataclass(frozen=True)
class ScrapydHealth:
    """Scrapyd saturation.

    Two views. ``pending``/``running``/``finished`` are the daemon's own
    numbers — the fleet-wide sum across ``nodes`` — and require an injected
    probe (Scrapyd is an HTTP daemon on the private network; the API does
    not otherwise talk to it, and this module refuses to open a network
    connection of its own). ``nodes`` carries the same numbers per node, so
    a queue backed up on one node is distinguishable from the fleet. The
    DB-derived ``in_flight_targets`` is always available and measures the
    *effect* of saturation without any network call.

    The aggregate ``pending``/``running``/``finished`` are ``None`` (not
    ``0``) whenever no node's probe succeeded — summing zero real numbers is
    not the same claim as "nothing pending".
    """

    available: bool
    unavailable_reason: str | None = None
    pending: int | None = None
    running: int | None = None
    finished: int | None = None
    in_flight_targets: int = 0
    nodes: tuple[ScrapydNodeHealth, ...] = ()


@dataclass(frozen=True)
class DatabaseRoleHealth:
    """Whether the connected role can actually be constrained by RLS.

    Audit **C3**: RLS is inert in production because every service
    connects as a superuser that bypasses it. A superuser/``BYPASSRLS``
    connection makes every workspace-isolation policy in the schema
    decorative, and nothing in the running system says so out loud.
    """

    available: bool
    unavailable_reason: str | None = None
    role: str | None = None
    is_superuser: bool | None = None
    bypasses_rls: bool | None = None

    @property
    def rls_effective(self) -> bool | None:
        if self.is_superuser is None or self.bypasses_rls is None:
            return None
        return not (self.is_superuser or self.bypasses_rls)


@dataclass(frozen=True)
class OpsSnapshot:
    """Everything H5 asks for, in one JSON-serialisable object."""

    collected_at: datetime
    partitions: tuple[PartitionHealth, ...] = ()
    partitions_available: bool = True
    partitions_unavailable_reason: str | None = None
    rollups: RollupHealth = field(default_factory=lambda: RollupHealth(available=False))
    outbox: OutboxHealth = field(default_factory=lambda: OutboxHealth(available=False))
    breaker: BreakerHealth = field(default_factory=lambda: BreakerHealth(available=False))
    queue: QueueHealth = field(default_factory=lambda: QueueHealth(available=False))
    domains_24h: tuple[DomainStats, ...] = ()
    domains_7d: tuple[DomainStats, ...] = ()
    domains_available: bool = True
    domains_unavailable_reason: str | None = None
    discovery: tuple[DomainDiscovery, ...] = ()
    discovery_available: bool = True
    discovery_unavailable_reason: str | None = None
    spend: SpendVelocity = field(default_factory=lambda: SpendVelocity(available=False))
    freshness: Freshness = field(default_factory=lambda: Freshness(available=False))
    optimizer: OptimizerChurn = field(
        default_factory=lambda: OptimizerChurn(available=False)
    )
    redis: RedisHealth = field(default_factory=lambda: RedisHealth(available=False))
    scrapyd: ScrapydHealth = field(default_factory=lambda: ScrapydHealth(available=False))
    db_role: DatabaseRoleHealth = field(
        default_factory=lambda: DatabaseRoleHealth(available=False)
    )

    def as_dict(self) -> dict[str, Any]:
        """Plain JSON-serialisable dict (datetimes as ISO-8601 strings)."""
        return _jsonify(
            {
                "collected_at": self.collected_at,
                "partitions": {
                    "available": self.partitions_available,
                    "unavailable_reason": self.partitions_unavailable_reason,
                    "tables": [asdict(p) for p in self.partitions],
                },
                "rollups": _with_props(self.rollups),
                "outbox": _with_props(self.outbox),
                "breaker": _with_props(self.breaker),
                "queue": _with_props(self.queue),
                "domains": {
                    "available": self.domains_available,
                    "unavailable_reason": self.domains_unavailable_reason,
                    "window_24h": [d.as_dict() for d in self.domains_24h],
                    "window_7d": [d.as_dict() for d in self.domains_7d],
                },
                "discovery": {
                    "available": self.discovery_available,
                    "unavailable_reason": self.discovery_unavailable_reason,
                    "domains": [d.as_dict() for d in self.discovery],
                },
                "spend": self.spend.as_dict(),
                "freshness": _with_props(self.freshness),
                "optimizer": _with_props(self.optimizer, churn_rate_24h=4),
                "redis": _with_props(self.redis, memory_utilisation=4),
                "scrapyd": _with_props(self.scrapyd),
                "db_role": _with_props(self.db_role),
            }
        )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _round(value: float | None, digits: int) -> float | str | None:
    """Round, mapping the infinities to the STRING ``"inf"``/``"-inf"``.

    ``float('inf')`` is a legitimate value here (see :meth:`_ratio`) but
    ``json.dumps`` renders it as the bare token ``Infinity``, which is
    not valid JSON and which strict parsers -- including most alert
    pipelines -- reject outright. Stringifying at the serialisation
    boundary keeps the signal and keeps the document parseable.
    """
    if value is None:
        return None
    if value == float("inf"):
        return "inf"
    if value == float("-inf"):
        return "-inf"
    return round(value, digits)


def _ratio(current: int, previous: int) -> float | None:
    """``current/previous``, with a defined answer when ``previous`` is 0.

    Returns ``None`` when both windows are empty (no signal), and
    ``float('inf')`` when work appeared out of a genuinely idle previous
    window -- which is a real, alertable transition, not a divide error.
    """
    if previous == 0:
        return None if current == 0 else float("inf")
    return current / previous


def _with_props(obj: Any, **rounding: int) -> dict[str, Any]:
    """``asdict`` plus every public ``@property`` on the dataclass.

    Properties carry the derived signals (``success_rate``,
    ``rls_effective``, ...) and would otherwise be silently dropped by
    ``asdict``.
    """
    out = asdict(obj)
    for name in dir(type(obj)):
        if name.startswith("_"):
            continue
        attr = getattr(type(obj), name, None)
        if isinstance(attr, property):
            value = getattr(obj, name)
            digits = rounding.get(name)
            out[name] = _round(value, digits) if digits is not None else value
    return out


def _jsonify(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if isinstance(value, (datetime, date_type)):
        return value.isoformat()
    if value == float("inf"):
        return "inf"
    return value


def seconds_remaining_in_month(now: datetime) -> float:
    """Seconds from ``now`` to the first instant of the next month."""
    last_day = calendar.monthrange(now.year, now.month)[1]
    month_end = now.replace(
        day=last_day, hour=23, minute=59, second=59, microsecond=999999
    ) + timedelta(microseconds=1)
    return max(0.0, (month_end - now).total_seconds())


def _age(now: datetime, moment: datetime | None) -> float | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (now - moment).total_seconds()


def _section(collector, fallback, session: Any = None):
    """Run ``collector``; on ANY failure return ``fallback(reason)``.

    See the module docstring: a snapshot must survive a database that is
    behind the code (missing tables), a permissions quirk, or a table
    that a future migration renames. Reporting *which* section is blind
    is strictly more useful than a 500.

    **The rollback is load-bearing, not defensive tidying.** Postgres puts
    a transaction into a failed state after *any* error, and every
    subsequent statement on that connection returns
    ``InFailedSqlTransaction``. Without the rollback, one genuinely
    missing table (production on 2026-08-15 has no ``outbox_messages``:
    the deployed migration head is behind the code) would cascade and
    blind every later section, reporting eight fake failures for one real
    one. Found by running this collector against production before
    shipping it.
    """
    try:
        return collector()
    except Exception as exc:  # noqa: BLE001 - deliberate: see docstring
        reason = f"{exc.__class__.__name__}: {exc}"[:300]
        if session is not None:
            try:
                session.rollback()
            except Exception:  # noqa: BLE001 - nothing left to salvage
                pass
        return fallback(reason)


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


def collect_snapshot(
    session: Any,
    *,
    now: datetime | None = None,
    redis: Any | None = None,
    scrapyd_status: dict[str, dict[str, Any] | None] | None = None,
    settings: Any | None = None,
) -> OpsSnapshot:
    """Collect the full snapshot. Read-only; never raises.

    Args:
        session: any SQLAlchemy-``Session``-shaped object. For the fleet
            aggregates to be complete it must be a BYPASSRLS/unscoped
            session (see ``apps/api/app/routers/ops_metrics.py``).
        now: injectable clock for tests.
        redis: optional ``redis.Redis``-shaped client. When supplied,
            ``INFO memory``/``INFO stats`` fill in memory and eviction
            counters. When omitted, the Redis section still reports the
            cached ``redis_policy.last_report()`` posture.
        scrapyd_status: optional mapping of ``node_url`` to that node's
            pre-fetched ``daemonstatus.json`` body, or ``None`` for a node
            whose probe failed (T6's ``ScrapydDispatchClient.daemon_status``
            contract). This module never opens a network connection itself
            — the caller iterates ``SCRAPYD_HTTP_URLS + SCRAPYD_BROWSER_URLS``
            and probes each node before calling in.
        settings: optional ``Settings``-shaped object, used only for the
            breaker's configured ceiling.
    """
    now = now or datetime.now(UTC)

    partitions, part_ok, part_reason = _collect_partitions(session, now)
    domains_24h, domains_7d, dom_ok, dom_reason = _collect_domains(session, now)
    discovery, disc_ok, disc_reason = _collect_discovery(session, now)

    return OpsSnapshot(
        collected_at=now,
        partitions=partitions,
        partitions_available=part_ok,
        partitions_unavailable_reason=part_reason,
        rollups=_section(
            lambda: _collect_rollups(session),
            lambda r: RollupHealth(available=False, unavailable_reason=r),
            session,
        ),
        outbox=_section(
            lambda: _collect_outbox(session, now),
            lambda r: OutboxHealth(available=False, unavailable_reason=r),
            session,
        ),
        breaker=_section(
            lambda: _collect_breaker(session, now),
            lambda r: BreakerHealth(available=False, unavailable_reason=r),
            session,
        ),
        queue=_section(
            lambda: _collect_queue(session, now),
            lambda r: QueueHealth(available=False, unavailable_reason=r),
            session,
        ),
        domains_24h=domains_24h,
        domains_7d=domains_7d,
        domains_available=dom_ok,
        domains_unavailable_reason=dom_reason,
        discovery=discovery,
        discovery_available=disc_ok,
        discovery_unavailable_reason=disc_reason,
        spend=_section(
            lambda: _collect_spend(session, now, settings),
            lambda r: SpendVelocity(available=False, unavailable_reason=r),
            session,
        ),
        freshness=_section(
            lambda: _collect_freshness(session, now),
            lambda r: Freshness(available=False, unavailable_reason=r),
            session,
        ),
        optimizer=_section(
            lambda: _collect_optimizer(session, now),
            lambda r: OptimizerChurn(available=False, unavailable_reason=r),
            session,
        ),
        redis=_section(
            lambda: _collect_redis(redis),
            lambda r: RedisHealth(available=False, unavailable_reason=r),
        ),
        scrapyd=_section(
            lambda: _collect_scrapyd(session, scrapyd_status),
            lambda r: ScrapydHealth(available=False, unavailable_reason=r),
            session,
        ),
        db_role=_section(
            lambda: _collect_db_role(session),
            lambda r: DatabaseRoleHealth(available=False, unavailable_reason=r),
            session,
        ),
    )


def _collect_partitions(
    session: Any, now: datetime
) -> tuple[tuple[PartitionHealth, ...], bool, str | None]:
    """Existence of the current + next month child partition per parent.

    Reuses ``app_shared.maintenance.partitions`` rather than
    re-implementing catalog reads: ``table_exists`` (``to_regclass``, so a
    registered-but-absent parent like ``webhook_events`` is skipped
    cleanly) and ``existing_partitions``/``partition_name``. The
    partition-*creation* job is owned elsewhere; this only observes it.
    """
    try:
        from app_shared.maintenance.partitions import (
            existing_partitions,
            month_partition_bounds,
            partition_name,
            table_exists,
        )
        from app_shared.maintenance.registry import PARTITIONED_TABLES
    except Exception as exc:  # noqa: BLE001
        return (), False, f"{exc.__class__.__name__}: {exc}"[:300]

    # `month_partition_bounds(now, offset)` is the SAME helper the
    # partition-creation job uses, so "the partition we expect" is
    # defined in exactly one place and the Dec->Jan rollover is handled
    # by already-tested code. `partition_name` takes the `YYYY_MM`
    # *suffix* string, not a datetime -- passing a datetime silently
    # yields a name that matches nothing, which is how an early version
    # of this function reported all four August partitions missing while
    # looking at a database that had them. Caught against production.
    current_suffix, _, next_month_start = month_partition_bounds(now, 0)
    next_suffix, _, _ = month_partition_bounds(now, 1)
    fuse_days = max(0, (next_month_start - now).days)

    out: list[PartitionHealth] = []
    for entry in PARTITIONED_TABLES:
        try:
            if not table_exists(session, entry.name):
                out.append(
                    PartitionHealth(
                        table=entry.name,
                        exists=False,
                        current_month_present=False,
                        next_month_present=False,
                        partition_count=0,
                        days_until_next_month=fuse_days,
                    )
                )
                continue
            names = {p.name for p in existing_partitions(session, entry.name)}
            out.append(
                PartitionHealth(
                    table=entry.name,
                    exists=True,
                    current_month_present=(
                        partition_name(entry.name, current_suffix) in names
                    ),
                    next_month_present=(
                        partition_name(entry.name, next_suffix) in names
                    ),
                    partition_count=len(names),
                    days_until_next_month=fuse_days,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad table must not blind the rest
            try:  # see `_section`: an aborted transaction poisons every later query
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
            return tuple(out), False, f"{entry.name}: {exc.__class__.__name__}"[:300]
    return tuple(out), True, None


def _collect_rollups(session: Any) -> RollupHealth:
    from sqlalchemy import func, select

    from app_shared.models.observations import PriceObservation
    from app_shared.models.rollups import VariantPriceDailyRollup

    rows, max_date = session.execute(
        select(func.count(), func.max(VariantPriceDailyRollup.date))
    ).one()
    obs_rows, max_obs = session.execute(
        select(func.count(), func.max(PriceObservation.scraped_at)).select_from(
            PriceObservation
        )
    ).one()

    lag_days: int | None = None
    unrolled = int(obs_rows or 0)
    if max_obs is not None and max_date is not None:
        lag_days = (max_obs.date() - max_date).days
        unrolled = int(
            session.execute(
                select(func.count())
                .select_from(PriceObservation)
                .where(func.date(PriceObservation.scraped_at) > max_date)
            ).scalar_one()
            or 0
        )

    return RollupHealth(
        available=True,
        rows=int(rows or 0),
        max_rollup_date=max_date,
        observation_rows=int(obs_rows or 0),
        max_observation_at=max_obs,
        lag_days=lag_days,
        unrolled_observations=unrolled,
    )


def _collect_outbox(session: Any, now: datetime) -> OutboxHealth:
    from sqlalchemy import func, select

    from app_shared.models.outbox import OutboxMessage

    counts: dict[str, int] = {}
    for status, count in session.execute(
        select(OutboxMessage.status, func.count()).group_by(OutboxMessage.status)
    ).all():
        counts[str(status)] = int(count)

    oldest = session.execute(
        select(func.min(OutboxMessage.created_at)).where(
            OutboxMessage.status == "PENDING"
        )
    ).scalar_one_or_none()

    return OutboxHealth(
        available=True,
        pending=counts.get("PENDING", 0),
        oldest_pending_age_seconds=_age(now, oldest),
        dead=counts.get("DEAD", 0),
        published=counts.get("PUBLISHED", 0),
    )


def _collect_breaker(session: Any, now: datetime) -> BreakerHealth:
    from sqlalchemy import select

    from app_shared.models.proxy_breaker import GLOBAL_BREAKER_SCOPE, ProxyCircuitBreaker

    row = session.execute(
        select(ProxyCircuitBreaker).where(
            ProxyCircuitBreaker.scope_key == GLOBAL_BREAKER_SCOPE
        )
    ).scalar_one_or_none()
    if row is None:
        # Table exists but the evaluator has never run. Deliberately NOT
        # created here -- this module is read-only.
        return BreakerHealth(
            available=True,
            scope_key=GLOBAL_BREAKER_SCOPE,
            state=None,
            unavailable_reason="no breaker row: evaluator has never run",
        )
    return BreakerHealth(
        available=True,
        scope_key=row.scope_key,
        state=str(row.state) if row.state is not None else None,
        trip_reason=str(row.trip_reason) if row.trip_reason is not None else None,
        detail=row.detail,
        tripped_at=row.tripped_at,
        trip_count=int(row.trip_count or 0),
        seconds_since_evaluation=_age(now, row.evaluated_at),
    )


def _collect_queue(session: Any, now: datetime) -> QueueHealth:
    from sqlalchemy import func, select

    from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget

    jobs_by_status: dict[str, int] = {}
    job_oldest: dict[str, datetime] = {}
    for status, count, oldest in session.execute(
        select(ScrapeJob.status, func.count(), func.min(ScrapeJob.created_at)).group_by(
            ScrapeJob.status
        )
    ).all():
        jobs_by_status[str(status)] = int(count)
        if oldest is not None:
            job_oldest[str(status)] = oldest

    targets_by_status: dict[str, int] = {}
    target_oldest: dict[str, datetime] = {}
    target_oldest_started: dict[str, datetime] = {}
    for status, count, oldest, oldest_started in session.execute(
        select(
            ScrapeJobTarget.status,
            func.count(),
            func.min(ScrapeJobTarget.created_at),
            func.min(ScrapeJobTarget.started_at),
        ).group_by(ScrapeJobTarget.status)
    ).all():
        key = str(status)
        targets_by_status[key] = int(count)
        if oldest is not None:
            target_oldest[key] = oldest
        if oldest_started is not None:
            target_oldest_started[key] = oldest_started

    return QueueHealth(
        available=True,
        jobs_by_status=jobs_by_status,
        jobs_pending=jobs_by_status.get("PENDING", 0),
        jobs_running=jobs_by_status.get("RUNNING", 0),
        oldest_pending_job_age_seconds=_age(now, job_oldest.get("PENDING")),
        oldest_running_job_age_seconds=_age(now, job_oldest.get("RUNNING")),
        targets_by_status=targets_by_status,
        targets_pending=targets_by_status.get("PENDING", 0),
        targets_started=targets_by_status.get("STARTED", 0),
        targets_deferred=targets_by_status.get("DEFERRED", 0),
        oldest_pending_target_age_seconds=_age(now, target_oldest.get("PENDING")),
        oldest_started_target_age_seconds=_age(
            now, target_oldest_started.get("STARTED")
        ),
        oldest_deferred_target_age_seconds=_age(now, target_oldest.get("DEFERRED")),
    )


#: One aggregate per domain per window. Raw SQL (not the ORM) because the
#: hostname has to be derived from ``url`` with ``regexp_replace`` --
#: ``request_attempts`` has no ``domain`` column. Postgres-only, which
#: ``request_attempts`` already is (declarative RANGE partitioning).
_DOMAIN_AGG_SQL = f"""
SELECT {_DOMAIN_SQL} AS domain,
       count(*)                                              AS attempts,
       count(*) FILTER (WHERE success)                       AS successes,
       count(DISTINCT url)                                   AS distinct_urls,
       count(*) FILTER (WHERE access_method = ANY(:paid))     AS proxied,
       count(*) FILTER (WHERE NOT success
                          AND access_method = ANY(:paid))     AS failed_paid
FROM request_attempts
WHERE created_at >= :since
GROUP BY 1
ORDER BY attempts DESC
LIMIT :limit
"""


#: Successful ``price_observations`` per domain (Task 2.4). Unlike
#: ``request_attempts``, ``price_observations`` carries no ``url`` --
#: domain attribution comes from ``match_id -> competitor_product_matches
#: -> competitors.domain`` (same join ``apps/api/app/services
#: /admin_usage.py`` uses for product attribution: "request_attempts has
#: match_id, not product_id -- product attribution comes from joining
#: competitor_product_matches"). Both joins are on primary-key /
#: unique-indexed columns (``cpm.id``, ``competitors.id``); the only
#: predicate is the bounded range on ``scraped_at`` -- the partition key
#: -- so this stays SARGABLE and prunes to the touched monthly
#: partition(s) exactly like ``_DOMAIN_AGG_SQL`` does on ``created_at``.
_SUCCESSFUL_PRICES_SQL = """
SELECT c.domain AS domain,
       count(*) FILTER (WHERE po.success) AS successful_prices
FROM price_observations po
JOIN competitor_product_matches cpm
  ON cpm.workspace_id = po.workspace_id AND cpm.id = po.match_id
JOIN competitors c
  ON c.workspace_id = cpm.workspace_id AND c.id = cpm.competitor_id
WHERE po.scraped_at >= :since
GROUP BY 1
"""


def _collect_domains(
    session: Any, now: datetime, *, limit: int = 50
) -> tuple[tuple[DomainStats, ...], tuple[DomainStats, ...], bool, str | None]:
    from sqlalchemy import text

    def run(since: datetime) -> tuple[DomainStats, ...]:
        rows = session.execute(
            text(_DOMAIN_AGG_SQL),
            {"since": since, "paid": list(PAID_ACCESS_METHODS), "limit": limit},
        ).all()
        # Second, independent pass rather than folding into one query:
        # `_DOMAIN_AGG_SQL` groups `request_attempts` (LIMIT'd, ordered
        # by attempts); `price_observations` has no row-count relation
        # to that at all, and a single query joining both would force a
        # domain into the top-`limit` request_attempts set to see its
        # prices, hiding the exact "spend but zero prices" case Task 2.4
        # exists to surface (a domain can be blocked/failing so hard it
        # falls out of the attempts ranking while still burning proxy
        # spend). A dict lookup by domain name merges the two safely.
        successful_by_domain: dict[str, int] = {
            (r[0] or "(unparsed)"): int(r[1])
            for r in session.execute(text(_SUCCESSFUL_PRICES_SQL), {"since": since}).all()
        }
        return tuple(
            DomainStats(
                domain=r[0] or "(unparsed)",
                attempts=int(r[1]),
                successes=int(r[2]),
                distinct_urls=int(r[3]),
                proxied=int(r[4]),
                failed_paid=int(r[5]),
                successful_prices=successful_by_domain.get(r[0] or "(unparsed)", 0),
            )
            for r in rows
        )

    try:
        return run(now - timedelta(hours=24)), run(now - timedelta(days=7)), True, None
    except Exception as exc:  # noqa: BLE001
        return (), (), False, f"{exc.__class__.__name__}: {exc}"[:300]


def _collect_discovery(
    session: Any, now: datetime, *, limit: int = 50
) -> tuple[tuple[DomainDiscovery, ...], bool, str | None]:
    from sqlalchemy import func, select

    from app_shared.models.strategy import StrategyDiscoveryRun

    try:
        rows = session.execute(
            select(
                StrategyDiscoveryRun.domain,
                func.count().filter(
                    StrategyDiscoveryRun.created_at >= now - timedelta(hours=24)
                ),
                func.count(),
            )
            .where(StrategyDiscoveryRun.created_at >= now - timedelta(days=7))
            .group_by(StrategyDiscoveryRun.domain)
            .order_by(func.count().desc())
            .limit(limit)
        ).all()
    except Exception as exc:  # noqa: BLE001
        return (), False, f"{exc.__class__.__name__}: {exc}"[:300]

    return (
        tuple(
            DomainDiscovery(domain=r[0], runs_24h=int(r[1] or 0), runs_7d=int(r[2] or 0))
            for r in rows
        ),
        True,
        None,
    )


def _collect_spend(session: Any, now: datetime, settings: Any | None) -> SpendVelocity:
    from sqlalchemy import func, select

    from app_shared.enums import AccessMethod
    from app_shared.models.observations import RequestAttempt

    paid = RequestAttempt.access_method.in_(
        [AccessMethod.PROXY_HTTP, AccessMethod.PLAYWRIGHT_PROXY]
    )
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def count(*where) -> int:
        return int(
            session.execute(
                select(func.count()).select_from(RequestAttempt).where(*where)
            ).scalar_one()
            or 0
        )

    h1, h24 = now - timedelta(hours=1), now - timedelta(hours=24)
    h2, h48 = now - timedelta(hours=2), now - timedelta(hours=48)

    ceiling = None
    if settings is not None:
        ceiling = getattr(settings, "PROXY_BREAKER_MONTHLY_PROXIED_REQUESTS", None)

    # The ledger's real cap. Best-effort: an older/newer schema must not
    # blind the whole spend section over a nice-to-have.
    provider_limit: int | None = None
    providers_without_budget = 0
    try:
        from app_shared.models.access import ProxyProvider

        rows = session.execute(
            select(
                func.sum(ProxyProvider.monthly_budget_limit),
                func.count().filter(ProxyProvider.monthly_budget_limit.is_(None)),
            ).where(ProxyProvider.status == "ACTIVE")
        ).one()
        provider_limit = int(rows[0]) if rows[0] is not None else None
        providers_without_budget = int(rows[1] or 0)
    except Exception:  # noqa: BLE001 - see comment above
        session.rollback()

    return SpendVelocity(
        available=True,
        proxied_month_to_date=count(paid, RequestAttempt.created_at >= month_start),
        proxied_1h=count(paid, RequestAttempt.created_at >= h1),
        proxied_prev_1h=count(
            paid, RequestAttempt.created_at >= h2, RequestAttempt.created_at < h1
        ),
        proxied_24h=count(paid, RequestAttempt.created_at >= h24),
        proxied_prev_24h=count(
            paid, RequestAttempt.created_at >= h48, RequestAttempt.created_at < h24
        ),
        attempts_month_to_date=count(RequestAttempt.created_at >= month_start),
        seconds_remaining_in_month=seconds_remaining_in_month(now),
        ceiling_proxied_requests=ceiling,
        provider_budget_limit=provider_limit,
        providers_without_budget=providers_without_budget,
    )


def _collect_freshness(session: Any, now: datetime) -> Freshness:
    from sqlalchemy import func, select

    from app_shared.models.observations import PriceObservation, RequestAttempt
    from app_shared.models.refresh_rules import RefreshRule

    last_attempt = session.execute(
        select(func.max(RequestAttempt.created_at))
    ).scalar_one_or_none()
    last_obs = session.execute(
        select(func.max(PriceObservation.scraped_at))
    ).scalar_one_or_none()

    total = int(
        session.execute(select(func.count()).select_from(RefreshRule)).scalar_one() or 0
    )
    enabled = total
    if hasattr(RefreshRule, "enabled"):
        enabled = int(
            session.execute(
                select(func.count()).select_from(RefreshRule).where(RefreshRule.enabled)
            ).scalar_one()
            or 0
        )

    return Freshness(
        available=True,
        last_attempt_at=last_attempt,
        last_observation_at=last_obs,
        seconds_since_attempt=_age(now, last_attempt),
        seconds_since_observation=_age(now, last_obs),
        refresh_rules_total=total,
        refresh_rules_enabled=enabled,
    )


def _collect_optimizer(session: Any, now: datetime) -> OptimizerChurn:
    from sqlalchemy import func, select

    from app_shared.models.strategy import DomainStrategyProfile

    total, changed_24h, changed_7d = session.execute(
        select(
            func.count(),
            func.count().filter(
                DomainStrategyProfile.updated_at >= now - timedelta(hours=24)
            ),
            func.count().filter(
                DomainStrategyProfile.updated_at >= now - timedelta(days=7)
            ),
        ).select_from(DomainStrategyProfile)
    ).one()

    return OptimizerChurn(
        available=True,
        profiles_total=int(total or 0),
        changed_24h=int(changed_24h or 0),
        changed_7d=int(changed_7d or 0),
    )


def _collect_redis(redis: Any | None) -> RedisHealth:
    """Redis posture from the cached policy report + an optional ``INFO``."""
    from app_shared.redis_policy import last_report

    report = last_report()
    fields: dict[str, Any] = {}
    if report is not None:
        fields = {
            # `RedisPolicyStatus` is a `(str, Enum)`, NOT a `StrEnum`, so
            # `str(...)` yields "RedisPolicyStatus.VIOLATION" and the
            # rules' `== "VIOLATION"` comparison would never match --
            # a redis-eviction alarm that could never fire. `.value` is
            # the bare member string. (Caught by a unit test; the same
            # trap does not apply to the `StrEnum`s elsewhere here.)
            "policy_status": report.status.value,
            "policy": report.policy,
            "maxmemory": report.maxmemory,
            "policy_detail": report.detail,
        }

    if redis is None:
        return RedisHealth(
            available=report is not None,
            unavailable_reason=(
                None if report is not None else "redis policy never probed in this process"
            ),
            **fields,
        )

    info: dict[str, Any] = {}
    try:
        raw = redis.info()
        if isinstance(raw, dict):
            info = raw
    except Exception as exc:  # noqa: BLE001 - INFO is a nice-to-have, never fatal
        return RedisHealth(
            available=True,
            unavailable_reason=f"INFO failed: {exc.__class__.__name__}",
            **fields,
        )

    return RedisHealth(
        available=True,
        used_memory=_as_int(info.get("used_memory")),
        maxmemory_from_info=_as_int(info.get("maxmemory")),
        evicted_keys=_as_int(info.get("evicted_keys")),
        connected_clients=_as_int(info.get("connected_clients")),
        **fields,
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scrapyd_node_health(node_url: str, payload: dict[str, Any] | None) -> ScrapydNodeHealth:
    """One entry of the per-node probe map -> its labelled-absence-safe shape.

    ``payload is None`` is ``ScrapydDispatchClient.daemon_status``'s "any
    transport/HTTP/parse error" contract (T6) — that node is reported
    ``available=False``, never as a healthy node with zero counts.
    """
    if payload is None:
        return ScrapydNodeHealth(
            node_url=node_url,
            available=False,
            unavailable_reason="daemonstatus probe failed or node unreachable",
        )
    status_value = payload.get("status")
    return ScrapydNodeHealth(
        node_url=node_url,
        available=True,
        status=status_value if isinstance(status_value, str) else None,
        pending=_as_int(payload.get("pending")),
        running=_as_int(payload.get("running")),
        finished=_as_int(payload.get("finished")),
    )


def _collect_scrapyd(
    session: Any, status: dict[str, dict[str, Any] | None] | None
) -> ScrapydHealth:
    from sqlalchemy import func, select

    from app_shared.models.jobs import ScrapeJobTarget

    in_flight = int(
        session.execute(
            select(func.count())
            .select_from(ScrapeJobTarget)
            .where(ScrapeJobTarget.status.in_(["PENDING", "STARTED"]))
        ).scalar_one()
        or 0
    )
    if status is None:
        return ScrapydHealth(
            available=True,
            unavailable_reason="no scrapyd daemonstatus probe supplied",
            in_flight_targets=in_flight,
        )

    nodes = tuple(
        _scrapyd_node_health(node_url, payload) for node_url, payload in status.items()
    )
    reachable = [n for n in nodes if n.available]
    unavailable_reason = None
    if nodes and not reachable:
        unavailable_reason = f"all {len(nodes)} scrapyd node(s) unreachable"

    return ScrapydHealth(
        available=True,
        unavailable_reason=unavailable_reason,
        # Summing zero real numbers is not the same claim as "nothing
        # pending" — the aggregate stays the absence when no node answered.
        pending=sum(n.pending or 0 for n in reachable) if reachable else None,
        running=sum(n.running or 0 for n in reachable) if reachable else None,
        finished=sum(n.finished or 0 for n in reachable) if reachable else None,
        in_flight_targets=in_flight,
        nodes=nodes,
    )


def _collect_db_role(session: Any) -> DatabaseRoleHealth:
    """Audit C3: is the connected role actually subject to RLS?"""
    from sqlalchemy import text

    row = session.execute(
        text(
            "SELECT current_user, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
    ).first()
    if row is None:
        return DatabaseRoleHealth(
            available=False, unavailable_reason="current_user not found in pg_roles"
        )
    return DatabaseRoleHealth(
        available=True,
        role=str(row[0]),
        is_superuser=bool(row[1]),
        bypasses_rls=bool(row[2]),
    )


__all__ = [
    "PAID_ACCESS_METHODS",
    "BreakerHealth",
    "DatabaseRoleHealth",
    "DomainDiscovery",
    "DomainStats",
    "Freshness",
    "OpsSnapshot",
    "OptimizerChurn",
    "OutboxHealth",
    "PartitionHealth",
    "QueueHealth",
    "RedisHealth",
    "RollupHealth",
    "ScrapydHealth",
    "ScrapydNodeHealth",
    "SpendVelocity",
    "collect_snapshot",
    "seconds_remaining_in_month",
]
