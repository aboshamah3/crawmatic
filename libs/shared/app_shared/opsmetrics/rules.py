"""Alert rules over an :class:`~app_shared.opsmetrics.snapshot.OpsSnapshot`.

**Pure.** Snapshot + thresholds -> verdicts. Stdlib only, no database, no
network, no clock — the same discipline ``access/engine.py`` and
``access/breaker.evaluate_thresholds`` already follow, so every threshold
is unit-testable and every alert is reproducible from a stored snapshot.

Why the rules live in code rather than in a monitoring vendor
------------------------------------------------------------

Investigated 2026-08-15: Railway (the only place this runs) can alert on
**CPU, RAM, disk and network egress only**. Its Monitors explicitly
cannot match log content, cannot probe an HTTP endpoint, and cannot read
a custom metric. Its webhooks fire on deployment state changes and
monitor thresholds. There is no Prometheus scrape, and the observability
dashboard filters logs but does not alert on them.

So a rule expressed anywhere except inside this codebase would never
fire. Putting the evaluation here means the *product* decides it has a
problem and says so through channels that already exist (a structured
ERROR log line, and an ops endpoint an operator or a one-line cron can
poll). See ``docs/ops/OBSERVABILITY_SLO_AND_ALERTS.md`` for the full
trade-off write-up.

Threshold discipline
--------------------

Every threshold below carries a ``justification`` string naming the
measurement it comes from. None were invented. Where a threshold exists
elsewhere in the codebase (the proxy breaker's
``PROXY_BREAKER_MAX_REQUESTS_PER_URL`` and
``..._MAX_DISCOVERY_RUNS_PER_DOMAIN_PER_DAY``) the *same* number is used
here, so the dashboard warns at exactly the point the stop-loss acts —
an alert that fires only after the breaker has already tripped is an
incident report, not a warning.

Rate-of-change is a first-class citizen (audit H5: "alert on
rate-of-change as well as absolute values"). Rules whose id starts
``surge``/``acceleration`` compare a window to its own baseline. Each
also carries an **absolute floor**, because a ratio on tiny numbers
(2 requests up from 0) is noise, and an alerting system that cries wolf
gets muted — which is how you end up back at silent failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

from app_shared.opsmetrics import cost
from app_shared.opsmetrics.snapshot import OpsSnapshot


class Severity(StrEnum):
    """How fast a human must act.

    ``CRITICAL`` — money is burning, data is being lost, or writes are
    failing (or will fail on a known date). Page.
    ``HIGH`` — a control is inert or a budget/quality SLO is breached.
    Same business day.
    ``WARNING`` — a leading indicator. Next working day.
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    WARNING = "WARNING"


class Category(StrEnum):
    COST = "COST"
    RELIABILITY = "RELIABILITY"
    DATA = "DATA"
    DELIVERY = "DELIVERY"
    INFRA = "INFRA"
    SECURITY = "SECURITY"


@dataclass(frozen=True)
class Thresholds:
    """Every tunable number in one place. Defaults are the measured ones."""

    # --- partitions / rollups -------------------------------------------
    #: Days of remaining fuse below which a missing next-month partition
    #: escalates from HIGH to CRITICAL. 14 = two weekly ops cycles.
    partition_fuse_critical_days: int = 14
    #: Rollup runs daily (``DAILY_ROLLUP_INTERVAL_SECONDS`` = 86400), so
    #: 2 days of lag means two consecutive runs were missed.
    rollup_stale_lag_days: int = 2

    # --- cost: network-ledger rollup (EPA C6) -----------------------------
    #: Same shape as ``rollup_stale_lag_days`` above: the cost rollup also
    #: runs daily, so 2 days of watermark lag means two consecutive runs
    #: were missed.
    cost_rollup_watermark_stale_days: int = 2
    #: NOT a measured production figure (no reconciled-cost history exists
    #: yet to measure from, unlike most thresholds in this file) — a
    #: conservative starting point pending real operator tuning once C5's
    #: reconciliation has run for a while. Flagged here rather than
    #: silently presented as derived.
    cost_rollup_variance_critical_pct: float = 20.0
    #: Same absolute figure as ``freshness_attempt_critical_seconds``: two
    #: days with no closed network operation at all.
    cost_rollup_ledger_freshness_critical_seconds: float = 172_800.0

    # --- outbox ----------------------------------------------------------
    outbox_pending_warning: int = 500
    outbox_pending_high: int = 5_000
    #: The dispatcher polls on the order of seconds; 15 minutes of
    #: un-dispatched backlog means it is not running.
    outbox_oldest_pending_seconds: float = 900.0

    # --- cost: requests per link ----------------------------------------
    #: Same number as ``PROXY_BREAKER_MAX_REQUESTS_PER_URL`` — warn where
    #: the breaker acts, never after.
    requests_per_url_high: float = 8.0
    #: ~1.6x the measured healthy amazon figure of 2.48 req/URL.
    requests_per_url_warning: float = 4.0
    proxied_per_url_high: float = 6.0
    #: Below this many attempts a ratio is not a measurement.
    requests_per_url_min_attempts: int = 100

    # --- cost: spend ------------------------------------------------------
    #: A full 4,587-link refresh of the whole catalog costs ~$2.00. One
    #: domain spending half that in a day is off-plan; 1.5x a whole
    #: refresh in a day is a runaway.
    domain_usd_per_day_warning: float = 1.00
    domain_usd_per_day_high: float = 3.00
    #: Fraction of the monthly proxied-request ceiling the 24h-velocity
    #: month-end forecast may reach before warning.
    forecast_warning_fraction: float = 0.75
    #: Trailing-24h proxied volume vs the preceding 24h.
    spend_acceleration_high: float = 4.0
    #: ...but only once the absolute volume is meaningful. 2,000 proxied
    #: attempts ~= $0.25 — enough to matter, small enough to catch early.
    spend_acceleration_min_proxied: int = 2_000
    #: Measured 2026-08 production: amazon 43.5%, noon 48.6% of paid
    #: attempts produced no observation. That is the "cost per usable
    #: price can be unbounded" failure the audit names.
    wasted_paid_rate_high: float = 0.40
    wasted_paid_min_attempts: int = 200

    # --- cost: cost per successful price (Task 2.4) -----------------------
    #: task-2.4 brief, verbatim: "alert when any domain's cost/price >
    #: $0.005 over 24h". Computed at each domain's OWN $/req rate (the
    #: 2026-08-12 report), not the fleet average -- see
    #: ``cost.usd_per_request_for_domain``. This is the tunable knob;
    #: the $/req rates themselves are observations-about-the-world
    #: constants in ``opsmetrics.cost`` (see that module's docstring),
    #: not settable here.
    cost_per_successful_price_high: float = 0.005

    # --- cost: discovery --------------------------------------------------
    #: Same number as ``PROXY_BREAKER_MAX_DISCOVERY_RUNS_PER_DOMAIN_PER_DAY``.
    discovery_runs_per_day_high: int = 50
    #: 10x the breaker ceiling. Measured production runaways: extra.com
    #: 7,186 runs on 2026-08-12, fqtoners.com ~1,430/day for ten
    #: consecutive days.
    discovery_runs_per_day_critical: int = 500
    #: Rate-of-change: today vs the daily mean of the PRECEDING 6 days
    #: (today excluded from its own baseline -- see
    #: ``DomainDiscovery.mean_daily_runs_7d``). 3.0 = tripling overnight.
    discovery_surge_factor: float = 3.0
    discovery_surge_min_runs: int = 30

    # --- reliability ------------------------------------------------------
    #: Grounded in the measured per-domain floor of the healthy direct
    #: sites (pcpalace 98.3%, rawand 99.0%, rowadalahbar 98.2%).
    domain_success_rate_warning: float = 0.90
    domain_success_rate_high: float = 0.70
    domain_success_min_attempts: int = 50
    #: A target that has been PENDING for an hour is not queued, it is lost.
    target_pending_age_seconds: float = 3_600.0
    #: ``MATCH_LOCK_BROWSER_TTL_SECONDS`` (1800) is the longest a healthy
    #: in-flight target can legitimately hold its lock — but since EPA
    #: A3/B2 a STARTED target past that is no longer a standing anomaly,
    #: it is the reaper's ordinary workload: `reap_stale_targets` reverts
    #: it to PENDING at ``SCRAPE_STARTED_REAP_AFTER_SECONDS`` (2100).
    #: Alerting at 1800 would therefore have fired on every single
    #: orphan in the 300s window the reaper is deliberately waiting out.
    #: Set past the reap deadline instead, so what this rule now means is
    #: the thing an operator actually has to act on: **the reaper is not
    #: running** (or is failing) — a target that survived its own reaping.
    target_started_age_seconds: float = 2_700.0
    #: DEFERRED is non-terminal by design, but a day-old deferral means
    #: nothing ever re-dispatched it.
    target_deferred_age_seconds: float = 86_400.0
    #: Nothing scraped in 24h/48h. Measured 2026-08-15: 2.6 days silent.
    freshness_attempt_high_seconds: float = 86_400.0
    freshness_attempt_critical_seconds: float = 172_800.0

    # --- infra ------------------------------------------------------------
    redis_memory_high_fraction: float = 0.85
    #: The breaker evaluator leases every ``PROXY_BREAKER_EVAL_INTERVAL_SECONDS``
    #: (300). Ten missed evaluations means the stop-loss is not running.
    breaker_evaluator_stale_seconds: float = 3_000.0
    scrapyd_in_flight_warning: int = 2_000
    #: Half the profile set rewritten in a day is the optimizer chasing
    #: noise (or fighting an operator edit — see the documented
    #: ``learned-profile-overrides-policy`` failure mode).
    optimizer_churn_high_fraction: float = 0.5
    optimizer_churn_min_profiles: int = 5

    # --- EPA B9 (F22, audit §13 Operations) --------------------------------
    # See the "EPA B9 new alert rules" section below for why each of these
    # is read from the snapshot via `getattr(..., None)` rather than a
    # declared `OpsSnapshot` field.
    #: Below this fraction of ACTIVE matches freshened in 24h, the scraping
    #: pipeline is degraded — the same number `apps.api.app.routers.health`
    #: (EPA B8) reports `/health/scraping` degraded at, so both surfaces
    #: agree.
    freshness_fraction_24h_min: float = 0.95
    #: More than this many pending persistence batches is a backlog the
    #: writer is not draining.
    persistence_pending_batches_high: int = 16
    #: Tighter, earlier-warning companion to `breaker_evaluator_stale_seconds`
    #: (3000s/CRITICAL region): 900s = three missed
    #: `PROXY_BREAKER_EVAL_INTERVAL_SECONDS` (300) leases, a WARNING that the
    #: evaluator is falling behind before it reaches the existing HIGH
    #: `breaker.evaluator_stale` alert's territory.
    breaker_evaluation_lag_warning_seconds: float = 900.0
    #: Host and every mounted volume: below this free fraction, the next
    #: write (a partition INSERT, a backup, a log line) may fail outright.
    disk_free_fraction_critical: float = 0.15
    #: Same figure as `app_shared.opsmetrics.emit.LEDGER_LINKED_ATTEMPT_FRACTION_MIN`
    #: (not imported directly: `emit` imports FROM this module, so the
    #: reverse import would be circular) — kept in sync by hand, and by the
    #: unit test that pins both to 0.95.
    ledger_linked_attempt_fraction_24h_min: float = 0.95
    #: `POSTED` dispatch intents unresolved for longer than this are the
    #: "for 10 min" duration in the rule's name — a `POSTED` intent seen
    #: once is not yet an incident; one still `POSTED` 10 minutes later is.
    dispatch_ambiguous_intents_sustained_seconds: float = 600.0


DEFAULT_THRESHOLDS = Thresholds()


# --------------------------------------------------------------------------
# EPA A5 (deep dive §5) — thresholds for the baseline OUTCOME gauges
#
# The `Thresholds` dataclass above is consumed by the `RULES` table, and
# every field in it has a rule that reads it. These five have no rule yet:
# the gauges they judge (`crawmatic_attempts_per_valid_fresh_24h`,
# `crawmatic_persistence_failures_1h`,
# `crawmatic_dispatch_ambiguous_intents`,
# `crawmatic_cost_model_drift_ratio` — see
# `app_shared.opsmetrics.emit.BaselineMetrics`) are collected outside
# `OpsSnapshot`, and `Rule.evaluate` takes an `OpsSnapshot`. Adding
# unevaluated fields to `Thresholds` would make `DEFAULT_THRESHOLDS`
# claim coverage that does not exist, so they are module constants until
# B9 wires the collection into the snapshot and can promote them.
#
# They are stated NOW, here, for the same reason the A7 ledger-coverage
# thresholds are stated in `emit`: "what counts as bad" is a judgement
# made while the measurement is fresh in mind, and re-deriving it months
# later from a dashboard is how thresholds end up meaning nothing.
# --------------------------------------------------------------------------

#: More than this many fetch attempts per freshly-priced match means the
#: escalation ladder is burning money without producing outcomes. Set
#: against the measured 2026-09-03 baseline, where a healthy domain sits
#: near 1-2 and a broken one runs the whole ladder for every match.
ATTEMPTS_PER_VALID_FRESH_MAX: float = 6.0

#: A fetch that succeeded on the wire and persisted nothing is a defect,
#: not noise — money was spent and no durable row came out of it. A
#: handful within one hour is a broken write path.
PERSISTENCE_FAILURES_1H_MAX: int = 5

#: `POSTED` is the ONE genuinely ambiguous dispatch state (the run may or
#: may not exist on the node). Any intent still sitting there past the
#: collector's 5-minute window is unresolved work: either it will never
#: run, or it may run twice. Zero is the only defensible ceiling.
DISPATCH_AMBIGUOUS_INTENTS_MAX: int = 0

#: Our ledger's proxied byte count may legitimately differ from the
#: provider's billed figure (compression, CONNECT/TLS overhead, redirect
#: chains) — but a factor of two in either direction is a cost model that
#: no longer describes what we are being charged for. NOTE: the gauge is
#: NULL (never 0) when no provider figure has been imported, so a missing
#: reconciliation must be alerted on as *absence*, never read as 0 drift.
COST_MODEL_DRIFT_RATIO_MIN: float = 0.5
COST_MODEL_DRIFT_RATIO_MAX: float = 2.0


@dataclass(frozen=True)
class Alert:
    """One firing rule instance."""

    rule_id: str
    severity: Severity
    category: Category
    #: Human summary with the observed numbers inlined.
    message: str
    #: Why this threshold is this number (from the rule definition).
    justification: str
    #: Machine-readable observed values behind the verdict.
    observed: dict[str, Any] = field(default_factory=dict)
    #: Which sub-entity fired (domain name, table name, ...), if any.
    subject: str | None = None
    #: Path into ``docs/ops/RUNBOOK_STOP_DISPATCH_AND_SPEND.md``.
    runbook: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": str(self.severity),
            "category": str(self.category),
            "subject": self.subject,
            "message": self.message,
            "justification": self.justification,
            "observed": self.observed,
            "runbook": self.runbook,
        }


@dataclass(frozen=True)
class Rule:
    """A named, documented rule and the function that evaluates it.

    ``evaluate`` returns zero or more :class:`Alert`\\ s — zero when the
    rule passes *or* when the underlying section is unavailable (a blind
    section is reported by its own ``*.unavailable`` rule, not by every
    rule that depends on it firing spuriously).
    """

    rule_id: str
    category: Category
    title: str
    justification: str
    evaluate: Callable[[OpsSnapshot, Thresholds], list[Alert]]


def _alert(
    rule: str,
    severity: Severity,
    category: Category,
    message: str,
    justification: str,
    *,
    subject: str | None = None,
    observed: dict[str, Any] | None = None,
    runbook: str | None = None,
) -> Alert:
    return Alert(
        rule_id=rule,
        severity=severity,
        category=category,
        message=message,
        justification=justification,
        subject=subject,
        observed=observed or {},
        runbook=runbook,
    )


# --------------------------------------------------------------------------
# DATA — the dated write outage and the rollup that never ran
# --------------------------------------------------------------------------

_J_PARTITION = (
    "A monthly RANGE-partitioned table with no partition for month M rejects "
    "every INSERT from the first instant of M. This is a dated, certain "
    "outage, not a probability. Production on 2026-08-15 had no 2026_09 "
    "partition on any of the four partitioned tables and nothing said so."
)


def _r_partition(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    if not snapshot.partitions_available:
        return [
            _alert(
                "partition.unavailable",
                Severity.HIGH,
                Category.DATA,
                f"Partition health could not be read: {snapshot.partitions_unavailable_reason}",
                "A blind partition check is indistinguishable from a passing one.",
                runbook="#partition-or-rollup-alert",
            )
        ]
    out: list[Alert] = []
    for p in snapshot.partitions:
        if not p.exists:
            continue
        if not p.current_month_present:
            out.append(
                _alert(
                    "partition.current_month_missing",
                    Severity.CRITICAL,
                    Category.DATA,
                    f"{p.table} has no partition for the CURRENT month — "
                    "inserts are failing right now.",
                    _J_PARTITION,
                    subject=p.table,
                    observed={"table": p.table, "partition_count": p.partition_count},
                    runbook="#partition-or-rollup-alert",
                )
            )
        if not p.next_month_present:
            critical = p.days_until_next_month <= t.partition_fuse_critical_days
            out.append(
                _alert(
                    "partition.next_month_missing",
                    Severity.CRITICAL if critical else Severity.HIGH,
                    Category.DATA,
                    f"{p.table} has no partition for next month; writes fail in "
                    f"{p.days_until_next_month} day(s).",
                    _J_PARTITION
                    + f" Escalates to CRITICAL inside {t.partition_fuse_critical_days} "
                    "days (two weekly ops cycles).",
                    subject=p.table,
                    observed={
                        "table": p.table,
                        "days_until_next_month": p.days_until_next_month,
                        "partition_count": p.partition_count,
                    },
                    runbook="#partition-or-rollup-alert",
                )
            )
    return out


_J_ROLLUP = (
    "price_observations retention drops a partition only after verifying "
    "daily-rollup coverage for it (registry.feeds_rollups). A rollup that "
    "has never run therefore pins every partition forever AND silently "
    "destroys the historical price series the product sells. Production on "
    "2026-08-15: 0 rollup rows against 32,231 observations spanning a month."
)


def _r_rollup(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    r = snapshot.rollups
    if not r.available:
        return [
            _alert(
                "rollup.unavailable",
                Severity.HIGH,
                Category.DATA,
                f"Rollup health could not be read: {r.unavailable_reason}",
                "A blind rollup check is indistinguishable from a passing one.",
                runbook="#partition-or-rollup-alert",
            )
        ]
    if r.rows == 0 and r.observation_rows > 0:
        return [
            _alert(
                "rollup.never_run",
                Severity.CRITICAL,
                Category.DATA,
                f"variant_price_daily_rollups is EMPTY against "
                f"{r.observation_rows} price observations — the daily rollup "
                "has never run.",
                _J_ROLLUP,
                observed={
                    "rollup_rows": 0,
                    "observation_rows": r.observation_rows,
                },
                runbook="#partition-or-rollup-alert",
            )
        ]
    if r.lag_days is not None and r.lag_days > t.rollup_stale_lag_days:
        return [
            _alert(
                "rollup.stale",
                Severity.HIGH,
                Category.DATA,
                f"Daily rollup is {r.lag_days} day(s) behind the newest "
                f"observation ({r.unrolled_observations} un-summarised rows).",
                _J_ROLLUP
                + f" The rollup runs daily, so >{t.rollup_stale_lag_days} days "
                "of lag means consecutive runs were missed.",
                observed={
                    "lag_days": r.lag_days,
                    "unrolled_observations": r.unrolled_observations,
                },
                runbook="#partition-or-rollup-alert",
            )
        ]
    return []


# --------------------------------------------------------------------------
# COST — the network-ledger cost rollup (EPA C6)
# --------------------------------------------------------------------------

_J_COST_ROLLUP = (
    "EPA C6: GET /ops/metrics reads ONLY the durable cost-rollup tables "
    "for spend health, by design, so it never runs a synchronous "
    "high-cardinality aggregation on request. That means a missing or "
    "stale rollup watermark does not show up as absent data -- it shows "
    "up as SILENTLY FROZEN numbers that look like a healthy, unchanging "
    "spend pattern. A rollup day with operations but zero reconciled "
    "cost is the other half: every dollar reported is an ESTIMATE "
    "nobody has checked against what the provider actually billed. Both "
    "are CRITICAL, not merely 'unavailable', per this task's explicit "
    "instruction."
)


def _r_cost_rollup(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    c = snapshot.cost_rollup
    if not c.available:
        return [
            _alert(
                "cost_rollup.unavailable",
                Severity.CRITICAL,
                Category.COST,
                f"Cost-rollup health could not be read: {c.unavailable_reason}",
                _J_COST_ROLLUP,
            )
        ]

    out: list[Alert] = []

    if not c.watermark_available:
        out.append(
            _alert(
                "cost_rollup.watermark_store_missing",
                Severity.CRITICAL,
                Category.COST,
                "rollup_watermarks is not available -- the network-cost-rollup "
                "cursor cannot be tracked at all.",
                _J_COST_ROLLUP,
            )
        )
    elif c.watermark_last_complete_date is None:
        out.append(
            _alert(
                "cost_rollup.watermark_never_seeded",
                Severity.CRITICAL,
                Category.COST,
                "The network_cost_rollup watermark has never been seeded -- "
                "the cost-rollup job has never run.",
                _J_COST_ROLLUP,
            )
        )
    elif (
        c.watermark_age_days is not None
        and c.watermark_age_days > t.cost_rollup_watermark_stale_days
    ):
        out.append(
            _alert(
                "cost_rollup.watermark_stale",
                Severity.CRITICAL,
                Category.COST,
                f"Cost-rollup watermark is {c.watermark_age_days} day(s) stale "
                f"(last complete: {c.watermark_last_complete_date}).",
                _J_COST_ROLLUP,
                observed={
                    "watermark_age_days": c.watermark_age_days,
                    "threshold_days": t.cost_rollup_watermark_stale_days,
                },
            )
        )

    if c.latest_rollup_date is None:
        out.append(
            _alert(
                "cost_rollup.no_data",
                Severity.CRITICAL,
                Category.COST,
                "No fleet cost-rollup rows exist yet.",
                _J_COST_ROLLUP,
            )
        )
    else:
        if c.total_operation_count > 0 and c.reconciled_operation_count == 0:
            out.append(
                _alert(
                    "cost_rollup.reconciliation_missing",
                    Severity.CRITICAL,
                    Category.COST,
                    f"No reconciled cost for any of {c.total_operation_count} "
                    f"operation(s) rolled up on {c.latest_rollup_date} -- every "
                    "reported dollar is an unchecked estimate.",
                    _J_COST_ROLLUP,
                    observed={"total_operation_count": c.total_operation_count},
                )
            )
        variance = c.estimated_vs_reconciled_variance_pct
        if variance is not None and variance > t.cost_rollup_variance_critical_pct:
            out.append(
                _alert(
                    "cost_rollup.variance_high",
                    Severity.CRITICAL,
                    Category.COST,
                    f"Estimated vs. reconciled cost variance is {variance:.1f}% "
                    f"on {c.latest_rollup_date}.",
                    _J_COST_ROLLUP,
                    observed={
                        "variance_pct": round(variance, 2),
                        "threshold_pct": t.cost_rollup_variance_critical_pct,
                    },
                )
            )

    age = c.ledger_freshness_seconds
    if age is not None and age > t.cost_rollup_ledger_freshness_critical_seconds:
        out.append(
            _alert(
                "cost_rollup.ledger_silent",
                Severity.CRITICAL,
                Category.COST,
                f"No network operation has closed in {age / 3600:.1f} hours -- "
                "the cost ledger itself is silent.",
                _J_COST_ROLLUP,
                observed={
                    "seconds_since_last_close": round(age),
                    "threshold_seconds": t.cost_rollup_ledger_freshness_critical_seconds,
                },
            )
        )

    return out


# --------------------------------------------------------------------------
# DELIVERY — the outbox
# --------------------------------------------------------------------------

_J_OUTBOX = (
    "Audit H1: async work can be lost on worker/broker failure. The outbox "
    "is the durability fix, so an outbox that is backing up or accumulating "
    "DEAD rows means the fix itself has stopped working and the loss is "
    "happening again — silently."
)


def _r_outbox(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    o = snapshot.outbox
    if not o.available:
        return [
            _alert(
                "outbox.unavailable",
                Severity.HIGH,
                Category.DELIVERY,
                f"Outbox health could not be read: {o.unavailable_reason}",
                "In a database behind the code (missing outbox_messages) the "
                "durable-delivery guarantee is simply not deployed.",
                runbook="#outbox-backlog",
            )
        ]
    out: list[Alert] = []
    if o.dead > 0:
        out.append(
            _alert(
                "outbox.dead",
                Severity.HIGH,
                Category.DELIVERY,
                f"{o.dead} outbox message(s) are DEAD — that work will never run.",
                _J_OUTBOX + " DEAD is terminal: any non-zero count is lost work.",
                observed={"dead": o.dead},
                runbook="#outbox-backlog",
            )
        )
    if o.pending >= t.outbox_pending_high:
        out.append(
            _alert(
                "outbox.backlog",
                Severity.HIGH,
                Category.DELIVERY,
                f"Outbox backlog is {o.pending} pending messages.",
                _J_OUTBOX,
                observed={"pending": o.pending, "threshold": t.outbox_pending_high},
                runbook="#outbox-backlog",
            )
        )
    elif o.pending >= t.outbox_pending_warning:
        out.append(
            _alert(
                "outbox.backlog",
                Severity.WARNING,
                Category.DELIVERY,
                f"Outbox backlog is {o.pending} pending messages.",
                _J_OUTBOX,
                observed={"pending": o.pending, "threshold": t.outbox_pending_warning},
                runbook="#outbox-backlog",
            )
        )
    age = o.oldest_pending_age_seconds
    if age is not None and age > t.outbox_oldest_pending_seconds:
        out.append(
            _alert(
                "outbox.oldest_pending_age",
                Severity.HIGH,
                Category.DELIVERY,
                f"Oldest PENDING outbox message is {age / 60:.0f} minutes old.",
                _J_OUTBOX
                + " The dispatcher polls on the order of seconds, so 15 minutes "
                "of un-dispatched backlog means it is not running at all.",
                observed={"oldest_pending_age_seconds": round(age)},
                runbook="#outbox-backlog",
            )
        )
    return out


# --------------------------------------------------------------------------
# COST — the audit's four named primary alarms
# --------------------------------------------------------------------------

_J_REQ_PER_URL = (
    "Audit §7 names requests/link and proxied attempts/link as primary cost "
    "alarms: they rise long before raw spend looks alarming, and a runaway "
    "loop's signature is a high ratio, not a high total. Healthy amazon is "
    "2.48 req/URL (2026-08-11 full scrape). Measured 30d production on "
    "2026-08-15: stech.ink 12.72, amazon.sa 8.44, noon.com 7.41. The HIGH "
    "threshold is deliberately the SAME number as the proxy breaker's "
    "PROXY_BREAKER_MAX_REQUESTS_PER_URL so the warning precedes the stop-loss."
)


def _r_requests_per_url(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    out: list[Alert] = []
    for d in snapshot.domains_24h:
        if d.attempts < t.requests_per_url_min_attempts:
            continue
        rpu, ppu = d.requests_per_url, d.proxied_per_url
        if rpu is not None and rpu >= t.requests_per_url_high:
            out.append(
                _alert(
                    "cost.requests_per_url",
                    Severity.HIGH,
                    Category.COST,
                    f"{d.domain}: {rpu:.2f} requests per unique URL in 24h "
                    f"({d.attempts} attempts / {d.distinct_urls} URLs).",
                    _J_REQ_PER_URL,
                    subject=d.domain,
                    observed={
                        "requests_per_url": round(rpu, 2),
                        "threshold": t.requests_per_url_high,
                        "attempts": d.attempts,
                        "distinct_urls": d.distinct_urls,
                    },
                    runbook="#stop-proxy-spend-now",
                )
            )
        elif rpu is not None and rpu >= t.requests_per_url_warning:
            out.append(
                _alert(
                    "cost.requests_per_url",
                    Severity.WARNING,
                    Category.COST,
                    f"{d.domain}: {rpu:.2f} requests per unique URL in 24h.",
                    _J_REQ_PER_URL,
                    subject=d.domain,
                    observed={
                        "requests_per_url": round(rpu, 2),
                        "threshold": t.requests_per_url_warning,
                        "healthy_reference": cost.AMAZON_HEALTHY_REQUESTS_PER_URL,
                    },
                    runbook="#stop-proxy-spend-now",
                )
            )
        if ppu is not None and ppu >= t.proxied_per_url_high:
            out.append(
                _alert(
                    "cost.proxied_per_url",
                    Severity.HIGH,
                    Category.COST,
                    f"{d.domain}: {ppu:.2f} PAID requests per unique URL in 24h "
                    f"(~${d.estimated_usd:.2f}).",
                    _J_REQ_PER_URL,
                    subject=d.domain,
                    observed={
                        "proxied_per_url": round(ppu, 2),
                        "threshold": t.proxied_per_url_high,
                        "estimated_usd": d.estimated_usd,
                    },
                    runbook="#stop-proxy-spend-now",
                )
            )
    return out


_J_DOMAIN_SPEND = (
    "Audit §7 names spend/domain/day a primary alarm and warns that 'one "
    "amazon-heavy catalog can dominate aggregate cost'. A complete refresh "
    "of the entire 4,587-link catalog costs ~$2.00 all-in, so a SINGLE "
    "domain spending $1/day is already off-plan and $3/day is more than a "
    "whole catalog refresh — per domain, per day."
)


def _r_domain_spend(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    out: list[Alert] = []
    for d in snapshot.domains_24h:
        spend = d.estimated_usd
        if spend >= t.domain_usd_per_day_high:
            severity = Severity.HIGH
            threshold = t.domain_usd_per_day_high
        elif spend >= t.domain_usd_per_day_warning:
            severity = Severity.WARNING
            threshold = t.domain_usd_per_day_warning
        else:
            continue
        out.append(
            _alert(
                "cost.spend_per_domain_per_day",
                severity,
                Category.COST,
                f"{d.domain}: ~${spend:.2f} of proxy spend in 24h "
                f"({d.proxied} paid attempts).",
                _J_DOMAIN_SPEND,
                subject=d.domain,
                observed={
                    "estimated_usd_24h": spend,
                    "proxied_24h": d.proxied,
                    "threshold_usd": threshold,
                    "usd_per_full_refresh": cost.USD_PER_FULL_REFRESH,
                },
                runbook="#stop-proxy-spend-now",
            )
        )
    return out


_J_WASTED = (
    "Audit §7: 'cost per attempt is low; cost per usable price can be "
    "unbounded when success is zero'. This is the percent-of-spend-"
    "producing-no-observation metric the financial recommendation names. "
    "Measured 30d production 2026-08-15: amazon.sa 4,156 of 9,555 paid "
    "attempts wasted (43.5%), noon.com 2,845 of 5,855 (48.6%)."
)


def _r_wasted_spend(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    out: list[Alert] = []
    for d in snapshot.domains_24h:
        rate = d.wasted_paid_rate
        if (
            rate is not None
            and d.proxied >= t.wasted_paid_min_attempts
            and rate >= t.wasted_paid_rate_high
        ):
            out.append(
                _alert(
                    "cost.wasted_paid_rate",
                    Severity.HIGH,
                    Category.COST,
                    f"{d.domain}: {rate:.0%} of paid attempts produced no "
                    f"observation ({d.failed_paid}/{d.proxied}, ~"
                    f"${cost.usd(d.failed_paid):.2f} burned).",
                    _J_WASTED,
                    subject=d.domain,
                    observed={
                        "wasted_paid_rate": round(rate, 3),
                        "failed_paid": d.failed_paid,
                        "proxied": d.proxied,
                        "wasted_usd": cost.usd(d.failed_paid),
                        "threshold": t.wasted_paid_rate_high,
                    },
                    runbook="#stop-proxy-spend-now",
                )
            )
    return out


_J_COST_PER_PRICE = (
    "task-2.4 brief, verbatim: 'cost per attempt is low, but cost per "
    "USABLE price can be unbounded (a domain can spend money and produce "
    "zero prices). Make that denominator first-class.' Two conditions, "
    "not one: the $/successful-price RATIO against a ceiling (computed at "
    "this domain's own $/req rate from the 2026-08-12 report), AND -- "
    "unconditionally, whatever the ratio threshold is set to -- ANY spend "
    "that produced ZERO successful prices, because at that point the "
    "ratio is infinite/undefined and a ratio-only rule can never fire on "
    "it. Amazon on 2026-08-12 (report §4.1) is the measured instance: 25 "
    "proxied attempts, zero successes, while still burning the largest "
    "share of the proxy bill."
)


def _r_cost_per_successful_price(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    out: list[Alert] = []
    for d in snapshot.domains_24h:
        if d.proxied_requests == 0:
            continue  # no spend at all -- nothing to alarm on
        spend = d.estimated_usd_domain_rate
        if d.successful_prices == 0:
            out.append(
                _alert(
                    "cost.zero_successful_prices",
                    Severity.CRITICAL,
                    Category.COST,
                    f"{d.domain}: ~${spend:.4f} of proxy spend in 24h produced "
                    f"ZERO successful prices ({d.proxied_requests} paid attempts).",
                    _J_COST_PER_PRICE,
                    subject=d.domain,
                    observed={
                        "proxied_requests": d.proxied_requests,
                        "successful_prices": 0,
                        "estimated_usd_24h": spend,
                    },
                    runbook="#stop-proxy-spend-now",
                )
            )
            continue
        ratio = d.cost_per_successful_price
        if ratio is not None and ratio > t.cost_per_successful_price_high:
            out.append(
                _alert(
                    "cost.per_successful_price",
                    Severity.HIGH,
                    Category.COST,
                    f"{d.domain}: ${ratio:.4f} per successful price over 24h "
                    f"({d.successful_prices} prices / ${spend:.4f} spend).",
                    _J_COST_PER_PRICE,
                    subject=d.domain,
                    observed={
                        "cost_per_successful_price": round(ratio, 6),
                        "successful_prices": d.successful_prices,
                        "proxied_requests": d.proxied_requests,
                        "estimated_usd_24h": spend,
                        "threshold": t.cost_per_successful_price_high,
                    },
                    runbook="#stop-proxy-spend-now",
                )
            )
    return out


_J_FORECAST = (
    "Audit §7: 'forecasted month-end spend from the latest 1-hour and "
    "24-hour velocity'. Absolute month-to-date spend looked survivable "
    "throughout the extra.com incident; the rate was the tell. Extrapolating "
    "the trailing window over the remaining seconds in the month converts a "
    "rate into a number an operator can act on before the invoice."
)


def _r_spend_forecast(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    s = snapshot.spend
    if not s.available:
        return [
            _alert(
                "spend.unavailable",
                Severity.HIGH,
                Category.COST,
                f"Spend velocity could not be read: {s.unavailable_reason}",
                "Unmeasurable spend is unbounded spend.",
                runbook="#stop-proxy-spend-now",
            )
        ]
    out: list[Alert] = []
    # Forecast against whichever ceiling BINDS FIRST, not the one that
    # happens to be configured in Settings. Production on 2026-08-15 had
    # a breaker ceiling of 250,000 and an enforced provider ledger cap of
    # 60,000; forecasting against 250,000 would have reported 8% of
    # budget consumed while the request-level gate was at 35%.
    ceiling = s.effective_ceiling
    if s.providers_without_budget:
        out.append(
            _alert(
                "cost.provider_without_budget",
                Severity.HIGH,
                Category.COST,
                f"{s.providers_without_budget} ACTIVE proxy provider(s) have no "
                "monthly_budget_limit — their spend is not counted at all.",
                "`access/budget.incr_and_check_monthly_budget` short-circuits "
                "before touching Redis when `limit is None`: an uncapped "
                "provider is never metered and never denied, so the ledger "
                "cannot bound it. Audit H3 names this exact hole.",
                observed={"providers_without_budget": s.providers_without_budget},
                runbook="#stop-proxy-spend-now",
            )
        )
    if ceiling:
        for label, forecast in (
            ("24h", s.forecast_month_end_24h),
            ("1h", s.forecast_month_end_1h),
        ):
            if forecast >= ceiling:
                severity = Severity.CRITICAL
                threshold = float(ceiling)
            elif forecast >= ceiling * t.forecast_warning_fraction:
                severity = Severity.WARNING
                threshold = ceiling * t.forecast_warning_fraction
            else:
                continue
            out.append(
                _alert(
                    "cost.month_end_forecast",
                    severity,
                    Category.COST,
                    f"Trailing-{label} velocity forecasts {forecast:,.0f} proxied "
                    f"requests (~${cost.usd(forecast):.2f}) by month end against a "
                    f"ceiling of {ceiling:,}.",
                    _J_FORECAST,
                    subject=label,
                    observed={
                        "window": label,
                        "forecast_proxied": round(forecast),
                        "forecast_usd": cost.usd(forecast),
                        "ceiling": ceiling,
                        "threshold": round(threshold),
                        "month_to_date": s.proxied_month_to_date,
                    },
                    runbook="#stop-proxy-spend-now",
                )
            )

    accel = s.acceleration_24h
    if (
        accel is not None
        and s.proxied_24h >= t.spend_acceleration_min_proxied
        and accel >= t.spend_acceleration_high
    ):
        out.append(
            _alert(
                "cost.spend_acceleration",
                Severity.HIGH,
                Category.COST,
                f"Proxy spend accelerated {accel:.1f}x day-over-day "
                f"({s.proxied_prev_24h} -> {s.proxied_24h} paid attempts).",
                _J_FORECAST
                + f" Gated on an absolute floor of {t.spend_acceleration_min_proxied} "
                "paid attempts so a ratio on tiny numbers cannot page anyone.",
                observed={
                    "acceleration_24h": (
                        "inf" if accel == float("inf") else round(accel, 2)
                    ),
                    "proxied_24h": s.proxied_24h,
                    "proxied_prev_24h": s.proxied_prev_24h,
                    "threshold": t.spend_acceleration_high,
                },
                runbook="#stop-proxy-spend-now",
            )
        )
    return out


_J_DISCOVERY = (
    "Audit §7 names discovery runs/domain/day a primary alarm, and the "
    "2026-08-12 hostname-normalisation loop is why: every individual request "
    "was legitimate, no gate was violated, and the run was on course for "
    "~$325/month. Measured 2026-08-15 production, AFTER that fix shipped: "
    "extra.com 7,186 runs on 2026-08-12 and fqtoners.com ~1,430 runs/day for "
    "ten consecutive days — 28x the breaker ceiling, undetected. HIGH is the "
    "same number as PROXY_BREAKER_MAX_DISCOVERY_RUNS_PER_DOMAIN_PER_DAY."
)


def _r_discovery(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    if not snapshot.discovery_available:
        return [
            _alert(
                "discovery.unavailable",
                Severity.HIGH,
                Category.COST,
                f"Discovery volume could not be read: "
                f"{snapshot.discovery_unavailable_reason}",
                "Unmeasurable discovery is how the last runaway hid.",
                runbook="#runaway-discovery",
            )
        ]
    out: list[Alert] = []
    for d in snapshot.discovery:
        if d.runs_24h >= t.discovery_runs_per_day_critical:
            out.append(
                _alert(
                    "discovery.runs_per_domain_per_day",
                    Severity.CRITICAL,
                    Category.COST,
                    f"{d.domain}: {d.runs_24h} strategy-discovery runs in 24h.",
                    _J_DISCOVERY,
                    subject=d.domain,
                    observed={
                        "runs_24h": d.runs_24h,
                        "runs_7d": d.runs_7d,
                        "threshold": t.discovery_runs_per_day_critical,
                    },
                    runbook="#runaway-discovery",
                )
            )
        elif d.runs_24h >= t.discovery_runs_per_day_high:
            out.append(
                _alert(
                    "discovery.runs_per_domain_per_day",
                    Severity.HIGH,
                    Category.COST,
                    f"{d.domain}: {d.runs_24h} strategy-discovery runs in 24h.",
                    _J_DISCOVERY,
                    subject=d.domain,
                    observed={
                        "runs_24h": d.runs_24h,
                        "runs_7d": d.runs_7d,
                        "threshold": t.discovery_runs_per_day_high,
                    },
                    runbook="#runaway-discovery",
                )
            )
        surge = d.surge_factor
        if (
            surge is not None
            and d.runs_24h >= t.discovery_surge_min_runs
            and surge >= t.discovery_surge_factor
        ):
            out.append(
                _alert(
                    "discovery.surge",
                    Severity.HIGH,
                    Category.COST,
                    f"{d.domain}: discovery runs surged "
                    f"{'from a standstill' if surge == float('inf') else f'{surge:.1f}x'}"
                    f" above its own preceding 6-day daily mean "
                    f"({d.mean_daily_runs_7d:.0f}/day -> {d.runs_24h}).",
                    _J_DISCOVERY
                    + " The surge rule catches a runaway that is still below any "
                    "absolute ceiling, which is the whole point of rate-of-change.",
                    subject=d.domain,
                    observed={
                        "surge_factor": "inf" if surge == float("inf") else round(surge, 2),
                        "runs_24h": d.runs_24h,
                        "mean_daily_runs_7d": round(d.mean_daily_runs_7d, 2),
                        "threshold": t.discovery_surge_factor,
                    },
                    runbook="#runaway-discovery",
                )
            )
    return out


# --------------------------------------------------------------------------
# RELIABILITY
# --------------------------------------------------------------------------

_J_SUCCESS = (
    "Per-domain success rate is the denominator of every cost metric: cost "
    "per usable price = spend / successes. Measured 30d production "
    "2026-08-15: pcpalace 98.3%, rawand 99.0%, rowadalahbar 98.2% (healthy "
    "direct sites, hence the 90% WARNING floor) vs amazon.sa 51.7%, "
    "stech.ink 56.9%, noon.com 49.3% (all three proxied and all three below "
    "the 70% HIGH floor)."
)


def _r_domain_success(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    if not snapshot.domains_available:
        return [
            _alert(
                "domains.unavailable",
                Severity.HIGH,
                Category.RELIABILITY,
                f"Per-domain stats could not be read: "
                f"{snapshot.domains_unavailable_reason}",
                "Blind per-domain stats hide both quality and cost regressions.",
                runbook="#per-domain-success-drop",
            )
        ]
    out: list[Alert] = []
    for d in snapshot.domains_24h:
        rate = d.success_rate
        if rate is None or d.attempts < t.domain_success_min_attempts:
            continue
        if rate < t.domain_success_rate_high:
            severity, threshold = Severity.HIGH, t.domain_success_rate_high
        elif rate < t.domain_success_rate_warning:
            severity, threshold = Severity.WARNING, t.domain_success_rate_warning
        else:
            continue
        out.append(
            _alert(
                "reliability.domain_success_rate",
                severity,
                Category.RELIABILITY,
                f"{d.domain}: {rate:.1%} success over {d.attempts} attempts in 24h.",
                _J_SUCCESS,
                subject=d.domain,
                observed={
                    "success_rate": round(rate, 4),
                    "attempts": d.attempts,
                    "threshold": threshold,
                },
                runbook="#per-domain-success-drop",
            )
        )
    return out


_J_QUEUE = (
    "Audit §12 requires queue age and stale jobs on the dashboard. A target "
    "stuck non-terminal holds a Scrapyd slot and a match lock, and the "
    "documented railway-migration trap is exactly this: wedged spiders block "
    "all 8 slots and nothing reports it. Measured 2026-08-15: 16 DEFERRED "
    "targets, the oldest 35 days old, never re-dispatched."
)


def _r_queue(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    q = snapshot.queue
    if not q.available:
        return [
            _alert(
                "queue.unavailable",
                Severity.HIGH,
                Category.RELIABILITY,
                f"Queue health could not be read: {q.unavailable_reason}",
                "Blind queue depth is how stuck jobs stay stuck.",
                runbook="#stop-dispatch-safely",
            )
        ]
    out: list[Alert] = []
    checks = (
        (
            "queue.pending_target_age",
            q.oldest_pending_target_age_seconds,
            t.target_pending_age_seconds,
            "PENDING",
        ),
        (
            "queue.started_target_age",
            q.oldest_started_target_age_seconds,
            t.target_started_age_seconds,
            "STARTED",
        ),
        (
            "queue.deferred_target_age",
            q.oldest_deferred_target_age_seconds,
            t.target_deferred_age_seconds,
            "DEFERRED",
        ),
    )
    for rule_id, age, limit, status in checks:
        if age is not None and age > limit:
            out.append(
                _alert(
                    rule_id,
                    Severity.WARNING if status == "DEFERRED" else Severity.HIGH,
                    Category.RELIABILITY,
                    f"Oldest {status} scrape target is {age / 3600:.1f} hours old "
                    f"({q.targets_by_status.get(status, 0)} in {status}).",
                    _J_QUEUE,
                    subject=status,
                    observed={
                        "age_seconds": round(age),
                        "threshold_seconds": limit,
                        "count": q.targets_by_status.get(status, 0),
                    },
                    runbook="#stop-dispatch-safely",
                )
            )
    if (
        q.oldest_pending_job_age_seconds is not None
        and q.oldest_pending_job_age_seconds > t.target_pending_age_seconds
    ):
        out.append(
            _alert(
                "queue.pending_job_age",
                Severity.HIGH,
                Category.RELIABILITY,
                f"Oldest PENDING scrape job is "
                f"{q.oldest_pending_job_age_seconds / 3600:.1f} hours old "
                f"({q.jobs_pending} pending).",
                _J_QUEUE,
                observed={
                    "age_seconds": round(q.oldest_pending_job_age_seconds),
                    "jobs_pending": q.jobs_pending,
                },
                runbook="#stop-dispatch-safely",
            )
        )
    return out


_J_FRESHNESS = (
    "The four failures this cycle found were all silent, and a threshold "
    "dashboard cannot see a system that has stopped producing numbers at "
    "all: every 'bad value' rule passes trivially on an empty window. "
    "Measured 2026-08-15: the last request attempt was 2026-08-12 21:04Z "
    "(2.6 days) and refresh_rules held ZERO rows, so nothing was ever going "
    "to scrape on a schedule. Nothing reported either fact."
)


def _r_freshness(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    f = snapshot.freshness
    if not f.available:
        return [
            _alert(
                "freshness.unavailable",
                Severity.HIGH,
                Category.RELIABILITY,
                f"Freshness could not be read: {f.unavailable_reason}",
                _J_FRESHNESS,
                runbook="#pipeline-silent",
            )
        ]
    out: list[Alert] = []
    age = f.seconds_since_attempt
    if age is None:
        out.append(
            _alert(
                "freshness.no_attempts_ever",
                Severity.HIGH,
                Category.RELIABILITY,
                "No request attempt has ever been recorded.",
                _J_FRESHNESS,
                runbook="#pipeline-silent",
            )
        )
    elif age > t.freshness_attempt_critical_seconds:
        out.append(
            _alert(
                "freshness.pipeline_silent",
                Severity.CRITICAL,
                Category.RELIABILITY,
                f"No scrape attempt for {age / 3600:.1f} hours — the pipeline "
                "is silent.",
                _J_FRESHNESS,
                observed={
                    "seconds_since_attempt": round(age),
                    "threshold_seconds": t.freshness_attempt_critical_seconds,
                },
                runbook="#pipeline-silent",
            )
        )
    elif age > t.freshness_attempt_high_seconds:
        out.append(
            _alert(
                "freshness.pipeline_silent",
                Severity.HIGH,
                Category.RELIABILITY,
                f"No scrape attempt for {age / 3600:.1f} hours.",
                _J_FRESHNESS,
                observed={
                    "seconds_since_attempt": round(age),
                    "threshold_seconds": t.freshness_attempt_high_seconds,
                },
                runbook="#pipeline-silent",
            )
        )
    if f.refresh_rules_enabled == 0:
        out.append(
            _alert(
                "freshness.no_refresh_rules",
                Severity.HIGH,
                Category.RELIABILITY,
                f"Zero enabled refresh rules ({f.refresh_rules_total} total) — "
                "nothing is scraped on a schedule.",
                _J_FRESHNESS,
                observed={
                    "refresh_rules_total": f.refresh_rules_total,
                    "refresh_rules_enabled": f.refresh_rules_enabled,
                },
                runbook="#pipeline-silent",
            )
        )
    return out


# --------------------------------------------------------------------------
# INFRA
# --------------------------------------------------------------------------

_J_REDIS = (
    "Audit H2: the same Redis carries the Celery broker, match locks, rate "
    "ceilings, cooldowns AND the monthly proxy budget ledger. An eviction on "
    "this instance simultaneously duplicates paid work, erases the spend "
    "ledger and drops throttles — so a single evicted key is a correctness "
    "AND a cost incident, which is why the eviction threshold is zero."
)


def _r_redis(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    r = snapshot.redis
    out: list[Alert] = []
    if r.policy_status == "VIOLATION":
        out.append(
            _alert(
                "redis.policy_violation",
                Severity.CRITICAL,
                Category.INFRA,
                f"Redis maxmemory-policy is {r.policy!r}, not noeviction.",
                _J_REDIS,
                observed={"policy": r.policy, "maxmemory": r.maxmemory},
                runbook="#redis-recovery",
            )
        )
    elif r.policy_status == "UNKNOWN":
        out.append(
            _alert(
                "redis.policy_unknown",
                Severity.WARNING,
                Category.INFRA,
                f"Redis maxmemory-policy could not be verified: {r.policy_detail}",
                _J_REDIS
                + " UNKNOWN is deliberately not fatal (see redis_policy's "
                "failure-mode note) but it must still be visible.",
                runbook="#redis-recovery",
            )
        )
    elif not r.available:
        out.append(
            _alert(
                "redis.unavailable",
                Severity.WARNING,
                Category.INFRA,
                f"Redis posture unknown: {r.unavailable_reason}",
                _J_REDIS,
                runbook="#redis-recovery",
            )
        )
    if r.evicted_keys:
        out.append(
            _alert(
                "redis.evictions",
                Severity.CRITICAL,
                Category.INFRA,
                f"Redis has evicted {r.evicted_keys} key(s).",
                _J_REDIS,
                observed={"evicted_keys": r.evicted_keys},
                runbook="#redis-recovery",
            )
        )
    util = r.memory_utilisation
    if util is not None and util >= t.redis_memory_high_fraction:
        out.append(
            _alert(
                "redis.memory",
                Severity.HIGH,
                Category.INFRA,
                f"Redis memory at {util:.0%} of maxmemory.",
                _J_REDIS + " Eviction pressure precedes eviction.",
                observed={
                    "used_memory": r.used_memory,
                    "utilisation": round(util, 3),
                    "threshold": t.redis_memory_high_fraction,
                },
                runbook="#redis-recovery",
            )
        )
    return out


_J_BREAKER = (
    "Audit H3: paid-proxy ceilings fail open exactly when accounting is "
    "impaired. The Postgres-backed breaker is the independent stop-loss. It "
    "cannot stop anything if it is not deployed (its table is missing from "
    "the running database) or if its evaluator has stopped taking the lease, "
    "and both of those are silent by construction."
)


def _r_breaker(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    b = snapshot.breaker
    if not b.available:
        return [
            _alert(
                "breaker.unavailable",
                Severity.HIGH,
                Category.COST,
                f"Proxy circuit breaker could not be read: {b.unavailable_reason} "
                "— the independent spend stop-loss is not in effect.",
                _J_BREAKER,
                runbook="#stop-proxy-spend-now",
            )
        ]
    out: list[Alert] = []
    if b.state == "OPEN":
        out.append(
            _alert(
                "breaker.open",
                Severity.CRITICAL,
                Category.COST,
                f"Proxy circuit breaker is OPEN ({b.trip_reason}): {b.detail}. "
                "All new paid requests are denied until a human closes it.",
                _J_BREAKER + " Recovery is deliberately manual.",
                observed={
                    "trip_reason": b.trip_reason,
                    "trip_count": b.trip_count,
                    "tripped_at": str(b.tripped_at),
                },
                runbook="#close-the-breaker",
            )
        )
    if b.state is None:
        out.append(
            _alert(
                "breaker.never_evaluated",
                Severity.HIGH,
                Category.COST,
                "Proxy circuit breaker has no state row — the evaluator has "
                "never run.",
                _J_BREAKER,
                runbook="#stop-proxy-spend-now",
            )
        )
    elif (
        b.seconds_since_evaluation is not None
        and b.seconds_since_evaluation > t.breaker_evaluator_stale_seconds
    ):
        out.append(
            _alert(
                "breaker.evaluator_stale",
                Severity.HIGH,
                Category.COST,
                f"Proxy breaker last evaluated "
                f"{b.seconds_since_evaluation / 60:.0f} minutes ago — the "
                "stop-loss is not being re-checked.",
                _J_BREAKER,
                observed={
                    "seconds_since_evaluation": round(b.seconds_since_evaluation),
                    "threshold_seconds": t.breaker_evaluator_stale_seconds,
                },
                runbook="#stop-proxy-spend-now",
            )
        )
    return out


def _r_scrapyd(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    s = snapshot.scrapyd
    if s.available and s.in_flight_targets >= t.scrapyd_in_flight_warning:
        return [
            _alert(
                "scrapyd.saturation",
                Severity.WARNING,
                Category.INFRA,
                f"{s.in_flight_targets} scrape targets are in flight "
                "(PENDING/STARTED).",
                "Scrapers are capped at 8 concurrent slots at both levels "
                "(railway-migration note). A large in-flight population with a "
                "fixed slot count is queue delay, and the documented failure "
                "mode is wedged spiders holding every slot.",
                observed={
                    "in_flight_targets": s.in_flight_targets,
                    "scrapyd_pending": s.pending,
                    "scrapyd_running": s.running,
                    "threshold": t.scrapyd_in_flight_warning,
                },
                runbook="#stop-dispatch-safely",
            )
        ]
    return []


def _r_optimizer(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    o = snapshot.optimizer
    rate = o.churn_rate_24h
    if (
        o.available
        and rate is not None
        and o.profiles_total >= t.optimizer_churn_min_profiles
        and rate >= t.optimizer_churn_high_fraction
    ):
        return [
            _alert(
                "optimizer.churn",
                Severity.WARNING,
                Category.RELIABILITY,
                f"{o.changed_24h}/{o.profiles_total} strategy profiles "
                f"({rate:.0%}) were rewritten in 24h.",
                "A profile set that rewrites itself daily is chasing noise or "
                "overwriting an operator's manual edit — the documented "
                "learned-profile-overrides-policy failure mode, where changing "
                "access_policies by hand silently does nothing. Profile churn "
                "also changes which requests are paid, so it is a cost signal.",
                observed={
                    "changed_24h": o.changed_24h,
                    "profiles_total": o.profiles_total,
                    "churn_rate": round(rate, 3),
                    "threshold": t.optimizer_churn_high_fraction,
                },
                runbook="#per-domain-success-drop",
            )
        ]
    return []


def _r_rls(snapshot: OpsSnapshot, _t: Thresholds) -> list[Alert]:
    d = snapshot.db_role
    if d.available and d.rls_effective is False:
        return [
            _alert(
                "security.rls_inert",
                Severity.CRITICAL,
                Category.SECURITY,
                f"This service connects as {d.role!r}, which is "
                f"{'superuser' if d.is_superuser else 'BYPASSRLS'} — every "
                "workspace-isolation policy in the schema is inert.",
                "Audit C3: RLS protection depends on unverified, manually "
                "created roles, and production was found connecting as a "
                "superuser. FORCE ROW LEVEL SECURITY on 40 tables provides "
                "exactly zero isolation against a BYPASSRLS role, and nothing "
                "in the running system says so.",
                observed={
                    "role": d.role,
                    "is_superuser": d.is_superuser,
                    "bypasses_rls": d.bypasses_rls,
                },
                runbook="#rls-inert",
            )
        ]
    return []


# --------------------------------------------------------------------------
# EPA B9 (F22, audit §13 Operations) — new alert rules
#
# Every gauge below is read via `getattr(snapshot, "<name>", None)` rather
# than a declared `OpsSnapshot` field. Wiring the actual DB/Redis
# collection into `OpsSnapshot`/`collect_snapshot`
# (`app_shared.opsmetrics.snapshot`) is out of this task's file scope (EPA
# plan-core-production-readiness-2026-09-07, packet PB-8/B9 — its Modify
# list is `heartbeat.py`, this file, `admin_ops.py`, and the scrapyd app;
# `snapshot.py`/`emit.py` are not in it). Each rule is INERT (returns no
# alerts) against any snapshot that does not carry the attribute — the
# same "stated, not yet wired" holding pattern this module already uses
# above for `ATTEMPTS_PER_VALID_FRESH_MAX`/`PERSISTENCE_FAILURES_1H_MAX`/
# `DISPATCH_AMBIGUOUS_INTENTS_MAX`/`COST_MODEL_DRIFT_RATIO_MIN/MAX` — and
# is fully implemented and unit-tested against a synthetic double
# (`tests/unit/test_ops_rules_new.py`) in the meantime, so wiring the
# attribute later is the only remaining step, not a rewrite.
#
# `queue_oldest_pending_seconds > 3600` (also in the plan's list) is NOT
# duplicated here: `_r_queue`'s existing `queue.pending_target_age` rule
# already fires at exactly this condition
# (`Thresholds.target_pending_age_seconds == 3_600.0`, evaluated against
# the already-wired `snapshot.queue.oldest_pending_target_age_seconds`) —
# see `test_ops_rules_new.py` for the ±1 coverage the plan asks for.
# --------------------------------------------------------------------------

_J_B9 = (
    "EPA B9 (audit §13 Operations): every process class needs a signal an "
    "operator can act on before it becomes an incident. See the module "
    "comment above this rule for why it is INERT until a later task "
    "attaches the named attribute to a real `OpsSnapshot`."
)


def _r_freshness_fraction_24h(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    fraction = getattr(snapshot, "freshness_fraction_24h", None)
    if fraction is None or fraction >= t.freshness_fraction_24h_min:
        return []
    return [
        _alert(
            "freshness.fraction_24h",
            Severity.HIGH,
            Category.RELIABILITY,
            f"Only {fraction:.0%} of ACTIVE matches had a fresh price in the "
            "last 24h.",
            _J_B9,
            observed={
                "freshness_fraction_24h": round(fraction, 4),
                "threshold": t.freshness_fraction_24h_min,
            },
        )
    ]


def _r_persistence_backlog(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    out: list[Alert] = []
    pending = getattr(snapshot, "persistence_pending_batches", None)
    if pending is not None and pending > t.persistence_pending_batches_high:
        out.append(
            _alert(
                "persistence.pending_batches",
                Severity.HIGH,
                Category.RELIABILITY,
                f"{pending} persistence batches pending — the writer is not "
                "draining the backlog.",
                _J_B9,
                observed={
                    "pending_batches": pending,
                    "threshold": t.persistence_pending_batches_high,
                },
            )
        )
    quarantined = getattr(snapshot, "persistence_quarantined_batches", None)
    if quarantined is not None and quarantined > 0:
        out.append(
            _alert(
                "persistence.quarantined_batches",
                Severity.CRITICAL,
                Category.RELIABILITY,
                f"{quarantined} persistence batches quarantined — data that "
                "arrived is not being written.",
                _J_B9,
                observed={"quarantined_batches": quarantined},
            )
        )
    return out


def _r_breaker_evaluation_lag(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    """Earlier-warning companion to `_r_breaker`'s `breaker.evaluator_stale`
    (3000s/HIGH): fires WARNING at 900s, well before the existing rule's
    territory, using the SAME already-wired `snapshot.breaker` field — no
    new attribute needed."""
    age = snapshot.breaker.seconds_since_evaluation
    if age is None or age <= t.breaker_evaluation_lag_warning_seconds:
        return []
    return [
        _alert(
            "breaker.evaluation_lag",
            Severity.WARNING,
            Category.COST,
            f"Proxy breaker last evaluated {age / 60:.0f} minutes ago — "
            "falling behind its lease cadence.",
            _J_BREAKER,
            observed={
                "seconds_since_evaluation": round(age),
                "threshold_seconds": t.breaker_evaluation_lag_warning_seconds,
            },
            runbook="#stop-proxy-spend-now",
        )
    ]


def _r_costauth_budget_denials(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    denials_by_reason = getattr(snapshot, "costauth_denials_1h_by_reason", None)
    if not denials_by_reason:
        return []
    budget_denials = denials_by_reason.get("BUDGET", 0) or 0
    if budget_denials <= 0:
        return []
    return [
        _alert(
            "costauth.budget_denials_1h",
            Severity.HIGH,
            Category.COST,
            f"{budget_denials} request(s) denied for reason=BUDGET in the "
            "last hour — a workspace or the fleet ceiling is exhausted.",
            _J_B9,
            observed={"denials_1h": budget_denials, "reason": "BUDGET"},
        )
    ]


def _r_disk_free(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    by_mount = getattr(snapshot, "disk_free_fraction_by_mount", None)
    if not by_mount:
        return []
    out: list[Alert] = []
    for mount, fraction in sorted(by_mount.items()):
        if fraction is not None and fraction < t.disk_free_fraction_critical:
            out.append(
                _alert(
                    "infra.disk_free",
                    Severity.CRITICAL,
                    Category.INFRA,
                    f"{mount}: only {fraction:.0%} free — the next write "
                    "(a partition INSERT, a backup, a log line) may fail.",
                    _J_B9,
                    subject=mount,
                    observed={
                        "free_fraction": round(fraction, 4),
                        "threshold": t.disk_free_fraction_critical,
                    },
                )
            )
    return out


def _r_restore_verify(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    failed = getattr(snapshot, "restore_verify_failed", None)
    if not failed:
        return []
    return [
        _alert(
            "backup.restore_verify_failed",
            Severity.CRITICAL,
            Category.DATA,
            "The most recent backup restore verification failed — a backup "
            "that has never been proven restorable is not a backup.",
            _J_B9,
        )
    ]


def _r_ledger_linked_attempt_fraction(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    fraction = getattr(snapshot, "ledger_linked_attempt_fraction_24h", None)
    if fraction is None or fraction >= t.ledger_linked_attempt_fraction_24h_min:
        return []
    return [
        _alert(
            "cost.ledger_linked_attempt_fraction_24h",
            Severity.WARNING,
            Category.COST,
            f"Only {fraction:.0%} of the last 24h's request attempts linked "
            "to a network-ledger operation — cost accounting coverage is "
            "dropping.",
            _J_B9,
            observed={
                "linked_attempt_fraction_24h": round(fraction, 4),
                "threshold": t.ledger_linked_attempt_fraction_24h_min,
            },
        )
    ]


def _r_dispatch_ambiguous_sustained(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    count = getattr(snapshot, "dispatch_ambiguous_intents", None)
    oldest_seconds = getattr(snapshot, "dispatch_ambiguous_intents_oldest_seconds", None)
    if not count or oldest_seconds is None:
        return []
    if oldest_seconds <= t.dispatch_ambiguous_intents_sustained_seconds:
        return []
    return [
        _alert(
            "dispatch.ambiguous_intents_sustained",
            Severity.HIGH,
            Category.RELIABILITY,
            f"{count} dispatch intent(s) still POSTED and unresolved after "
            f"{oldest_seconds / 60:.0f} minutes — the run may or may not "
            "exist on the node.",
            _J_B9,
            observed={
                "ambiguous_intents": count,
                "oldest_seconds": round(oldest_seconds),
                "threshold_seconds": t.dispatch_ambiguous_intents_sustained_seconds,
            },
        )
    ]


def _r_heartbeat_missing(snapshot: OpsSnapshot, t: Thresholds) -> list[Alert]:
    missing = getattr(snapshot, "heartbeat_missing_services", None)
    if not missing:
        return []
    return [
        _alert(
            "heartbeat.missing",
            Severity.HIGH,
            Category.RELIABILITY,
            f"No fresh heartbeat instance for service {service!r}.",
            _J_B9,
            subject=service,
        )
        for service in sorted(missing)
    ]


# --------------------------------------------------------------------------
# Registry + entry point
# --------------------------------------------------------------------------

RULES: tuple[Rule, ...] = (
    Rule("partition.*", Category.DATA, "Partition existence", _J_PARTITION, _r_partition),
    Rule("rollup.*", Category.DATA, "Daily-rollup freshness", _J_ROLLUP, _r_rollup),
    Rule(
        "cost_rollup.*",
        Category.COST,
        "Network-ledger cost-rollup watermark/reconciliation health",
        _J_COST_ROLLUP,
        _r_cost_rollup,
    ),
    Rule("outbox.*", Category.DELIVERY, "Outbox backlog/dead", _J_OUTBOX, _r_outbox),
    Rule(
        "cost.requests_per_url",
        Category.COST,
        "Requests and paid requests per unique link",
        _J_REQ_PER_URL,
        _r_requests_per_url,
    ),
    Rule(
        "cost.spend_per_domain_per_day",
        Category.COST,
        "Spend per domain per day",
        _J_DOMAIN_SPEND,
        _r_domain_spend,
    ),
    Rule(
        "cost.wasted_paid_rate",
        Category.COST,
        "Paid attempts producing no observation",
        _J_WASTED,
        _r_wasted_spend,
    ),
    Rule(
        "cost.cost_per_successful_price",
        Category.COST,
        "Cost per successful price, and any spend with zero prices",
        _J_COST_PER_PRICE,
        _r_cost_per_successful_price,
    ),
    Rule(
        "cost.month_end_forecast",
        Category.COST,
        "Month-end spend forecast and acceleration",
        _J_FORECAST,
        _r_spend_forecast,
    ),
    Rule(
        "discovery.*",
        Category.COST,
        "Discovery runs per domain per day, and surge",
        _J_DISCOVERY,
        _r_discovery,
    ),
    Rule(
        "reliability.domain_success_rate",
        Category.RELIABILITY,
        "Per-domain success rate",
        _J_SUCCESS,
        _r_domain_success,
    ),
    Rule("queue.*", Category.RELIABILITY, "Queue depth and age", _J_QUEUE, _r_queue),
    Rule(
        "freshness.*",
        Category.RELIABILITY,
        "Pipeline liveness and schedule existence",
        _J_FRESHNESS,
        _r_freshness,
    ),
    Rule("redis.*", Category.INFRA, "Redis policy, memory, evictions", _J_REDIS, _r_redis),
    Rule("breaker.*", Category.COST, "Proxy circuit-breaker posture", _J_BREAKER, _r_breaker),
    Rule(
        "scrapyd.saturation",
        Category.INFRA,
        "Scrapyd saturation",
        "Fixed 8-slot concurrency; wedged spiders hold slots silently.",
        _r_scrapyd,
    ),
    Rule(
        "optimizer.churn",
        Category.RELIABILITY,
        "Strategy-profile churn",
        "Churn changes which requests are paid.",
        _r_optimizer,
    ),
    Rule(
        "security.rls_inert",
        Category.SECURITY,
        "RLS effectiveness of the connected role",
        "Audit C3.",
        _r_rls,
    ),
    # --- EPA B9 (F22) — inert until a later task wires the named
    # attribute onto a real OpsSnapshot; see the section comment above.
    Rule(
        "freshness.fraction_24h",
        Category.RELIABILITY,
        "24h price-freshness fraction",
        _J_B9,
        _r_freshness_fraction_24h,
    ),
    Rule(
        "persistence.*",
        Category.RELIABILITY,
        "Persistence pending/quarantined batch backlog",
        _J_B9,
        _r_persistence_backlog,
    ),
    Rule(
        "breaker.evaluation_lag",
        Category.COST,
        "Proxy breaker evaluation lag (early warning)",
        _J_BREAKER,
        _r_breaker_evaluation_lag,
    ),
    Rule(
        "costauth.budget_denials_1h",
        Category.COST,
        "Cost-authorization denials for reason=BUDGET",
        _J_B9,
        _r_costauth_budget_denials,
    ),
    Rule(
        "infra.disk_free",
        Category.INFRA,
        "Host and volume free-disk fraction",
        _J_B9,
        _r_disk_free,
    ),
    Rule(
        "backup.restore_verify_failed",
        Category.DATA,
        "Backup restore verification",
        _J_B9,
        _r_restore_verify,
    ),
    Rule(
        "cost.ledger_linked_attempt_fraction_24h",
        Category.COST,
        "Network-ledger linked-attempt coverage",
        _J_B9,
        _r_ledger_linked_attempt_fraction,
    ),
    Rule(
        "dispatch.ambiguous_intents_sustained",
        Category.RELIABILITY,
        "Dispatch intents stuck POSTED past the resolution window",
        _J_B9,
        _r_dispatch_ambiguous_sustained,
    ),
    Rule(
        "heartbeat.missing",
        Category.RELIABILITY,
        "Missing fresh heartbeat instance per declared service",
        _J_B9,
        _r_heartbeat_missing,
    ),
)

_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.WARNING: 2}


def evaluate(
    snapshot: OpsSnapshot, thresholds: Thresholds | None = None
) -> list[Alert]:
    """Evaluate every rule against ``snapshot``, most severe first.

    A rule that raises is itself reported as a HIGH ``rule.error`` alert
    rather than aborting the pass: an alerting engine that goes silent
    because one rule has a bug is the failure mode this whole module
    exists to prevent.
    """
    t = thresholds or DEFAULT_THRESHOLDS
    alerts: list[Alert] = []
    for rule in RULES:
        try:
            alerts.extend(rule.evaluate(snapshot, t))
        except Exception as exc:  # noqa: BLE001 - see docstring
            alerts.append(
                _alert(
                    "rule.error",
                    Severity.HIGH,
                    rule.category,
                    f"Rule {rule.rule_id} raised {exc.__class__.__name__}: {exc}",
                    "A rule that cannot run is a blind spot.",
                    subject=rule.rule_id,
                )
            )
    alerts.sort(key=lambda a: (_SEVERITY_ORDER[a.severity], a.rule_id, a.subject or ""))
    return alerts


def worst_severity(alerts: list[Alert]) -> Severity | None:
    """Highest severity among ``alerts``, or ``None`` when all clear."""
    if not alerts:
        return None
    return min(alerts, key=lambda a: _SEVERITY_ORDER[a.severity]).severity


__all__ = [
    "DEFAULT_THRESHOLDS",
    "RULES",
    "Alert",
    "Category",
    "Rule",
    "Severity",
    "Thresholds",
    "evaluate",
    "worst_severity",
]
