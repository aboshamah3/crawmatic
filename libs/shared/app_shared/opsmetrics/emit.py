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
from typing import Any, Iterable

from app_shared.opsmetrics.rules import Alert, Severity, evaluate, worst_severity
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
}


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
    "emit_alerts",
    "emit_snapshot",
    "render_prometheus",
]
