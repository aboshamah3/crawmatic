"""Delivery: structured JSON log lines, and a Prometheus text rendering.

Conforms to the repository's existing observability convention
(`specs/011-rate-limiting-inflight-locks/contracts/observability.md`):
**one single-line JSON object per event, with an ``event`` key**, logged
through the stdlib ``logging`` module. No new transport, no new
dependency.

Event vocabulary added by this module (namespaced ``ops.*`` so it never
collides with the existing ``rate_limit.*``, ``dedup.*``, ``proxy_ledger.*``,
``proxy_breaker.*`` and ``redis.policy.*`` families):

===================== ======== ===============================================
Event                 Level    Meaning
===================== ======== ===============================================
``ops.snapshot``      INFO     The full metric snapshot, one line.
``ops.alert``         ERROR/   One firing rule. Level tracks severity:
                      WARNING  CRITICAL/HIGH -> ERROR, WARNING -> WARNING.
``ops.all_clear``     INFO     A snapshot with zero firing alerts.
===================== ======== ===============================================

Why one line per alert rather than one blob: Railway's log view filters
on exact text and key/value pairs, so ``event=ops.alert severity=CRITICAL``
is a usable saved filter, whereas a nested array inside a single line is
not. Railway cannot *alert* on either (investigated 2026-08-15 — its
monitors cover CPU/RAM/disk/egress only), so these lines are for the
human reading the dashboard and for any external log drain; the
authoritative alerting path is a poller against the ops endpoint. See
``docs/ops/OBSERVABILITY_SLO_AND_ALERTS.md``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from app_shared.opsmetrics.rules import (
    ATTEMPTS_PER_VALID_FRESH_MAX,
    COST_MODEL_DRIFT_RATIO_MAX,
    COST_MODEL_DRIFT_RATIO_MIN,
    DISPATCH_AMBIGUOUS_INTENTS_MAX,
    PERSISTENCE_FAILURES_1H_MAX,
    Alert,
    Severity,
    evaluate,
    worst_severity,
)
from app_shared.opsmetrics.snapshot import OpsSnapshot

logger = logging.getLogger("app_shared.opsmetrics")

SNAPSHOT_EVENT = "ops.snapshot"
ALERT_EVENT = "ops.alert"
ALL_CLEAR_EVENT = "ops.all_clear"

_LEVEL_FOR_SEVERITY = {
    Severity.CRITICAL: logging.ERROR,
    Severity.HIGH: logging.ERROR,
    Severity.WARNING: logging.WARNING,
}


def _line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, separators=(",", ":"))


# --------------------------------------------------------------------------
# Ledger coverage (EPA A7, deep dive §8.2)
#
# Deliberately NOT folded into `OpsSnapshot`/`collect_snapshot`
# (`app_shared.opsmetrics.snapshot`, out of this task's file scope and
# shared with a LATER EPA task) -- these two functions are pure, take
# already-collected counts, and stay self-contained here so that whoever
# wires the DB collection (a `request_attempts`/`network_operations`
# query, most naturally a new `OpsSnapshot` field) can compose them
# without this module and that one touching the same lines at once.
#
# The two counts these fractions need:
#   * `crawmatic_ledger_linked_attempt_fraction_24h` --
#     COUNT(request_attempts WHERE network_operation_id IS NOT NULL)
#     / COUNT(request_attempts), both over the last 24h
#     (`created_at >= now() - interval '24 hours'`).
#   * `crawmatic_ledger_bytes_missing_fraction_24h` --
#     COUNT(network_operations WHERE transport = 'PROXY'
#           AND bytes_compressed IS NULL)
#     / COUNT(network_operations WHERE transport = 'PROXY'), both over
#     the last 24h (`created_at >= now() - interval '24 hours'`).
#
# Alert thresholds -- WARNING when linked-attempt coverage drops below
# 95%, WARNING when the proxied-bytes-missing fraction rises above 5% --
# are named here for B9 to wire as `Rule(...)` entries in
# `app_shared.opsmetrics.rules` against whatever `OpsSnapshot` field ends
# up carrying these fractions. Nothing evaluates them yet; naming them
# now is so B9 does not have to re-derive "what counts as bad" from
# scratch.
LEDGER_LINKED_ATTEMPT_FRACTION_MIN = 0.95
LEDGER_BYTES_MISSING_FRACTION_MAX = 0.05


@dataclass(frozen=True)
class LedgerCoverage:
    """The two A7 coverage fractions, already computed. ``None`` means
    "no signal" (the relevant count was zero over the window), the same
    convention `opsmetrics.snapshot._ratio` uses -- never a fabricated
    0%/100%."""

    linked_attempt_fraction_24h: float | None = None
    bytes_missing_fraction_24h: float | None = None


def ledger_linked_attempt_fraction(*, total_attempts: int, linked_attempts: int) -> float | None:
    """Fraction of ``request_attempts`` rows (24h) with a non-null
    ``network_operation_id``. ``None`` when the window saw zero attempts
    -- there is no coverage claim to make about a window with no rows."""
    if total_attempts <= 0:
        return None
    return linked_attempts / total_attempts


def ledger_bytes_missing_fraction(*, total_proxied: int, missing_bytes: int) -> float | None:
    """Fraction of PROXY ``network_operations`` (24h) with a null
    ``bytes_compressed``. ``None`` when the window saw zero proxied
    operations."""
    if total_proxied <= 0:
        return None
    return missing_bytes / total_proxied


def render_ledger_coverage_prometheus(coverage: LedgerCoverage) -> str:
    """Prometheus text exposition for the two A7 gauges, standalone.

    Kept separate from `render_prometheus` (below) for the same reason
    `LedgerCoverage` is kept out of `OpsSnapshot` -- see this section's
    module-level comment. A caller that already renders the main
    exposition can simply concatenate this function's output onto it.
    """
    lines: list[str] = []
    for name, value in (
        (
            "crawmatic_ledger_linked_attempt_fraction_24h",
            coverage.linked_attempt_fraction_24h,
        ),
        (
            "crawmatic_ledger_bytes_missing_fraction_24h",
            coverage.bytes_missing_fraction_24h,
        ),
    ):
        if value is None:
            continue
        kind, help_text = _PROM_HELP[name]
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.append(f"{name} {_fmt(float(value))}")
    return ("\n".join(lines) + "\n") if lines else ""


# --------------------------------------------------------------------------
# Baseline outcome metrics (EPA A5, deep dive §5)
#
# Everything already in `OpsSnapshot` measures the machinery: partitions
# exist, the outbox drains, the breaker is closed, attempts are being
# made. None of it measures the OUTCOME the product is sold on -- how
# many matches actually got a fresh price, and what we spent in attempts
# to get each one. A fleet can be perfectly "healthy" by every existing
# gauge while refreshing nothing, and did: the 2026-09-03 review found
# production had done ZERO proxied scraping for four days with no alert
# anywhere.
#
# These seven gauges are that missing half. Their NAMES are a contract
# consumed by B9 (alerting) and D5 (the readiness gates) -- do not rename
# them.
#
# Kept out of `OpsSnapshot`/`collect_snapshot` for exactly the reason the
# A7 ledger-coverage block above is (see its comment): `snapshot.py` is
# shared with later EPA tasks, and these functions are self-contained
# here so the two modules never have to be edited on the same lines at
# once. `collect_baseline_metrics` takes an already-opened session and,
# like `collect_snapshot`, degrades per-metric rather than raising -- a
# missing table must not blind the other six.
# --------------------------------------------------------------------------

#: Unique matches whose current price actually moved forward in the last
#: 24h. THE product-outcome number: not "attempts made", not "jobs run" --
#: matches a customer would see a fresh figure for.
FRESH_UNIQUE_MATCHES_24H_SQL = """
SELECT COUNT(DISTINCT match_id) AS fresh_unique_matches_24h
FROM match_current_prices
WHERE updated_at > now() - interval '24 hours'
"""

#: Attempts spent per freshly-priced match -- the amplification factor.
#: `NULLIF(...,0)` is what keeps a window with zero fresh matches
#: reporting NULL ("no signal") instead of a division error or a
#: fabricated 0; a run that refreshed nothing has no ratio, and saying so
#: is the honest answer.
ATTEMPTS_PER_VALID_FRESH_24H_SQL = """
SELECT
    attempts.attempts_24h AS attempts_24h,
    fresh.fresh_unique_matches_24h AS fresh_unique_matches_24h,
    attempts.attempts_24h::numeric
        / NULLIF(fresh.fresh_unique_matches_24h, 0) AS attempts_per_valid_fresh_24h
FROM
    (
        SELECT COUNT(*) AS attempts_24h
        FROM request_attempts
        WHERE created_at > now() - interval '24 hours'
    ) AS attempts,
    (
        SELECT COUNT(DISTINCT match_id) AS fresh_unique_matches_24h
        FROM match_current_prices
        WHERE updated_at > now() - interval '24 hours'
    ) AS fresh
"""

#: EPA C1 (F08): targets finalized in the last 24 h because they had
#: spent every physical attempt their budget allowed
#: (the scraping runtime's per-target attempt budget). Paired with
#: `crawmatic_attempts_per_valid_fresh_24h` this is C1's KPI, and the
#: pairing is the point: the amplification ratio alone cannot tell a
#: fleet that got EFFICIENT from one that simply gave up. A falling ratio
#: with a rising exhaustion count is the second thing, and it is a
#: regression, not a win. Counted from `request_attempts` rather than
#: `scrape_job_targets` so a target refused more than once (a redispatch)
#: is visible as the repeated spend it is.
ATTEMPT_BUDGET_EXHAUSTED_24H_SQL = """
SELECT COUNT(*) AS attempt_budget_exhausted_24h
FROM request_attempts
WHERE created_at > now() - interval '24 hours'
  AND error_code = 'ATTEMPT_BUDGET_EXHAUSTED'
"""

#: Attempts that SUCCEEDED on the wire in the last hour and yet left no
#: observation behind for their match. That gap is the signature of a
#: persistence failure -- money was spent, a page was fetched and parsed,
#: and nothing durable came out of it. Deliberately scoped to
#: `success IS TRUE`: a failed fetch legitimately produces no price, and
#: counting those would drown the signal in ordinary scrape failures.
PERSISTENCE_FAILURES_1H_SQL = """
SELECT COUNT(*) AS persistence_failures_1h
FROM request_attempts ra
WHERE ra.created_at > now() - interval '1 hour'
  AND ra.success IS TRUE
  AND NOT EXISTS (
      SELECT 1
      FROM price_observations po
      WHERE po.workspace_id = ra.workspace_id
        AND po.match_id = ra.match_id
        AND po.scraped_at > now() - interval '1 hour'
  )
"""

#: Dispatch intents stuck in `POSTED` -- the ONE genuinely ambiguous
#: state (the Scrapyd run may or may not exist on the node; see
#: `app_shared.enums.DispatchIntentState`). A row that has sat there for
#: five minutes is not in flight, it is unresolved, and every one of them
#: is either work that will never run or work that may run twice.
DISPATCH_AMBIGUOUS_INTENTS_SQL = """
SELECT COUNT(*) AS dispatch_ambiguous_intents
FROM dispatch_intents
WHERE state = 'POSTED'
  AND updated_at < now() - interval '5 minutes'
"""

#: Age of the oldest target nothing has picked up yet. Distinct from the
#: existing `crawmatic_target_oldest_age_seconds{status="PENDING"}` only
#: in being a top-level, unlabelled gate figure for D5 -- same fact, and
#: deliberately the same SQL shape, so the two can never disagree.
QUEUE_OLDEST_PENDING_SECONDS_SQL = """
SELECT EXTRACT(EPOCH FROM (now() - MIN(created_at))) AS queue_oldest_pending_seconds
FROM scrape_job_targets
WHERE status = 'PENDING'
"""

#: What our own ledger thinks we moved, over 24h, on the billed
#: (`PROXY`) path, against what the provider's own usage export says.
#: The provider figure comes from `provider_usage_records`, which
#: `scripts/import_dataimpulse_usage.py` fills.
#:
#: The ratio is `NULL` -- never `0`, never `1` -- whenever the provider
#: figure is absent or zero. That is the whole point of the gauge: an
#: un-imported window means we CANNOT say whether the cost model is
#: drifting, and a fabricated `0` would read as "perfect agreement" on
#: exactly the days nobody reconciled anything.
COST_MODEL_DRIFT_RATIO_SQL = """
SELECT
    ledger.ledger_bytes_24h AS ledger_bytes_24h,
    provider.provider_bytes_24h AS provider_bytes_24h,
    ledger.ledger_bytes_24h::numeric
        / NULLIF(provider.provider_bytes_24h, 0) AS cost_model_drift_ratio
FROM
    (
        SELECT SUM(bytes_compressed) AS ledger_bytes_24h
        FROM network_operations
        WHERE transport = 'PROXY'
          AND created_at > now() - interval '24 hours'
    ) AS ledger,
    (
        SELECT SUM(total_bytes) AS provider_bytes_24h
        FROM provider_usage_records
        WHERE window_end > now() - interval '24 hours'
    ) AS provider
"""

#: p95 of each phase of a target's life, over targets created in the last
#: 24h. `percentile_cont` (interpolating) rather than `percentile_disc`
#: so a small sample does not quantise to whichever single row happens to
#: sit at the 95th position.
#:
#: Every phase is `FILTER`ed to rows where BOTH boundaries are non-NULL.
#: A target that never reached a boundary has no duration for that phase
#: -- it is not a zero-length one -- and folding those in would make an
#: outage (nothing dispatched, so nothing has `dispatched_at`) look like
#: a suspiciously fast p95, which is the exact failure mode these gauges
#: exist to catch.
TARGET_PHASE_P95_SECONDS_SQL = """
SELECT
    percentile_cont(0.95) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (dispatched_at - created_at))
    ) FILTER (WHERE dispatched_at IS NOT NULL) AS due_to_dispatch,
    percentile_cont(0.95) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (first_network_at - dispatched_at))
    ) FILTER (
        WHERE first_network_at IS NOT NULL AND dispatched_at IS NOT NULL
    ) AS dispatch_to_first_network,
    percentile_cont(0.95) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (persisted_at - first_network_at))
    ) FILTER (
        WHERE persisted_at IS NOT NULL AND first_network_at IS NOT NULL
    ) AS first_network_to_persisted
FROM scrape_job_targets
WHERE created_at > now() - interval '24 hours'
"""

#: The three phase labels, in lifecycle order. This tuple IS the label
#: vocabulary of `crawmatic_target_phase_p95_seconds{phase=...}` (a
#: contract for B9/D5) and the column order of
#: :data:`TARGET_PHASE_P95_SECONDS_SQL`.
TARGET_PHASES: tuple[str, ...] = (
    "due_to_dispatch",
    "dispatch_to_first_network",
    "first_network_to_persisted",
)

# The thresholds for these seven gauges live in
# `app_shared.opsmetrics.rules` -- the judgement half of this trio -- and
# are re-exported here so a caller that already has `emit` imported can
# read a gauge and its limit from one place. They are imported at the top
# of this module (no cycle: `rules` does not import `emit`).


@dataclass(frozen=True)
class BaselineMetrics:
    """The A5 baseline gauges, already collected.

    ``None`` everywhere means "no signal" -- the window held no rows, the
    denominator was zero, or the section could not be read (in which case
    :attr:`unavailable_reasons` names the exception class, mirroring
    ``OpsSnapshot``'s per-section ``unavailable_reason``). It never means
    zero, and nothing here is ever coerced to zero.
    """

    fresh_unique_matches_24h: int | None = None
    attempts_24h: int | None = None
    attempts_per_valid_fresh_24h: float | None = None
    #: EPA C1 (F08). ``None`` means "could not be read", never zero --
    #: the same discipline as every gauge here, and it matters more for
    #: this one: an unreadable exhaustion count read as 0 would say "the
    #: budget never fired", which is the exact opposite of "we do not
    #: know".
    attempt_budget_exhausted_24h: int | None = None
    persistence_failures_1h: int | None = None
    dispatch_ambiguous_intents: int | None = None
    queue_oldest_pending_seconds: float | None = None
    ledger_bytes_24h: int | None = None
    provider_bytes_24h: int | None = None
    cost_model_drift_ratio: float | None = None
    #: ``phase -> p95 seconds``; keys are :data:`TARGET_PHASES`, values
    #: ``None`` for a phase with no completed pair in the window.
    target_phase_p95_seconds: dict[str, float | None] = field(default_factory=dict)
    #: ``metric group -> exception class name`` for the groups that could
    #: not be read at all.
    unavailable_reasons: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fresh_unique_matches_24h": self.fresh_unique_matches_24h,
            "attempts_24h": self.attempts_24h,
            "attempts_per_valid_fresh_24h": self.attempts_per_valid_fresh_24h,
            "attempt_budget_exhausted_24h": self.attempt_budget_exhausted_24h,
            "persistence_failures_1h": self.persistence_failures_1h,
            "dispatch_ambiguous_intents": self.dispatch_ambiguous_intents,
            "queue_oldest_pending_seconds": self.queue_oldest_pending_seconds,
            "ledger_bytes_24h": self.ledger_bytes_24h,
            "provider_bytes_24h": self.provider_bytes_24h,
            "cost_model_drift_ratio": self.cost_model_drift_ratio,
            "target_phase_p95_seconds": dict(self.target_phase_p95_seconds),
            "unavailable_reasons": dict(self.unavailable_reasons),
        }


def _as_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _as_int(value: Any) -> int | None:
    return None if value is None else int(value)


def collect_baseline_metrics(session: Any) -> BaselineMetrics:
    """Run the A5 baseline SQL against ``session``. Read-only; never raises.

    ``session`` is any SQLAlchemy-``Session``-shaped object; as with
    ``collect_snapshot`` it must be an unscoped/BYPASSRLS session for the
    fleet aggregates to be complete. Each metric group is read
    independently and a failing group degrades to ``None`` plus an entry
    in ``unavailable_reasons`` -- one table missing (production has run
    behind the code's migration head before) must not blind the rest.
    """
    from sqlalchemy import text  # local: keeps this module import-light

    values: dict[str, Any] = {}
    reasons: dict[str, str] = {}
    phases: dict[str, float | None] = {}

    def _read(group: str, sql: str) -> Any:
        try:
            return session.execute(text(sql)).one()
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            reasons[group] = type(exc).__name__
            session_rollback = getattr(session, "rollback", None)
            if callable(session_rollback):
                # A failed statement poisons the transaction in Postgres;
                # without this every SUBSEQUENT group would fail too and
                # report a misleading `InFailedSqlTransactionError`.
                try:
                    session_rollback()
                except Exception:  # noqa: BLE001 - best effort only
                    pass
            return None

    row = _read("freshness_amplification", ATTEMPTS_PER_VALID_FRESH_24H_SQL)
    if row is not None:
        values["fresh_unique_matches_24h"] = _as_int(row.fresh_unique_matches_24h)
        values["attempts_24h"] = _as_int(row.attempts_24h)
        values["attempts_per_valid_fresh_24h"] = _as_float(row.attempts_per_valid_fresh_24h)

    row = _read("attempt_budget", ATTEMPT_BUDGET_EXHAUSTED_24H_SQL)
    if row is not None:
        values["attempt_budget_exhausted_24h"] = _as_int(row.attempt_budget_exhausted_24h)

    row = _read("persistence_failures", PERSISTENCE_FAILURES_1H_SQL)
    if row is not None:
        values["persistence_failures_1h"] = _as_int(row.persistence_failures_1h)

    row = _read("dispatch_ambiguity", DISPATCH_AMBIGUOUS_INTENTS_SQL)
    if row is not None:
        values["dispatch_ambiguous_intents"] = _as_int(row.dispatch_ambiguous_intents)

    row = _read("queue_age", QUEUE_OLDEST_PENDING_SECONDS_SQL)
    if row is not None:
        values["queue_oldest_pending_seconds"] = _as_float(row.queue_oldest_pending_seconds)

    row = _read("cost_model_drift", COST_MODEL_DRIFT_RATIO_SQL)
    if row is not None:
        values["ledger_bytes_24h"] = _as_int(row.ledger_bytes_24h)
        values["provider_bytes_24h"] = _as_int(row.provider_bytes_24h)
        # NULL (never 0) when the provider figure is absent -- the SQL's
        # `NULLIF(provider_bytes_24h, 0)` already guarantees it; this
        # keeps the guarantee true even for a caller that hands us a row
        # built some other way.
        values["cost_model_drift_ratio"] = (
            None
            if not row.provider_bytes_24h
            else _as_float(row.cost_model_drift_ratio)
        )

    row = _read("target_phases", TARGET_PHASE_P95_SECONDS_SQL)
    if row is not None:
        for phase in TARGET_PHASES:
            phases[phase] = _as_float(getattr(row, phase))

    return BaselineMetrics(
        target_phase_p95_seconds=phases, unavailable_reasons=reasons, **values
    )


def render_baseline_metrics_prometheus(metrics: BaselineMetrics) -> str:
    """Prometheus text exposition for the seven A5 gauges, standalone.

    Standalone for the same reason `render_ledger_coverage_prometheus` is
    -- a caller that already renders the main exposition concatenates
    this onto it. A ``None`` value emits NO sample at all (rather than a
    `0`), so "we could not measure this" and "this is zero" stay
    distinguishable in the scrape, which is the entire discipline these
    gauges are built on.
    """
    lines: list[str] = []

    def _family(name: str, samples: list[str]) -> None:
        if not samples:
            return
        kind, help_text = _PROM_HELP[name]
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(samples)

    for name, value in (
        ("crawmatic_fresh_unique_matches_24h", metrics.fresh_unique_matches_24h),
        ("crawmatic_attempts_per_valid_fresh_24h", metrics.attempts_per_valid_fresh_24h),
        (
            "crawmatic_attempt_budget_exhausted_24h",
            metrics.attempt_budget_exhausted_24h,
        ),
        ("crawmatic_persistence_failures_1h", metrics.persistence_failures_1h),
        ("crawmatic_dispatch_ambiguous_intents", metrics.dispatch_ambiguous_intents),
        ("crawmatic_queue_oldest_pending_seconds", metrics.queue_oldest_pending_seconds),
        ("crawmatic_cost_model_drift_ratio", metrics.cost_model_drift_ratio),
    ):
        if value is None:
            continue
        _family(name, [f"{name} {_fmt(float(value))}"])

    phase_samples = [
        f'crawmatic_target_phase_p95_seconds{{phase="{_esc(phase)}"}} '
        f"{_fmt(float(metrics.target_phase_p95_seconds[phase]))}"
        for phase in TARGET_PHASES
        if metrics.target_phase_p95_seconds.get(phase) is not None
    ]
    _family("crawmatic_target_phase_p95_seconds", phase_samples)

    return ("\n".join(lines) + "\n") if lines else ""


def render_costauth_denials_prometheus(
    counts: Mapping[str, int] | None = None,
) -> str:
    """Prometheus exposition of `costauth_denials_by_reason` (EPA C6/F18).

    Standalone, for the same reason `render_ledger_coverage_prometheus`
    is: a caller that already renders the main exposition concatenates
    this onto it, and the fact it reports is process-local rather than a
    database query, so it does not belong inside `collect_snapshot`.

    `counts` defaults to the live tally in
    `app_shared.costauth.service`. It is a parameter at all so a test can
    render a known tally without reaching into module state, and so an
    aggregator that has collected several workers' tallies can render the
    sum.

    Unlike the gauges above, a zero here IS a fact: `costauth` pre-seeds
    every `DenialReason` at zero, and a reason that has never fired must
    still be a present series -- a dashboard cannot alert on the absence
    of a series it has never seen. Every reason is therefore emitted,
    zeros included. The import is function-local so this module stays
    import-light and `opsmetrics` does not pull the reservation ledger in
    at import time.
    """
    if counts is None:
        from app_shared.costauth.service import (  # local: keeps this module light
            costauth_denials_by_reason,
        )

        counts = costauth_denials_by_reason()
    if not counts:
        return ""
    name = "crawmatic_costauth_denials_total"
    kind, help_text = _PROM_HELP[name]
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    lines.extend(
        f'{name}{{reason="{_esc(reason)}"}} {_fmt(float(counts[reason]))}'
        for reason in sorted(counts)
    )
    return "\n".join(lines) + "\n"


def emit_alerts(alerts: Iterable[Alert]) -> int:
    """Log one ``ops.alert`` line per firing rule. Returns the count."""
    count = 0
    for alert in alerts:
        logger.log(
            _LEVEL_FOR_SEVERITY[alert.severity],
            _line({"event": ALERT_EVENT, **alert.as_dict()}),
        )
        count += 1
    return count


def emit_snapshot(
    snapshot: OpsSnapshot, alerts: list[Alert] | None = None
) -> list[Alert]:
    """Evaluate (if needed), log the snapshot and every firing alert.

    This is the one call a periodic task needs. Returns the alert list so
    a caller can act on it (e.g. push to an operator webhook).
    """
    firing = evaluate(snapshot) if alerts is None else alerts
    worst = worst_severity(firing)
    logger.info(
        _line(
            {
                "event": SNAPSHOT_EVENT,
                "alert_count": len(firing),
                "worst_severity": str(worst) if worst else None,
                "snapshot": snapshot.as_dict(),
            }
        )
    )
    if not firing:
        logger.info(
            _line({"event": ALL_CLEAR_EVENT, "collected_at": snapshot.collected_at})
        )
        return firing
    emit_alerts(firing)
    return firing


# --------------------------------------------------------------------------
# Prometheus text exposition
# --------------------------------------------------------------------------

_PROM_HELP: dict[str, tuple[str, str]] = {
    "crawmatic_partition_next_month_present": (
        "gauge",
        "1 if the next month's partition exists for this table",
    ),
    "crawmatic_partition_fuse_days": (
        "gauge",
        "Days until writes hit the next calendar month",
    ),
    "crawmatic_rollup_rows": ("gauge", "Rows in variant_price_daily_rollups"),
    "crawmatic_rollup_lag_days": ("gauge", "Days the daily rollup is behind"),
    "crawmatic_outbox_pending": ("gauge", "PENDING outbox messages"),
    "crawmatic_outbox_dead": ("gauge", "DEAD outbox messages"),
    "crawmatic_outbox_oldest_pending_seconds": ("gauge", "Age of oldest PENDING outbox row"),
    "crawmatic_breaker_open": ("gauge", "1 if the proxy circuit breaker is OPEN"),
    "crawmatic_breaker_seconds_since_evaluation": (
        "gauge",
        "Seconds since the breaker evaluator last ran",
    ),
    "crawmatic_targets_in_flight": ("gauge", "Scrape targets PENDING or STARTED"),
    "crawmatic_target_oldest_age_seconds": ("gauge", "Oldest target age by status"),
    "crawmatic_domain_attempts_24h": ("gauge", "Fetch attempts per domain, 24h"),
    "crawmatic_domain_success_rate_24h": ("gauge", "Success rate per domain, 24h"),
    "crawmatic_domain_requests_per_url_24h": ("gauge", "Requests per unique URL, 24h"),
    "crawmatic_domain_proxied_per_url_24h": ("gauge", "Paid requests per unique URL, 24h"),
    "crawmatic_domain_proxy_usd_24h": ("gauge", "Estimated proxy USD per domain, 24h"),
    "crawmatic_discovery_runs_24h": ("gauge", "Strategy discovery runs per domain, 24h"),
    "crawmatic_discovery_surge_factor": (
        "gauge",
        "Discovery runs vs the preceding 6-day daily mean",
    ),
    "crawmatic_proxied_requests_month_to_date": ("counter", "Paid attempts this month"),
    "crawmatic_proxy_usd_month_to_date": ("gauge", "Estimated paid spend this month"),
    "crawmatic_proxy_forecast_month_end_usd": ("gauge", "Month-end USD from 24h velocity"),
    "crawmatic_seconds_since_last_attempt": ("gauge", "Pipeline liveness"),
    "crawmatic_refresh_rules_enabled": ("gauge", "Enabled scheduled refresh rules"),
    "crawmatic_redis_evicted_keys": ("counter", "Redis evicted keys"),
    "crawmatic_redis_used_memory_bytes": ("gauge", "Redis used memory"),
    "crawmatic_rls_effective": ("gauge", "1 if the connected DB role is subject to RLS"),
    "crawmatic_alerts_firing": ("gauge", "Firing ops alerts by severity"),
    # EPA A7 (deep dive §8.2) -- see `render_ledger_coverage_prometheus` above.
    "crawmatic_ledger_linked_attempt_fraction_24h": (
        "gauge",
        "Fraction of 24h request_attempts rows with a non-null network_operation_id",
    ),
    "crawmatic_ledger_bytes_missing_fraction_24h": (
        "gauge",
        "Fraction of 24h proxied network_operations with a null bytes_compressed",
    ),
    # EPA C6 (F18) -- see `render_costauth_denials_prometheus` below. A
    # COUNTER, not a gauge: the value only ever grows within a process
    # and the scraper differentiates it, so a worker restart reads as a
    # reset rather than as a cliff in the denial rate.
    "crawmatic_costauth_denials_total": (
        "counter",
        "Cost authorizations refused, by denial reason",
    ),
    # EPA A5 (deep dive §5) -- see `render_baseline_metrics_prometheus` above.
    # These names are a contract consumed by B9 and D5; do not rename them.
    "crawmatic_fresh_unique_matches_24h": (
        "gauge",
        "Unique matches whose current price was refreshed in the last 24h",
    ),
    "crawmatic_attempts_per_valid_fresh_24h": (
        "gauge",
        "Fetch attempts spent per freshly-priced match, 24h",
    ),
    # EPA C1 (F08) -- read WITH the ratio above, never instead of it.
    "crawmatic_attempt_budget_exhausted_24h": (
        "gauge",
        "Attempts refused because the target's physical attempt budget was spent, 24h",
    ),
    "crawmatic_persistence_failures_1h": (
        "gauge",
        "Successful attempts in the last hour that persisted no observation",
    ),
    "crawmatic_dispatch_ambiguous_intents": (
        "gauge",
        "Dispatch intents stuck in POSTED for more than 5 minutes",
    ),
    "crawmatic_queue_oldest_pending_seconds": (
        "gauge",
        "Age of the oldest PENDING scrape target",
    ),
    "crawmatic_cost_model_drift_ratio": (
        "gauge",
        "Ledger proxied bytes divided by provider-reported bytes, 24h "
        "(absent when no provider figure has been imported)",
    ),
    "crawmatic_target_phase_p95_seconds": (
        "gauge",
        "p95 seconds per target lifecycle phase, over targets created in 24h",
    ),
    # EPA C9 (F14) -- see `render_storage_metrics_prometheus` below. These
    # five are the STORAGE half of the readiness picture: the four gauges
    # above say whether the fleet is producing value, these say whether it
    # is producing value at a cost per unit that stays flat as it grows.
    "crawmatic_bytes_per_physical_attempt": (
        "gauge",
        "Ledger bytes divided by physical network operations, 24h",
    ),
    "crawmatic_bytes_per_logical_target": (
        "gauge",
        "Ledger bytes divided by logical request attempts, 24h",
    ),
    "crawmatic_dead_tuple_fraction": (
        "gauge",
        "Dead tuples as a fraction of live+dead, per table",
    ),
    "crawmatic_index_bytes": ("gauge", "Total index size in bytes, per table"),
    "crawmatic_partition_maintenance_seconds": (
        "gauge",
        "Wall seconds the last partition-maintenance pass took",
    ),
}


# --------------------------------------------------------------------------
# Storage & per-unit-cost metrics (EPA C9, F14, deep dive §9.1)
#
# The A5 block above measures OUTCOME (matches refreshed, attempts spent
# per fresh match). These five measure the thing that decides whether
# that outcome is affordable at ten times the size:
#
#   * the two RATIOS are the unit economics of the wire -- bytes per
#     PHYSICAL operation and bytes per LOGICAL target. They are separate
#     gauges, not one, precisely because the gap between them is the
#     coalescing win: one browser navigation serving two workspaces makes
#     bytes-per-logical-target fall while bytes-per-physical-attempt
#     stays flat, and a single blended number would hide exactly that.
#   * `dead_tuple_fraction` and `index_bytes` are the two ways a table
#     stops being affordable without anyone noticing: bloat that vacuum
#     is not keeping up with, and indexes that grow faster than the heap.
#     Both are per-table LABELS because the answer is never fleet-wide --
#     it is always one table, and knowing which one is the whole point.
#   * `partition_maintenance_seconds` is the clock on the job that makes
#     the whole retention design work. If it starts climbing, the monthly
#     drop is turning into something that needs a window.
#
# Kept out of `OpsSnapshot`/`collect_snapshot` for the same reason the A5
# and A7 blocks are: `snapshot.py` is shared with other EPA tasks, and a
# self-contained function here means the two modules are never edited on
# the same lines at once. Like `collect_baseline_metrics`, this degrades
# PER METRIC -- one missing table must not blind the other four.
# --------------------------------------------------------------------------

#: Bytes moved per PHYSICAL operation, 24h. `NULLIF(...,0)` so a window
#: with no operations reports NULL ("no signal") rather than a fabricated
#: 0 -- the same honesty rule the A5 ratios follow.
BYTES_PER_PHYSICAL_ATTEMPT_SQL = """
SELECT SUM(COALESCE(bytes_compressed, 0))::float8
       / NULLIF(COUNT(*), 0) AS bytes_per_physical_attempt
FROM network_operations
WHERE created_at > now() - interval '24 hours'
"""

#: Bytes moved per LOGICAL target, 24h. The denominator is
#: `request_attempts`, not `network_operations`: a retry and a shared
#: navigation are extra PHYSICAL work for the same logical target, and
#: the ratio of these two gauges is what coalescing actually buys.
BYTES_PER_LOGICAL_TARGET_SQL = """
SELECT (
    SELECT SUM(COALESCE(bytes_compressed, 0))
    FROM network_operations
    WHERE created_at > now() - interval '24 hours'
)::float8 / NULLIF((
    SELECT COUNT(*)
    FROM request_attempts
    WHERE created_at > now() - interval '24 hours'
), 0) AS bytes_per_logical_target
"""

#: Per-table bloat and index size, straight from the statistics/catalog
#: views. `pg_indexes_size` over `relname` rather than a hand-rolled sum
#: of `pg_class.relpages` so a partitioned parent's own (empty) entry
#: does not read as "this table has no indexes".
TABLE_STORAGE_SQL = """
SELECT relname AS table_name,
       n_dead_tup::float8 / NULLIF(n_live_tup + n_dead_tup, 0)
           AS dead_tuple_fraction,
       pg_indexes_size(relid) AS index_bytes
FROM pg_stat_user_tables
WHERE schemaname = 'public'
"""


@dataclass(frozen=True)
class StorageMetrics:
    """The five EPA C9 storage gauges. ``None`` means UNMEASURED."""

    bytes_per_physical_attempt: float | None = None
    bytes_per_logical_target: float | None = None
    #: ``{table: fraction}`` -- only tables the statistics view knows.
    dead_tuple_fraction: dict[str, float] = field(default_factory=dict)
    #: ``{table: bytes}``.
    index_bytes: dict[str, int] = field(default_factory=dict)
    #: Wall seconds of the last partition-maintenance pass, supplied by
    #: the caller (the maintenance task times itself; this module does not
    #: run the job). ``None`` until a pass has been timed.
    partition_maintenance_seconds: float | None = None


def collect_storage_metrics(
    session: Any,
    *,
    tables: Iterable[str] | None = None,
    partition_maintenance_seconds: float | None = None,
) -> StorageMetrics:
    """Collect the five C9 gauges on an already-open session.

    Degrades per metric: every read is independently guarded, so a
    database missing `network_operations` still reports table bloat, and
    a `pg_stat_user_tables` a restricted role cannot read still leaves
    the two ratios intact. A metric that could not be read is absent, NOT
    zero -- see `render_storage_metrics_prometheus`.

    ``tables`` restricts the per-table labels; ``None`` means every table
    the statistics view returns. Restricting is the normal case for a
    scrape (label cardinality is a cost), and the retention families'
    tables are the set worth watching.
    """
    from sqlalchemy import text  # local: keeps this module import-light

    wanted = set(tables) if tables is not None else None

    def _scalar(sql: str) -> float | None:
        try:
            return _as_float(session.execute(text(sql)).scalar())
        except Exception:
            return None

    dead: dict[str, float] = {}
    index_bytes: dict[str, int] = {}
    try:
        for name, fraction, idx_bytes in session.execute(text(TABLE_STORAGE_SQL)):
            if wanted is not None and name not in wanted:
                continue
            value = _as_float(fraction)
            if value is not None:
                dead[str(name)] = value
            size = _as_int(idx_bytes)
            if size is not None:
                index_bytes[str(name)] = size
    except Exception:
        pass

    return StorageMetrics(
        bytes_per_physical_attempt=_scalar(BYTES_PER_PHYSICAL_ATTEMPT_SQL),
        bytes_per_logical_target=_scalar(BYTES_PER_LOGICAL_TARGET_SQL),
        dead_tuple_fraction=dead,
        index_bytes=index_bytes,
        partition_maintenance_seconds=partition_maintenance_seconds,
    )


def render_storage_metrics_prometheus(metrics: StorageMetrics) -> str:
    """Prometheus text exposition for the five C9 gauges, standalone.

    Same contract as the A5/A7 renderers: a ``None`` value emits NO
    sample rather than a ``0``, so "could not measure" never masquerades
    as "measured zero" -- which for `dead_tuple_fraction` in particular
    is the difference between a healthy table and an unreadable one.
    """
    lines: list[str] = []

    def _family(name: str, samples: list[str]) -> None:
        if not samples:
            return
        kind, help_text = _PROM_HELP[name]
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(samples)

    for name, value in (
        ("crawmatic_bytes_per_physical_attempt", metrics.bytes_per_physical_attempt),
        ("crawmatic_bytes_per_logical_target", metrics.bytes_per_logical_target),
        (
            "crawmatic_partition_maintenance_seconds",
            metrics.partition_maintenance_seconds,
        ),
    ):
        if value is None:
            continue
        _family(name, [f"{name} {_fmt(float(value))}"])

    _family(
        "crawmatic_dead_tuple_fraction",
        [
            f'crawmatic_dead_tuple_fraction{{table="{_esc(table)}"}} '
            f"{_fmt(float(value))}"
            for table, value in sorted(metrics.dead_tuple_fraction.items())
        ],
    )
    _family(
        "crawmatic_index_bytes",
        [
            f'crawmatic_index_bytes{{table="{_esc(table)}"}} {_fmt(float(value))}'
            for table, value in sorted(metrics.index_bytes.items())
        ],
    )

    return ("\n".join(lines) + "\n") if lines else ""


def _fmt(value: float) -> str:
    return repr(float(value))


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_prometheus(snapshot: OpsSnapshot, alerts: list[Alert] | None = None) -> str:
    """Render the snapshot as Prometheus text exposition (v0.0.4).

    **Nothing scrapes this today** — Railway has no Prometheus scraper
    (investigated 2026-08-15) and this repo deliberately adds no
    collector service. It exists because the format is the lingua franca
    of every metrics backend, it costs ~60 lines and zero dependencies,
    and it makes the "we chose not to run a TSDB" decision reversible in
    an afternoon: point any scraper (or a Grafana Alloy sidecar) at
    ``GET /ops/metrics?format=prometheus`` and every rule threshold in
    ``rules.py`` becomes a PromQL alert without re-deriving a single
    number. Emitting it is not a claim that it is being consumed.
    """
    firing = evaluate(snapshot) if alerts is None else alerts
    # Samples are BUFFERED PER METRIC NAME rather than appended in call
    # order. The exposition format requires each metric family to appear
    # as one contiguous block preceded by a single HELP/TYPE pair;
    # emitting them in collection order interleaves families (a
    # `..._fuse_days` sample between two `..._next_month_present`
    # samples) and strict parsers reject that as a duplicated family.
    families: dict[str, list[str]] = {}

    def add(name: str, value: float | None, **labels: str) -> None:
        if value is None:
            return
        label_str = ""
        if labels:
            label_str = "{" + ",".join(
                f'{k}="{_esc(v)}"' for k, v in sorted(labels.items())
            ) + "}"
        families.setdefault(name, []).append(f"{name}{label_str} {_fmt(value)}")

    for p in snapshot.partitions:
        if not p.exists:
            continue
        add(
            "crawmatic_partition_next_month_present",
            1.0 if p.next_month_present else 0.0,
            table=p.table,
        )
        add("crawmatic_partition_fuse_days", p.days_until_next_month, table=p.table)

    if snapshot.rollups.available:
        add("crawmatic_rollup_rows", snapshot.rollups.rows)
        add("crawmatic_rollup_lag_days", snapshot.rollups.lag_days)

    if snapshot.outbox.available:
        add("crawmatic_outbox_pending", snapshot.outbox.pending)
        add("crawmatic_outbox_dead", snapshot.outbox.dead)
        add(
            "crawmatic_outbox_oldest_pending_seconds",
            snapshot.outbox.oldest_pending_age_seconds,
        )

    if snapshot.breaker.available and snapshot.breaker.state is not None:
        add("crawmatic_breaker_open", 1.0 if snapshot.breaker.state == "OPEN" else 0.0)
        add(
            "crawmatic_breaker_seconds_since_evaluation",
            snapshot.breaker.seconds_since_evaluation,
        )

    if snapshot.queue.available:
        add(
            "crawmatic_targets_in_flight",
            snapshot.queue.targets_pending + snapshot.queue.targets_started,
        )
        for status, age in (
            ("PENDING", snapshot.queue.oldest_pending_target_age_seconds),
            ("STARTED", snapshot.queue.oldest_started_target_age_seconds),
            ("DEFERRED", snapshot.queue.oldest_deferred_target_age_seconds),
        ):
            add("crawmatic_target_oldest_age_seconds", age, status=status)

    for d in snapshot.domains_24h:
        add("crawmatic_domain_attempts_24h", d.attempts, domain=d.domain)
        add("crawmatic_domain_success_rate_24h", d.success_rate, domain=d.domain)
        add("crawmatic_domain_requests_per_url_24h", d.requests_per_url, domain=d.domain)
        add("crawmatic_domain_proxied_per_url_24h", d.proxied_per_url, domain=d.domain)
        add("crawmatic_domain_proxy_usd_24h", d.estimated_usd, domain=d.domain)

    for d in snapshot.discovery:
        add("crawmatic_discovery_runs_24h", d.runs_24h, domain=d.domain)
        add("crawmatic_discovery_surge_factor", d.surge_factor, domain=d.domain)

    if snapshot.spend.available:
        add(
            "crawmatic_proxied_requests_month_to_date",
            snapshot.spend.proxied_month_to_date,
        )
        add("crawmatic_proxy_usd_month_to_date", snapshot.spend.usd_month_to_date)
        from app_shared.opsmetrics import cost as _cost

        add(
            "crawmatic_proxy_forecast_month_end_usd",
            _cost.usd(snapshot.spend.forecast_month_end_24h),
        )

    if snapshot.freshness.available:
        add(
            "crawmatic_seconds_since_last_attempt",
            snapshot.freshness.seconds_since_attempt,
        )
        add("crawmatic_refresh_rules_enabled", snapshot.freshness.refresh_rules_enabled)

    add("crawmatic_redis_evicted_keys", snapshot.redis.evicted_keys)
    add("crawmatic_redis_used_memory_bytes", snapshot.redis.used_memory)

    if snapshot.db_role.rls_effective is not None:
        add("crawmatic_rls_effective", 1.0 if snapshot.db_role.rls_effective else 0.0)

    counts = {s: 0 for s in Severity}
    for a in firing:
        counts[a.severity] += 1
    for severity, count in counts.items():
        add("crawmatic_alerts_firing", count, severity=str(severity))

    lines: list[str] = []
    for name, samples in families.items():
        kind, help_text = _PROM_HELP.get(name, ("gauge", name))
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(samples)
    return "\n".join(lines) + "\n"


__all__ = [
    "ALERT_EVENT",
    "ALL_CLEAR_EVENT",
    "SNAPSHOT_EVENT",
    "ATTEMPTS_PER_VALID_FRESH_24H_SQL",
    "ATTEMPT_BUDGET_EXHAUSTED_24H_SQL",
    "ATTEMPTS_PER_VALID_FRESH_MAX",
    "BaselineMetrics",
    "COST_MODEL_DRIFT_RATIO_MAX",
    "COST_MODEL_DRIFT_RATIO_MIN",
    "COST_MODEL_DRIFT_RATIO_SQL",
    "DISPATCH_AMBIGUOUS_INTENTS_MAX",
    "DISPATCH_AMBIGUOUS_INTENTS_SQL",
    "FRESH_UNIQUE_MATCHES_24H_SQL",
    "PERSISTENCE_FAILURES_1H_MAX",
    "PERSISTENCE_FAILURES_1H_SQL",
    "QUEUE_OLDEST_PENDING_SECONDS_SQL",
    "TARGET_PHASES",
    "TARGET_PHASE_P95_SECONDS_SQL",
    "collect_baseline_metrics",
    "render_baseline_metrics_prometheus",
    "LEDGER_LINKED_ATTEMPT_FRACTION_MIN",
    "LEDGER_BYTES_MISSING_FRACTION_MAX",
    "LedgerCoverage",
    "emit_alerts",
    "emit_snapshot",
    "ledger_linked_attempt_fraction",
    "ledger_bytes_missing_fraction",
    "BYTES_PER_LOGICAL_TARGET_SQL",
    "BYTES_PER_PHYSICAL_ATTEMPT_SQL",
    "StorageMetrics",
    "TABLE_STORAGE_SQL",
    "collect_storage_metrics",
    "render_storage_metrics_prometheus",
    "render_costauth_denials_prometheus",
    "render_ledger_coverage_prometheus",
    "render_prometheus",
]
