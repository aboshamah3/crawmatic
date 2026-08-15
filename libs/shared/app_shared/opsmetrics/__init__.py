"""Production observability and cost stop-loss signals (audit H5).

Three layers, deliberately separated the same way
``app_shared.access.breaker`` separates them:

1. :mod:`~app_shared.opsmetrics.snapshot` — **measurement**. One
   read-only SQL pass over tables that already exist, producing a plain
   :class:`~app_shared.opsmetrics.snapshot.OpsSnapshot`. Never raises;
   each section degrades independently.
2. :mod:`~app_shared.opsmetrics.rules` — **judgement**. Pure functions
   from snapshot + thresholds to alerts. Stdlib only, fully unit-testable.
3. :mod:`~app_shared.opsmetrics.emit` — **delivery**. Structured
   single-line JSON logs per `contracts/observability.md`, plus a
   Prometheus text rendering for a future scraper.

:mod:`~app_shared.opsmetrics.cost` holds the measured USD constants with
their provenance.

The HTTP surface is ``GET /ops/metrics`` in ``apps/api``; the operator
documents are ``docs/ops/OBSERVABILITY_SLO_AND_ALERTS.md`` and
``docs/ops/RUNBOOK_STOP_DISPATCH_AND_SPEND.md``.
"""

from __future__ import annotations

from app_shared.opsmetrics.cost import (
    USD_FIXED_MONTHLY,
    USD_PER_FULL_REFRESH,
    USD_PER_PROXIED_REQUEST,
    usd,
)
from app_shared.opsmetrics.emit import (
    ALERT_EVENT,
    ALL_CLEAR_EVENT,
    SNAPSHOT_EVENT,
    emit_alerts,
    emit_snapshot,
    render_prometheus,
)
from app_shared.opsmetrics.rules import (
    DEFAULT_THRESHOLDS,
    RULES,
    Alert,
    Category,
    Severity,
    Thresholds,
    evaluate,
    worst_severity,
)
from app_shared.opsmetrics.snapshot import OpsSnapshot, collect_snapshot

__all__ = [
    "ALERT_EVENT",
    "ALL_CLEAR_EVENT",
    "DEFAULT_THRESHOLDS",
    "RULES",
    "SNAPSHOT_EVENT",
    "USD_FIXED_MONTHLY",
    "USD_PER_FULL_REFRESH",
    "USD_PER_PROXIED_REQUEST",
    "Alert",
    "Category",
    "OpsSnapshot",
    "Severity",
    "Thresholds",
    "collect_snapshot",
    "emit_alerts",
    "emit_snapshot",
    "evaluate",
    "render_prometheus",
    "usd",
    "worst_severity",
]
