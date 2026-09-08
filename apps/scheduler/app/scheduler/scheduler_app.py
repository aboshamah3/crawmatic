"""Scheduler process entry point.

Boots to a running loop via ``python -m app.scheduler.scheduler_app``
(contracts/service-topology.md). SPEC-01 shipped this as a no-op skeleton
("later specs add periodic scan scheduling here"); SPEC-12 US4
(contracts/rediscovery.md "Periodic light re-check", FR-021) is the first
to need one, so this loop now also enqueues ``STRATEGY_LIGHT_RECHECK`` on
the ``maintenance`` queue every ``STRATEGY_STATS_FLUSH_INTERVAL_SECONDS``
(the one SPEC-12 cadence knob, data-model §7 — reused rather than adding
an eleventh ``Settings`` knob just for this interval). US5 (T036,
contracts/stats-buffer.md §Flush, FR-023) adds ``STRATEGY_STATS_FLUSH``
on the SAME tick/queue/interval — its own named cadence knob, so no
Redis buffer sits un-flushed for longer than one interval even absent a
just-finalized job.

SPEC-13 US2 (`contracts/scheduler-loop.md`) adds a **second, independent**
interval accumulator driven by ``SCHEDULER_POLL_INTERVAL_SECONDS`` that
calls ``app.scheduler.refresh.run_refresh_pass`` on the BYPASSRLS system
session (`app_shared.database.get_system_sessionmaker`) to claim and
fire due ``refresh_rules``. This is a genuine DB-driven enqueuer (as
opposed to the fixed-cadence SPEC-12 maintenance enqueues above); a
further ``celery beat``-style scheduler remains a later spec's concern.
The loop still exits cleanly on SIGTERM/SIGINT so the container can be
stopped by the orchestrator without a crash-loop, and a raised
refresh-pass exception is logged and swallowed — it must never
crash-loop the process (same best-effort posture as the maintenance
enqueues).

SPEC-15 US1 (contracts/partition-creation.md) adds a **third,
independent** interval accumulator driven by
``PARTITION_CREATE_INTERVAL_SECONDS`` that fire-and-forget enqueues
``MAINTENANCE_PARTITION_CREATE`` on the ``maintenance`` queue — mirroring
the SPEC-12 fixed-cadence enqueues above (not a DB-driven claim like the
SPEC-13 refresh pass). Daily by default: weeks of lead so next-month's
partitions always exist before the month begins (SC-001).

SPEC-15 US2 (contracts/daily-rollup.md) adds a **fourth, independent**
interval accumulator driven by ``DAILY_ROLLUP_INTERVAL_SECONDS`` that
fire-and-forget enqueues ``MAINTENANCE_DAILY_ROLLUP`` on the
``maintenance`` queue — same fixed-cadence shape as the partition-create
accumulator above (no arguments; the task itself defaults to yesterday
UTC).

SPEC-15 US3 (contracts/retention-drop.md) adds a **fifth, independent**
interval accumulator driven by ``RETENTION_INTERVAL_SECONDS`` that
fire-and-forget enqueues ``MAINTENANCE_RETENTION_DROP`` on the
``maintenance`` queue — same fixed-cadence shape as the
partition-create/daily-rollup accumulators above (no arguments; the
task re-checks partition-drop eligibility + rollup coverage on every
pass, so a not-yet-verified partition is simply retained until a later
run, self-healing).

2026-08-15 readiness cycle — **the three daily cadences above are no
longer driven by in-process accumulators.** A float that starts at
``0.0`` on every process start, compared against ``86400``, only ever
fires on a container that survives a full day; Railway restarts
containers on every deploy, so those three tasks could go — and did go —
arbitrarily long without firing. The production symptom was no
``2026_09`` partition on any of the four partitioned tables (a total
write outage on a known date) and zero rows ever written to
``variant_price_daily_rollups``. Their deadlines now live in the
``maintenance_cadences`` table and are claimed atomically
(``app_shared.maintenance.cadence``), so a restart cannot reset the
countdown, an overdue cadence fires on the next poll, and multiple
scheduler replicas (or a crash-loop) still enqueue exactly once.

The 60s-class cadences (refresh poll, strategy flush/finalize/recover/
redispatch, outbox drain/reconcile) deliberately stay in-process: a
restart costs at most one interval of *seconds*, every one of them is an
idempotent sweep whose work is durably recorded elsewhere, and making
them durable would add a write per tick to protect against a bounded
loss. See ``app_shared.maintenance.cadence``'s module docstring.

A separate, slower **health tick** runs the outcome assertions in
``app_shared.maintenance.health`` — "does the partition the calendar
needs in N months exist?" and "has the daily rollup produced a row
recently?" — at ERROR level. These assert on *results read from the
database*, not on the scheduler's own bookkeeping, because the second,
independent root cause of the same incident was the ``worker`` service
missing ``SYSTEM_DATABASE_URL``/``AUTH_DATABASE_URL``: the scheduler was
enqueueing correctly the whole time and every task died on arrival.

A separate **ops snapshot tick** (``OPS_SNAPSHOT_INTERVAL_SECONDS``,
15 min) collects ``app_shared.opsmetrics.snapshot.collect_snapshot`` and
hands it to ``...opsmetrics.emit.emit_snapshot``, which logs the
snapshot plus one line per firing alert rule. Audit §H5 shipped the
collector, the rules and the emitter with exactly one caller — the
on-demand ``GET /ops/metrics`` endpoint — so until this tick existed
every rule in that file could only fire while an operator was already
looking at the dashboard. Read-only, on the system session, errors
swallowed: same posture as the health tick.

``main()`` opens with
`app_shared.config_validation.assert_production_safe` (audit §L1), the
scheduler's equivalent of the API's import-time call in
``apps/api/app/main.py``: a production deploy still carrying
``.env.example``-shaped secrets, ``PGBOUNCER_AUTH_TYPE=trust`` or the DB
owner role as ``DATABASE_URL`` refuses to boot rather than starting to
drive fleet-wide maintenance under it. A no-op outside production, so
local runs and CI are untouched.
"""

from __future__ import annotations

import logging
import signal
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from types import FrameType

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_shared.config import Settings, get_settings
from app_shared.config_validation import assert_production_safe
from app_shared.costauth.service import CostAuthorizationService
from app_shared.database import get_system_sessionmaker
from app_shared.heartbeat import HeartbeatEmitter, PeriodicHeartbeat, default_instance_id
from app_shared.enums import (
    ScrapeJobSource,
    ScrapeJobType,
    ScrapeScope,
    WebhookEventType,
)
from app_shared.ids import new_uuid7
from app_shared.jobs.service import create_scope_job
from app_shared.maintenance.cadence import (
    claim_cadence,
    ensure_cadence_rows,
    log_overdue_cadences,
    read_cadence_statuses,
)
from app_shared.maintenance.health import check_maintenance_health, log_health_report
from app_shared.messaging import enqueue
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.cost_authorization import CostReservation, ReservationState
from app_shared.models.maintenance_cadence import (
    CADENCE_BREAKER_EVALUATE,
    CADENCE_COST_ROLLUP,
    CADENCE_DAILY_ROLLUP,
    CADENCE_DISPATCH_RECONCILE,
    CADENCE_ENTITLEMENT_REFRESH,
    CADENCE_FLEET_BUDGET_ROLLFORWARD,
    CADENCE_PARTITION_CREATE,
    CADENCE_RECONCILE_PROVIDER_USAGE,
    CADENCE_EVIDENCE_RETENTION,
    CADENCE_LEDGER_SUMMARIZE_CHILDREN,
    CADENCE_RETENTION_DROP,
)
from app_shared.models.refresh_rule_occurrences import refresh_rule_occurrences
from app_shared.models.refresh_rules import RefreshRule
from app_shared.opsmetrics import collect_snapshot, emit_snapshot
from app_shared.outbox.writer import write_outbox_message
from app_shared.scheduling.cadence import compute_next_run_at
from app_shared.scheduling.fair_queue import (
    DEFAULT_DOMAIN_CONCURRENCY,
    DEFAULT_FLEET_CONCURRENCY,
    DEFAULT_MAX_ATTEMPTS,
    DeadLetterRecord,
    FairShare,
    FleetLimits,
    LiveUsage,
    PassOutcome,
    RetryLedger,
    ScheduleCandidate,
    default_denial_reason,
    run_pass,
)
from app_shared.task_names import (
    MAINTENANCE_BREAKER_EVALUATE,
    COSTAUTH_RESERVATION_SWEEP,
    CREATE_WEBHOOK_EVENT,
    DISPATCH_RECONCILE_INTENTS,
    MAINTENANCE_COST_ROLLUP,
    MAINTENANCE_DAILY_ROLLUP,
    MAINTENANCE_ENTITLEMENT_REFRESH,
    MAINTENANCE_FLEET_BUDGET_ROLLFORWARD,
    MAINTENANCE_PARTITION_CREATE,
    MAINTENANCE_RECONCILE_PROVIDER_USAGE,
    MAINTENANCE_EVIDENCE_RETENTION,
    MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN,
    MAINTENANCE_RETENTION_DROP,
    OUTBOX_DRAIN,
    OUTBOX_RECONCILE,
    SCRAPE_FINALIZE_JOBS,
    SCRAPE_REAP_STALE_TARGETS,
    SCRAPE_RECOVER_STALLED,
    SCRAPE_REDISPATCH_JOBS,
    STRATEGY_LIGHT_RECHECK,
    STRATEGY_STATS_FLUSH,
)

from app.scheduler.refresh import (
    clear_rule_failures,
    record_rule_failure,
    run_refresh_pass,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler")

_shutdown_requested = False

#: Coarse tick granularity for the shutdown-check/enqueue loop -- fine
#: enough to react to SIGTERM/SIGINT promptly without busy-looping.
_TICK_SECONDS = 1.0


def _handle_shutdown(signum: int, frame: FrameType | None) -> None:
    global _shutdown_requested
    logger.info("scheduler received signal %s, shutting down", signum)
    _shutdown_requested = True


def _enqueue_light_recheck() -> None:
    """Fire-and-forget `STRATEGY_LIGHT_RECHECK` on the `maintenance` queue.

    Errors (e.g. the broker being unreachable) are logged and swallowed --
    a missed tick just means degradation on patrol is caught one interval
    later; it must never crash the scheduler process (the same resilience
    posture as `scrape_core.pipelines`'s own best-effort enqueue sites).
    """
    try:
        enqueue(STRATEGY_LIGHT_RECHECK, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", STRATEGY_LIGHT_RECHECK)


def _enqueue_stats_flush() -> None:
    """Fire-and-forget periodic `STRATEGY_STATS_FLUSH` on the `maintenance`
    queue (US5, T036, contracts/stats-buffer.md §Flush, FR-023) -- the
    no-argument call shape, which sweeps every workspace's `stratdirty`
    set. Errors are logged and swallowed for the same reason
    `_enqueue_light_recheck` swallows them: a missed tick just means
    buffered stats sit one interval longer before flushing, never a
    crashed scheduler process.
    """
    try:
        enqueue(STRATEGY_STATS_FLUSH, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", STRATEGY_STATS_FLUSH)


def _enqueue_reap_stale_targets() -> None:
    """Fire-and-forget periodic `SCRAPE_REAP_STALE_TARGETS` on the
    `maintenance` queue (EPA A3/B2, 2026-09-03) — reverts targets
    orphaned `STARTED` by a vanished scrapyd container and fails every
    non-terminal target of a job past its hard runtime ceiling.

    Enqueued BEFORE `_enqueue_finalize_jobs` on purpose. The reaper's
    whole output is targets reaching a terminal status, and
    `finalize_jobs` closes a job only once ALL of its targets are
    terminal — so running the reaper first lets a job un-wedged by this
    tick finalize on the same tick instead of waiting a full minute for
    the next one. (The ordering is an optimisation, not a correctness
    requirement: both tasks are idempotent no-arg sweeps and the queue
    gives no delivery-order guarantee anyway.)

    Errors are logged and swallowed like every other maintenance
    enqueue: a missed tick just leaves a wedged job wedged one more
    minute, never a crashed scheduler.
    """
    try:
        enqueue(SCRAPE_REAP_STALE_TARGETS, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", SCRAPE_REAP_STALE_TARGETS)


def _enqueue_finalize_jobs() -> None:
    """Fire-and-forget periodic `SCRAPE_FINALIZE_JOBS` on the `maintenance`
    queue (ISSUES_FULL_RUN_2026-07-17 Issue "no job finalization in the
    scheduler"). The pipeline's own per-batch enqueue only fires when a
    result batch actually flushes -- a job whose last targets end via
    never-dispatched skips (or a crashed spider) otherwise dangles RUNNING
    forever. `finalize_jobs()` is a no-arg, idempotent sweep (terminal
    jobs are skipped outright), so re-enqueueing it every tick is safe.
    Errors are logged and swallowed like every other maintenance enqueue.
    """
    try:
        enqueue(SCRAPE_FINALIZE_JOBS, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", SCRAPE_FINALIZE_JOBS)


def _enqueue_recover_stalled() -> None:
    """Fire-and-forget periodic `SCRAPE_RECOVER_STALLED` on the
    `maintenance` queue (ISSUES_FULL_RUN_2026-07-17 Issue 8: the task
    existed but nothing ever scheduled it). Re-dispatches batches whose
    targets sat PENDING past `SCRAPE_STALL_TIMEOUT_SECONDS`; idempotent,
    no-arg. Errors are logged and swallowed like every other maintenance
    enqueue.
    """
    try:
        enqueue(SCRAPE_RECOVER_STALLED, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", SCRAPE_RECOVER_STALLED)


def _enqueue_redispatch_pending() -> None:
    """Fire-and-forget periodic `SCRAPE_REDISPATCH_JOBS` on the
    `maintenance` queue (PLAN_AMAZON_NOON_PRICING Phase 1: the DEFERRED
    deadlock). `dispatch_job` selects PENDING **and** DEFERRED targets,
    but nothing ever re-enqueued it once the initial delivery ran — a
    DEFERRED target (requeue-cap overflow) therefore sat forever and any
    run past ~20 products wedged. The sweep task re-enqueues
    `SCRAPE_DISPATCH_JOB` for every job still holding such targets;
    idempotent (the dispatch client's TTL-bounded Redis guard paces
    actual re-POSTs). Errors are logged and swallowed like every other
    maintenance enqueue.
    """
    try:
        enqueue(SCRAPE_REDISPATCH_JOBS, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", SCRAPE_REDISPATCH_JOBS)


def _enqueue_dispatch_reconcile() -> None:
    """Fire-and-forget `DISPATCH_RECONCILE_INTENTS` on the `maintenance`
    queue (EPA B3, 2026-09-07, closing B2's owed wiring) — step 5 of the
    commit-before-send dispatch protocol: settle every `POSTED`
    `dispatch_intents` row against the node it names.

    On the DURABLE cadence, not one of the in-process accumulators above,
    and that distinction is the whole point: the rows this settles exist
    precisely because a worker died between its POST and the node's
    answer. A countdown that resets on process start would reset on the
    very class of event it is there to recover from. Errors are logged
    and swallowed like every other maintenance enqueue — the deadline is
    still in Postgres, unclaimed, for the next tick.
    """
    try:
        enqueue(DISPATCH_RECONCILE_INTENTS, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", DISPATCH_RECONCILE_INTENTS)


def _enqueue_partition_create() -> None:
    """Fire-and-forget `MAINTENANCE_PARTITION_CREATE` on the `maintenance`
    queue (SPEC-15 US1, contracts/partition-creation.md) -- ensures
    current + next month's partitions exist for every existing registered
    table. Errors are logged and swallowed for the same reason
    `_enqueue_light_recheck`/`_enqueue_stats_flush` swallow them: a
    missed tick just means one fewer day of lead before the next poll
    interval retries, never a crashed scheduler process.
    """
    try:
        enqueue(MAINTENANCE_PARTITION_CREATE, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", MAINTENANCE_PARTITION_CREATE)


def _enqueue_daily_rollup() -> None:
    """Fire-and-forget `MAINTENANCE_DAILY_ROLLUP` on the `maintenance`
    queue (SPEC-15 US2, contracts/daily-rollup.md) -- upserts one
    `variant_price_daily_rollups` row per (workspace, variant) with
    activity on the default target day (yesterday UTC). Errors are
    logged and swallowed for the same reason
    `_enqueue_light_recheck`/`_enqueue_stats_flush`/
    `_enqueue_partition_create` swallow them: a missed tick just means
    that day's rollup is retried on the next interval, never a crashed
    scheduler process.
    """
    try:
        enqueue(MAINTENANCE_DAILY_ROLLUP, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", MAINTENANCE_DAILY_ROLLUP)


def _enqueue_reconcile_provider_usage() -> None:
    """Fire-and-forget `MAINTENANCE_RECONCILE_PROVIDER_USAGE` on the
    `maintenance` queue (EPA C5, READY-005 part 2) -- the owed wiring
    this run closes. C5 registered the task
    (`apps/workers/app/workers/tasks_maintenance.py::
    reconcile_provider_usage`) and its Celery name but could not wire a
    schedule entry (`apps/scheduler` was fenced then; it is free now).
    No-arg call reconciles the PRIOR UTC day for every provider with
    imported evidence -- see that task's own docstring. Errors are
    logged and swallowed like every other maintenance enqueue: a missed
    tick just means that day's reconciliation is retried on the next
    interval, never a crashed scheduler process.
    """
    try:
        enqueue(MAINTENANCE_RECONCILE_PROVIDER_USAGE, queue="maintenance")
    except Exception:
        logger.exception(
            "scheduler: failed to enqueue %s", MAINTENANCE_RECONCILE_PROVIDER_USAGE
        )


def _enqueue_cost_rollup() -> None:
    """Fire-and-forget `MAINTENANCE_COST_ROLLUP` on the `maintenance`
    queue (EPA C6) -- maintains the bounded, durable cost-rollup tables
    `GET /ops/metrics` and `GET /v1/cost-rollups` both read. No-arg call
    runs the durable-watermark catch-up path -- see that task's own
    docstring. Errors are logged and swallowed like every other
    maintenance enqueue: a missed tick just means the rollup is retried
    on the next interval, never a crashed scheduler process.
    """
    try:
        enqueue(MAINTENANCE_COST_ROLLUP, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", MAINTENANCE_COST_ROLLUP)


def _enqueue_retention_drop() -> None:
    """Fire-and-forget `MAINTENANCE_RETENTION_DROP` on the `maintenance`
    queue (SPEC-15 US3, contracts/retention-drop.md) -- drops whole
    expired partitions (verify-before-drop for `price_observations`) and
    ages the non-partitioned rollup table. Errors are logged and
    swallowed for the same reason
    `_enqueue_light_recheck`/`_enqueue_stats_flush`/
    `_enqueue_partition_create`/`_enqueue_daily_rollup` swallow them: a
    missed tick just means retention is retried on the next interval,
    never a crashed scheduler process.
    """
    try:
        enqueue(MAINTENANCE_RETENTION_DROP, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", MAINTENANCE_RETENTION_DROP)


def _enqueue_evidence_retention() -> None:
    """Fire-and-forget `MAINTENANCE_EVIDENCE_RETENTION` on the
    `maintenance` queue (EPA C5, F19) -- the age-AND-reference gated
    sweep of the raw-evidence blob store, closing the deletion gap
    `docs/RETENTION_POLICY.md` §2.1 records.

    Errors logged and swallowed, same as every sibling here: a missed
    tick means blobs are swept on the next interval, and evidence
    retained one day too long is the safe direction of that failure.
    """
    try:
        enqueue(MAINTENANCE_EVIDENCE_RETENTION, queue="maintenance")
    except Exception:
        logger.exception(
            "scheduler: failed to enqueue %s", MAINTENANCE_EVIDENCE_RETENTION
        )


def _enqueue_ledger_summarize_children() -> None:
    """Fire-and-forget `MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN` on the
    `maintenance` queue (EPA C9, F14) -- compress a settled browser
    navigation's subresource children into one
    `network_operation_resource_summaries` row and delete them.

    Errors logged and swallowed, same as every sibling here. The failure
    direction is safe by construction: a missed tick means the children
    survive one more day, and the task itself does nothing at all until
    the owner names `network_operation_children` in
    `RETENTION_ENABLED_CLASSES`.
    """
    try:
        enqueue(MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN, queue="maintenance")
    except Exception:
        logger.exception(
            "scheduler: failed to enqueue %s", MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN
        )


def _enqueue_outbox_drain() -> None:
    """Fire-and-forget `OUTBOX_DRAIN` on the `maintenance` queue (audit
    risk H1). This is the pass that turns durably-recorded
    `outbox_messages` rows into real Celery deliveries, so it runs on its
    own, much tighter accumulator (`OUTBOX_DRAIN_INTERVAL_SECONDS`) than
    the 60s maintenance tick — that interval is the worst-case added
    latency between a producer's COMMIT and its follow-up task reaching
    the broker.

    Errors are logged and swallowed like every other maintenance enqueue,
    and here that is *safe by construction* rather than merely tolerable:
    a missed tick cannot lose anything, because the messages are already
    committed in Postgres. The next tick (or the next scheduler process)
    publishes them.
    """
    try:
        enqueue(OUTBOX_DRAIN, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", OUTBOX_DRAIN)


def _enqueue_outbox_reconcile() -> None:
    """Fire-and-forget `OUTBOX_RECONCILE` on the `maintenance` queue
    (audit risk H1) — backlog/dead-letter reporting plus retention on the
    outbox table. Slower cadence than the drain
    (`OUTBOX_RECONCILE_INTERVAL_SECONDS`); idempotent and no-arg. Errors
    are logged and swallowed like every other maintenance enqueue.
    """
    try:
        enqueue(OUTBOX_RECONCILE, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", OUTBOX_RECONCILE)


def _enqueue_entitlement_refresh() -> None:
    """Fire-and-forget `MAINTENANCE_ENTITLEMENT_REFRESH` on the
    `maintenance` queue (EPA go-live prep, 2026-08-26) — re-stamps
    `observed_at` on the SEEDED `workspace_entitlements` rows so the C3
    gate's placeholder evidence never ages into a stale-deny.

    The only DURABLE cadence here that is not daily
    (`ENTITLEMENT_REFRESH_INTERVAL_SECONDS`, 6h), and durable rather than
    a 60s-class in-process accumulator for exactly the reason the
    2026-08-15 readiness cycle established: an interval measured in hours
    against a process that restarts on every deploy is a countdown that
    may never complete. Here that failure mode has teeth — the deadline
    it races is `DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS`, past
    which EVERY workspace is denied all paid work.

    Errors are logged and swallowed like every other maintenance enqueue.
    A missed tick loses nothing on its own: the deadline is in
    `maintenance_cadences`, the evidence still has hours of margin, and
    the next tick re-stamps it.
    """
    try:
        enqueue(MAINTENANCE_ENTITLEMENT_REFRESH, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", MAINTENANCE_ENTITLEMENT_REFRESH)


def _enqueue_breaker_evaluate() -> None:
    """Fire-and-forget `MAINTENANCE_BREAKER_EVALUATE` on the `maintenance`
    queue (EPA B1, 2026-09-03) — re-evaluates the durable proxy circuit
    breaker so its `evaluated_at` evidence never ages past
    `DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS`.

    This cadence exists to break a DEADLOCK, not merely to keep a number
    warm. The cost gate denies all paid work on stale breaker evidence,
    and the only other evaluator runs inside the scraping path that the
    denial blocks — so once the row went stale, nothing in the system
    could ever refresh it again. The scheduler can: it needs no
    permission from the gate it is unblocking.

    The fastest durable cadence here by an order of magnitude
    (`PROXY_BREAKER_EVAL_INTERVAL_SECONDS`, 5m against a 3600s deadline),
    which is affordable precisely because the expensive part is behind
    the evaluator's own lease: an enqueue that arrives while the lease is
    fresh does no aggregate work at all.

    Errors are logged and swallowed like every other maintenance enqueue.
    A missed tick loses nothing: the deadline lives in
    `maintenance_cadences`, the evidence still has many ticks of margin,
    and the next tick re-evaluates.
    """
    try:
        enqueue(MAINTENANCE_BREAKER_EVALUATE, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", MAINTENANCE_BREAKER_EVALUATE)


def _enqueue_fleet_budget_rollforward() -> None:
    """Fire-and-forget `MAINTENANCE_FLEET_BUDGET_ROLLFORWARD` on the
    `maintenance` queue (EPA A4/B3, 2026-09-03) — re-caps
    `fleet_cost_budgets` for the current and next month so the fleet-wide
    money ceiling survives a month boundary without an operator.

    The gap it closes is DATED, not intermittent: the one-off seeder
    capped a fixed number of months ending `2026_10`, and a budget row is
    born with `NULL` limits, so the first paid dispatch of the following
    month creates an uncapped row and the ceiling disappears silently.

    Reuses `ENTITLEMENT_REFRESH_INTERVAL_SECONDS` (6h) rather than
    introducing a knob of its own. The only deadline this cadence races
    is a MONTH boundary, so six hours is already three orders of
    magnitude of margin, and reusing an existing 6-hourly value keeps the
    scheduler's tuning surface from growing a knob nobody would ever have
    a reason to turn. It is affordable because the work is bounded and
    idempotent: on all but the first tick of a month it is one SELECT
    that finds every row already capped.

    Errors are logged and swallowed like every other maintenance enqueue.
    A missed tick loses nothing — the deadline lives in
    `maintenance_cadences` and the next tick re-caps.
    """
    try:
        enqueue(MAINTENANCE_FLEET_BUDGET_ROLLFORWARD, queue="maintenance")
    except Exception:
        logger.exception(
            "scheduler: failed to enqueue %s", MAINTENANCE_FLEET_BUDGET_ROLLFORWARD
        )


def _enqueue_costauth_reservation_sweep() -> None:
    """Fire-and-forget `COSTAUTH_RESERVATION_SWEEP` on the `maintenance`
    queue (EPA C3, READY-006) — reap expired cost-authorization leases
    whose grant has no open operation in C1's ledger.

    Reuses the existing 60s-class maintenance tick/knob rather than
    introducing a cadence of its own, the same SPEC-12 precedent
    `finalize_jobs`/`recover_stalled_batches`/`redispatch_pending_jobs`
    already follow. That is the right cadence here too: reservation
    leases are minutes long, so a daily durable cadence would be far too
    slow, and the sweep is a cheap indexed scan
    (`ix_cost_reservations_state_lease_expires_at`) that no-ops when
    nothing has expired.

    Errors are logged and swallowed like every other maintenance enqueue.
    A missed tick loses nothing: the expired leases are still in
    Postgres, and the next tick reaps them.
    """
    try:
        enqueue(COSTAUTH_RESERVATION_SWEEP, queue="maintenance")
    except Exception:
        logger.exception("scheduler: failed to enqueue %s", COSTAUTH_RESERVATION_SWEEP)


def _run_refresh_pass_tick(batch_limit: int) -> None:
    """Run one SPEC-13 refresh pass (`app.scheduler.refresh.run_refresh_pass`)
    on the BYPASSRLS system sessionmaker, claiming/firing up to
    ``batch_limit`` due `refresh_rules`. Any exception raised by the pass
    (a DB hiccup, a misconfigured `SYSTEM_DATABASE_URL`, ...) is logged
    and swallowed -- exactly like `_enqueue_light_recheck`/
    `_enqueue_stats_flush`, a missed/failed tick is retried on the next
    poll interval and must never crash-loop the scheduler process
    (contracts/scheduler-loop.md).
    """
    try:
        fired = run_refresh_pass(
            get_system_sessionmaker(),
            now=datetime.now(timezone.utc),
            batch_limit=batch_limit,
        )
        if fired:
            logger.info("scheduler: refresh pass fired %d rule(s)", fired)
    except Exception:
        logger.exception("scheduler: refresh pass failed")


#: The durable daily cadences: ``(cadence_key, settings attribute holding
#: the interval, enqueue callable)``. Driven by ``maintenance_cadences``
#: deadlines, NOT by in-process accumulators — see the module docstring.
#:
#: The two EPA C5/C6 entries added 2026-08-26
#: (``CADENCE_RECONCILE_PROVIDER_USAGE``/``CADENCE_COST_ROLLUP``)
#: deliberately reuse ``DAILY_ROLLUP_INTERVAL_SECONDS`` rather than a
#: `Settings` field of their own: `libs/shared/app_shared/config.py` is
#: held by a concurrent worker for this run's duration (this task's HARD
#: FENCES), and C5's own task docstring already describes this task as
#: running "on the existing daily-cadence maintenance tick" — both are
#: daily-shaped cadences, and each still gets its OWN
#: ``maintenance_cadences`` row (own claim, own `last_run_at`, own
#: observability), only the interval VALUE is shared.
# TODO(config): promote RECONCILE_PROVIDER_USAGE_INTERVAL_SECONDS and
# COST_ROLLUP_INTERVAL_SECONDS to real `Settings` fields once config.py
# is free, so each cadence has its own independently-tunable interval
# instead of borrowing the daily-rollup one.
_DURABLE_CADENCES = (
    (CADENCE_PARTITION_CREATE, "PARTITION_CREATE_INTERVAL_SECONDS", _enqueue_partition_create),
    (CADENCE_DAILY_ROLLUP, "DAILY_ROLLUP_INTERVAL_SECONDS", _enqueue_daily_rollup),
    (CADENCE_RETENTION_DROP, "RETENTION_INTERVAL_SECONDS", _enqueue_retention_drop),
    (
        CADENCE_RECONCILE_PROVIDER_USAGE,
        "DAILY_ROLLUP_INTERVAL_SECONDS",
        _enqueue_reconcile_provider_usage,
    ),
    (CADENCE_COST_ROLLUP, "DAILY_ROLLUP_INTERVAL_SECONDS", _enqueue_cost_rollup),
    # EPA go-live prep 2026-08-26. Unlike the five above this one has its
    # OWN `Settings` field (`ENTITLEMENT_REFRESH_INTERVAL_SECONDS`, 6h)
    # rather than borrowing the daily one — it must stay far under
    # `DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS` (86400), so sharing
    # a knob whose whole purpose is to be daily would couple this
    # cadence's correctness to an unrelated tuning decision.
    (
        CADENCE_ENTITLEMENT_REFRESH,
        "ENTITLEMENT_REFRESH_INTERVAL_SECONDS",
        _enqueue_entitlement_refresh,
    ),
    # EPA B1 2026-09-03. Reuses the evaluator's OWN knob
    # (`PROXY_BREAKER_EVAL_INTERVAL_SECONDS`, 5m) rather than a new one,
    # so the cadence that enqueues an evaluation and the lease that
    # decides whether one is due can never disagree. Far faster than
    # every other durable cadence because the deadline it races
    # (`DEFAULT_BREAKER_MAX_EVIDENCE_AGE_SECONDS`, 3600) denies ALL paid
    # work when missed, and no other process can recover from it.
    (
        CADENCE_BREAKER_EVALUATE,
        "PROXY_BREAKER_EVAL_INTERVAL_SECONDS",
        _enqueue_breaker_evaluate,
    ),
    # EPA A4/B3 2026-09-03. Shares the entitlement refresh's 6h knob
    # rather than adding one: both are "keep a piece of durable evidence
    # from expiring" cadences, and the deadline here is a MONTH boundary
    # — six hours is three orders of magnitude of margin, so a knob of
    # its own would be a setting with no decision behind it.
    (
        CADENCE_FLEET_BUDGET_ROLLFORWARD,
        "ENTITLEMENT_REFRESH_INTERVAL_SECONDS",
        _enqueue_fleet_budget_rollforward,
    ),
    # EPA B3 2026-09-07, closing B2's owed wiring. Its OWN knob
    # (`DISPATCH_RECONCILE_INTERVAL_SECONDS`, 5m): this interval IS the
    # bound on how long a `POSTED` dispatch intent can sit unsettled
    # after the worker that sent it died — a real decision, not a
    # borrowed daily one. Durable rather than in-process for the reason
    # given on `_enqueue_dispatch_reconcile`.
    (
        CADENCE_DISPATCH_RECONCILE,
        "DISPATCH_RECONCILE_INTERVAL_SECONDS",
        _enqueue_dispatch_reconcile,
    ),
    # EPA C5 2026-09-08 (F19). Its OWN knob
    # (`EVIDENCE_RETENTION_INTERVAL_SECONDS`, daily) rather than sharing
    # `RETENTION_INTERVAL_SECONDS` with the partition drop: the two jobs
    # delete different kinds of thing with different reversibility, and
    # an operator who needs to pause irreversible blob deletion must not
    # have to stop ordinary partition retention to do it.
    (
        CADENCE_EVIDENCE_RETENTION,
        "EVIDENCE_RETENTION_INTERVAL_SECONDS",
        _enqueue_evidence_retention,
    ),
    # EPA C9 2026-09-08 (F14). Its OWN knob
    # (`LEDGER_SUMMARIZE_INTERVAL_SECONDS`, daily) rather than sharing
    # `RETENTION_INTERVAL_SECONDS`: this job's work is gated on a
    # provider settlement having arrived, so an operator tuning how often
    # to chase newly-settled operations is making a different decision
    # from how often to drop an expired partition.
    (
        CADENCE_LEDGER_SUMMARIZE_CHILDREN,
        "LEDGER_SUMMARIZE_INTERVAL_SECONDS",
        _enqueue_ledger_summarize_children,
    ),
)


def _run_durable_cadence_tick(settings: Settings) -> list[str]:
    """Claim every *due* daily cadence and enqueue its task.

    One system session, one transaction per cadence claim. The claim is
    the atomic ``UPDATE ... WHERE next_due_at <= now RETURNING id`` in
    ``app_shared.maintenance.cadence`` — so with N scheduler replicas
    exactly one enqueues, and a scheduler that crash-loops enqueues once
    rather than once per boot (the winning claim advances the deadline a
    full interval before the enqueue happens).

    Enqueue-then-commit, deliberately: this mirrors
    ``run_refresh_pass``'s "enqueue already happened; commit last"
    ordering and the codebase-wide duplicate-over-miss posture. All three
    maintenance tasks are idempotent no-arg sweeps, so a duplicate is
    free while a miss costs a full day.

    Returns the cadence keys actually claimed (for tests/diagnostics).
    Any exception — an unreachable database, a missing
    ``SYSTEM_DATABASE_URL`` — is logged and swallowed: a failed tick is
    retried on the next poll and must never crash-loop the scheduler
    (same posture as every other seam here). Crucially, a failure here no
    longer *loses* the cadence: the deadline is still in Postgres,
    unclaimed.
    """
    claimed: list[str] = []
    try:
        session_factory = get_system_sessionmaker()
        with session_factory() as session:
            ensure_cadence_rows(session)
            session.commit()

            now = datetime.now(timezone.utc)
            for cadence_key, interval_attr, enqueue_task in _DURABLE_CADENCES:
                interval = getattr(settings, interval_attr)
                if not claim_cadence(session, cadence_key, interval_seconds=interval, now=now):
                    session.rollback()
                    continue
                enqueue_task()
                session.commit()
                claimed.append(cadence_key)
                logger.info(
                    "scheduler: claimed durable cadence %s (interval %ss)",
                    cadence_key,
                    interval,
                )
    except Exception:
        logger.exception("scheduler: durable cadence tick failed")
    return claimed


def _run_health_tick(settings: Settings) -> None:
    """Assert on maintenance **outcomes** and emit ERROR signals.

    Answers "does the partition the calendar needs actually exist?" and
    "has the daily rollup produced a row recently?" by reading the
    database, plus "is any durable cadence sitting past its deadline?".
    Deliberately independent of whether the scheduler *thinks* it
    enqueued anything: in the 2026-08-15 incident the scheduler was
    enqueueing correctly and every task died on arrival in the worker for
    want of ``SYSTEM_DATABASE_URL``, so only an outcome check could have
    caught it. Errors are logged and swallowed — an observability probe
    must never be able to take down the process it observes.
    """
    try:
        now = datetime.now(timezone.utc)
        session_factory = get_system_sessionmaker()
        with session_factory() as session:
            report = check_maintenance_health(
                session,
                now_utc=now,
                months_ahead=settings.PARTITION_CREATE_LOOKAHEAD_MONTHS,
                rollup_stale_after_days=settings.MAINTENANCE_ROLLUP_STALE_AFTER_DAYS,
            )
            log_health_report(
                report,
                now_utc=now,
                threshold_days=settings.MAINTENANCE_ROLLUP_STALE_AFTER_DAYS,
            )
            log_overdue_cadences(
                read_cadence_statuses(session),
                now=now,
                grace_seconds=settings.MAINTENANCE_CADENCE_OVERDUE_GRACE_SECONDS,
            )
            session.rollback()
    except Exception:
        logger.exception("scheduler: maintenance health tick failed")


def _start_scheduler_heartbeat() -> PeriodicHeartbeat | None:
    """`heartbeat:scheduler:<instance>` every 30s (EPA B9, F22).

    F22 is "heartbeats for every process class", and the scheduler is the
    one process class whose silence is invisible from the outside: it
    serves no port, so nothing polls it, and every cadence it drives is
    slow enough that a dead scheduler looks like a quiet fleet for hours.
    This is also what makes `READY_REQUIRED_HEARTBEAT_SERVICES=scheduler,worker`
    (`docs/ops/RELEASE_1_2026-09.md`, checked in RELEASE_2's post-deploy
    step) a real check rather than a permanent 503: `/ready`'s
    `check_required_heartbeats` fails a declared service that nothing
    emits for.

    Started once from `main()`, on a daemon thread, so it keeps beating
    across the long `time.sleep`s and DB passes of the tick loop; a tick
    that blocks on a slow query therefore does NOT look like a dead
    process. The flip side is deliberate: this beat asserts the process
    is alive, not that its passes are healthy — maintenance *outcomes*
    are what `_run_health_tick`/`_run_ops_snapshot_tick` assert.

    `get_redis_client` is imported lazily for the same reason
    `_ops_snapshot_redis` does it, and the whole thing is best-effort:
    Redis being unreachable at boot must not stop the scheduler from
    running cadences. Returns the handle so `main()` can stop it on a
    graceful shutdown, or `None` when it could not start.
    """
    try:
        from app_shared.redis_client import get_redis_client

        return PeriodicHeartbeat(
            HeartbeatEmitter(
                get_redis_client(), service="scheduler", instance_id=default_instance_id()
            )
        ).start()
    except Exception:  # noqa: BLE001 - best-effort, see docstring
        logger.warning("scheduler heartbeat did not start", exc_info=True)
        return None


def _ops_snapshot_redis() -> object | None:
    """The process Redis client, or ``None`` when it cannot be had.

    Mirrors `apps/api/app/routers/ops_metrics.py::_get_redis`, including
    the lazy import: `get_redis_client()` performs the one-shot
    `maxmemory-policy` probe on first use (`app_shared.redis_policy`) and
    *raises* on an eviction-capable server, which is exactly the
    deployment state the snapshot exists to report. Letting that escape
    would turn the observability tick into a second outage. Without a
    client the Redis section still carries the cached
    `redis_policy.last_report()` posture — the signal that matters (audit
    H2) — and only loses the memory/eviction counters.
    """
    try:
        from app_shared.redis_client import get_redis_client

        return get_redis_client()
    except Exception:
        logger.exception("scheduler: ops snapshot could not obtain a redis client")
        return None


def _run_ops_snapshot_tick(settings: Settings) -> None:
    """Collect the ops snapshot and emit it plus every firing alert rule.

    Audit §H5 shipped the collector (`app_shared.opsmetrics.snapshot`),
    the rules (`...opsmetrics.rules`) and the emitter
    (`...opsmetrics.emit`), but wired them to exactly one caller: the
    on-demand `GET /ops/metrics` endpoint. Nothing evaluated the rules on
    a schedule, so every alert in that file could only fire while an
    operator was already looking at the dashboard — which is the failure
    mode H5 is about, one layer up. This tick is the scheduled evaluator:
    `emit_snapshot` writes the `ops.snapshot` line and one `ops.alert`
    line per firing rule (ERROR for CRITICAL/HIGH), so Railway's log view
    and any drain get a standing alerting record.

    Runs on the BYPASSRLS system sessionmaker for the same reason the API
    route runs on `get_auth_session()`: these are *fleet* aggregates, and
    a workspace-scoped session would silently under-report every one of
    them (spend most damagingly). Read-only — the session is rolled back,
    never committed.

    Errors are logged and swallowed exactly like `_run_health_tick`: an
    observability probe must never be able to take down the process it
    observes. `collect_snapshot` is itself section-isolating and
    documents that it never raises, so this guard covers the seams around
    it (obtaining a session, the emit).
    """
    try:
        session_factory = get_system_sessionmaker()
        with session_factory() as session:
            snapshot = collect_snapshot(
                session,
                now=datetime.now(timezone.utc),
                redis=_ops_snapshot_redis(),
                settings=settings,
            )
            alerts = emit_snapshot(snapshot)
            if alerts:
                logger.warning(
                    "scheduler: ops snapshot emitted %d firing alert(s)", len(alerts)
                )
            session.rollback()
    except Exception:
        logger.exception("scheduler: ops snapshot tick failed")


# ---------------------------------------------------------------------------
# EPA W4.2 — two-plane limits + weighted fair queuing (report §6)
# ---------------------------------------------------------------------------
#
# The pass below is an ALTERNATIVE due-rule pass, not a replacement of the
# SPEC-13 one: it is gated by `SCHEDULER_FAIR_QUEUE_ENABLED`, which
# defaults to False, so a deploy that merely takes this code changes no
# live scheduling behaviour at all. Turning it on swaps the SPEC-13
# refresh tick (`_run_refresh_pass_tick`) for `_run_fair_scheduling_tick`
# on the SAME cadence knob and the SAME batch limit — no new interval.
#
# What it adds over `run_refresh_pass`:
#
# * a FLEET plane: a per-domain concurrency cap enforced ACROSS tenants,
#   so two workspaces monitoring one merchant share that merchant's cap
#   instead of multiplying it (the workspace-scoped multiplication of
#   merchant traffic report §6 names);
# * a TENANT plane: weighted deficit round robin, so a workspace with ten
#   thousand due rules cannot consume the whole pass;
# * per-item failure isolation with bounded retries and a dead letter, so
#   one poison rule no longer aborts the pass for every other tenant (the
#   `break` in `run_refresh_pass`'s except branch).
#
# The policy itself lives in `app_shared.scheduling.fair_queue`, which is
# pure and deterministic; everything here is the thin I/O wiring —
# reading occupancy, resolving domains, firing a rule, recording a dead
# letter.


def read_fleet_domain_usage(session: Session, now: datetime) -> LiveUsage:
    """Live per-domain occupancy, read from C3's OWN reservations.

    The fleet plane must be measured against something authoritative, and
    C3 (`app_shared.costauth.service`) already holds exactly that: a
    ``RESERVED`` reservation with an unexpired lease IS one unit of live,
    paid-for work on that domain. Reading it here is what makes this a
    *composition* with the single budget authority rather than a second
    one — this module counts what C3 has granted; it never grants, never
    reserves, and never settles.

    "Live" is spelled the same way `CostAuthorizationService._check_
    concurrency` spells it (``RESERVED`` **and** ``lease_expires_at >
    now``), deliberately: a lapsed lease the sweeper has not reached yet
    must not be able to wedge a domain, and the two definitions drifting
    apart would silently change what the cap means.

    Cross-tenant by nature, so it runs on the sanctioned BYPASSRLS system
    session — the same seam, for the same reason, as `run_refresh_pass`'s
    due-rule claim and the outbox drain.
    """
    rows = session.execute(
        select(CostReservation.domain, func.count())  # noqa: workspace-scope
        .where(
            CostReservation.state == ReservationState.RESERVED,
            CostReservation.lease_expires_at > now,
        )
        .group_by(CostReservation.domain)
    ).all()
    per_domain = {str(domain): int(count) for domain, count in rows}
    return LiveUsage(per_domain=per_domain, total=sum(per_domain.values()))


def load_due_candidates(
    session: Session, *, now: datetime, limit: int
) -> list[ScheduleCandidate]:
    """Due `refresh_rules` as fair-queue candidates, with their domain resolved.

    The domain is what the fleet plane keys on, so it is resolved in the
    claim query rather than guessed: a ``COMPETITOR``-scope rule takes its
    competitor's ``domain``, a ``MATCH``-scope rule takes the domain of
    the competitor behind the match. Every other scope legitimately spans
    several domains, and a scheduler-plane cap cannot bind a domain it
    cannot name, so those become wildcard candidates — they still consume
    fleet capacity and a fair-share slot, and their per-domain accounting
    happens where the domain IS known: C3's per-batch authorization inside
    dispatch.

    Cross-tenant, sanctioned-unscoped, exactly like `run_refresh_pass`'s
    own claim (which this replaces when the fair queue is enabled).

    ``limit`` is deliberately larger than the pass's batch limit at the
    call site: the planner needs to SEE the noisy tenant's backlog in
    order to fairly not schedule most of it.

    **Work-level fairness at the window itself (EPA B3/F07).** Seeing the
    backlog only helps if the backlog does not fill the window. A single
    plain ``ORDER BY next_run_at LIMIT n`` hands back the noisiest
    tenant's oldest ``n`` rules when that tenant is far enough behind —
    and a workspace that is *absent from the candidate list* cannot be
    arbitrated for by any downstream fair-share pass, however fair that
    pass is. So the window is read in two pages: first one
    ``DISTINCT ON (workspace_id)`` row per workspace (each workspace's
    single most-overdue rule), then the remainder in plain due order,
    with the already-taken ids excluded. Every workspace with due work is
    therefore represented in the window before any workspace gets a
    second slot, and :func:`~app_shared.scheduling.fair_queue.plan_pass`
    does the actual admitting from a list that can no longer hide anyone.
    """
    if limit <= 0:
        return []

    def _rows(*, exclude: Sequence[uuid.UUID], distinct_per_workspace: bool, cap: int):
        """One page of the due window, with each rule's domain resolved."""
        match_competitor = Competitor.__table__.alias("match_competitor")
        stmt = (
            select(  # noqa: workspace-scope
                RefreshRule,
                func.coalesce(Competitor.domain, match_competitor.c.domain),
            )
            .select_from(RefreshRule)
            .outerjoin(
                Competitor,
                (Competitor.workspace_id == RefreshRule.workspace_id)
                & (Competitor.id == RefreshRule.competitor_id),
            )
            .outerjoin(
                CompetitorProductMatch,
                (CompetitorProductMatch.workspace_id == RefreshRule.workspace_id)
                & (CompetitorProductMatch.id == RefreshRule.match_id),
            )
            .outerjoin(
                match_competitor,
                (match_competitor.c.workspace_id == CompetitorProductMatch.workspace_id)
                & (match_competitor.c.id == CompetitorProductMatch.competitor_id),
            )
            .where(RefreshRule.enabled, RefreshRule.next_run_at <= now)
        )
        if exclude:
            stmt = stmt.where(RefreshRule.id.notin_(list(exclude)))
        if distinct_per_workspace:
            # Postgres requires the DISTINCT ON expression to lead the
            # ORDER BY; "most overdue first" therefore orders WITHIN a
            # workspace, and the resulting page is ordered by workspace.
            # The pass re-orders by freshness urgency anyway
            # (`fair_queue._ordering_key`), so the page's own order is
            # only ever a tie-break.
            stmt = stmt.distinct(RefreshRule.workspace_id).order_by(
                RefreshRule.workspace_id, RefreshRule.next_run_at, RefreshRule.id
            )
        else:
            stmt = stmt.order_by(RefreshRule.next_run_at, RefreshRule.id)
        return session.execute(stmt.limit(cap)).all()

    rows = list(_rows(exclude=(), distinct_per_workspace=True, cap=limit))
    remaining = limit - len(rows)
    if remaining > 0:
        rows.extend(
            _rows(
                exclude=[rule.id for rule, _domain in rows],
                distinct_per_workspace=False,
                cap=remaining,
            )
        )

    candidates: list[ScheduleCandidate] = []
    for rule, domain in rows:
        # The cadence IS the freshness target: a rule that asks to run
        # every 15 minutes is a rule whose data is stale after 15 minutes.
        # Cron rules have no single interval, so they fall back to the
        # no-target branch of `freshness_urgency` (hours overdue).
        target = (
            int(rule.interval_minutes) * 60
            if rule.interval_minutes is not None
            else None
        )
        candidates.append(
            ScheduleCandidate(
                key=str(rule.id),
                workspace_id=str(rule.workspace_id),
                domain=domain,
                due_at=rule.next_run_at,
                priority=int(rule.priority or 0),
                last_success_at=rule.last_run_at,
                freshness_target_seconds=target,
                payload={"scope": rule.scope.value, "name": rule.name},
            )
        )
    return candidates


def fire_refresh_rule(
    session: Session, *, rule_id: uuid.UUID | str, now: datetime
) -> bool:
    """Create + enqueue one due rule's job and advance its clock.

    The per-rule half of `run_refresh_pass`, addressed by id instead of by
    claim order, and holding the SAME row lock (``FOR UPDATE SKIP
    LOCKED``) so two scheduler replicas cannot both fire one rule. Returns
    False when the row is gone, disabled, held by another claimant, no
    longer due, or already fired for this occurrence — all five are
    ordinary, none is an error.

    **Two claims, not one (EPA B3/F07).** The row lock alone only settles
    a simultaneous race. It does not settle the sequential one, which is
    the one that actually happened: replica A loads a due candidate;
    replica B fires it and advances ``next_run_at``; A's lock then
    succeeds — on a row that is no longer due — and fires the same
    occurrence twice.

    1. ``.where(RefreshRule.next_run_at <= now)`` is part of the LOCKING
       select, so the due-ness A observed when it built its candidate list
       is *rechecked under the lock* rather than trusted. This is what
       makes a stale candidate cheap to discard.
    2. The occurrence itself — ``(rule_id, scheduled_for)``, where
       ``scheduled_for`` is ``next_run_at`` truncated to whole seconds —
       is INSERTed into ``refresh_rule_occurrences`` **before**
       `create_scope_job` runs. The primary key is the claim: a second
       INSERT for the same occurrence raises ``IntegrityError``, this
       rolls back and returns False, and no job is created. Postgres is
       the only participant that can see both claimants, so it is the one
       that decides.

    The recheck without the ledger would still lose to a clock skew or a
    long-enough pause between the two statements; the ledger without the
    recheck would turn every stale candidate into a wasted INSERT and
    rollback. Together they cost one extra predicate and one narrow row
    per firing.

    Enqueue-then-commit, like every other producer here: a crash between
    them re-fires the rule, which the SPEC-08 idempotent dispatch guard
    absorbs. Duplicates over misses.

    Raises whatever `create_scope_job` raises. That is the point — the
    caller (`app_shared.scheduling.fair_queue.execute_plan`) is what turns
    a raising rule into a bounded retry and then a dead letter, instead of
    into an aborted pass.
    """
    rule = (
        session.execute(
            select(RefreshRule)  # noqa: workspace-scope
            .where(
                RefreshRule.id == _as_uuid(rule_id),
                RefreshRule.enabled,
                RefreshRule.next_run_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .first()
    )
    if rule is None:
        return False

    scheduled_for = occurrence_key(rule.next_run_at)
    try:
        session.execute(
            insert(refresh_rule_occurrences).values(
                rule_id=rule.id, scheduled_for=scheduled_for, fired_at=now
            )
        )
        session.flush()
    except IntegrityError:
        # Another replica already claimed this occurrence. Not an error:
        # the work it names is being done, by someone.
        session.rollback()
        logger.info(
            "scheduler: occurrence already claimed rule_id=%s scheduled_for=%s",
            rule.id,
            scheduled_for.isoformat(),
        )
        return False

    target_id = _target_id_for_rule(rule)
    job_id, _status = create_scope_job(
        session,
        workspace_id=rule.workspace_id,
        scope=rule.scope,
        target_id=target_id,
        requested_by=None,
        job_type=ScrapeJobType.SCHEDULED,
        source=ScrapeJobSource.SCHEDULER,
    )
    if job_id is not None:
        # `job_id` is NULL when the scope resolved to zero matches
        # (FR-015: no matches -> no job). The occurrence still stands —
        # it was claimed and it happened.
        session.execute(
            update(refresh_rule_occurrences)
            .where(
                refresh_rule_occurrences.c.rule_id == rule.id,
                refresh_rule_occurrences.c.scheduled_for == scheduled_for,
            )
            .values(scrape_job_id=job_id)
        )
    rule.last_run_at = now
    rule.locked_at = now
    rule.next_run_at = compute_next_run_at(rule, now)
    session.commit()
    return True


def occurrence_key(next_run_at: datetime) -> datetime:
    """``next_run_at`` truncated to whole seconds — the occurrence identity.

    Sub-second precision is noise here, not information: a cadence is
    expressed in minutes or a cron expression, and `compute_next_run_at`
    derives the next due time from the previous one. Keeping microseconds
    in the primary key would let a microsecond of clock or round-trip
    drift mint a second "distinct" occurrence for what is plainly the
    same one — which is exactly the duplicate this key exists to prevent.
    """
    return next_run_at.replace(microsecond=0)



def _target_id_for_rule(rule: RefreshRule) -> uuid.UUID | None:
    """The non-null scope-target id for ``rule.scope`` (``None`` for WORKSPACE).

    Same mapping as `app.scheduler.refresh._target_id_for_rule`; kept here
    so this pass does not import the pass it replaces.
    """
    if rule.scope is ScrapeScope.WORKSPACE:
        return None
    if rule.scope is ScrapeScope.COMPETITOR:
        return rule.competitor_id
    if rule.scope is ScrapeScope.PRODUCT:
        return rule.product_id
    if rule.scope is ScrapeScope.VARIANT:
        return rule.product_variant_id
    if rule.scope is ScrapeScope.PRODUCT_GROUP:
        return rule.product_group_id
    if rule.scope is ScrapeScope.MATCH:
        return rule.match_id
    raise ValueError(f"unsupported scope {rule.scope!r}")


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


#: ``webhook_events.event_type`` announcing a dead-lettered scheduler item.
SCHEDULER_DEAD_LETTER_EVENT_TYPE = WebhookEventType.SCHEDULER_ITEM_DEAD_LETTERED.value


def record_dead_letter(
    session_factory: Callable[[], Session], record: DeadLetterRecord
) -> None:
    """Make one dead letter DURABLE: disable the rule, announce the event.

    Two writes in one transaction:

    1. ``refresh_rules.enabled = false`` — the terminal decision itself.
       ``enabled`` is the column the claim query already filters on and
       the one an operator already understands, so a restart cannot
       resurrect a rule this pass gave up on. This is the "existing store"
       half of the fallback: the in-memory ledger holds the *attempt
       count*, but the *outcome* lives in Postgres.
    2. A transactional-outbox message on the EXISTING
       ``webhook_events.create_webhook_event`` consumer, carrying
       ``event_type = scheduler.item.dead_lettered``. No new task name is
       invented (EPA B7's ruling: an outbox row naming a task nothing
       consumes is a message that looks delivered and never is), and the
       write is in the same transaction as the disable, so an operator
       cannot be told about a rule that is still running or left unaware
       of one that has stopped.

    ``dedup_key`` includes the attempt count, so a rule that is replayed,
    re-poisons and is dead-lettered again does announce a second time —
    the partial unique index only collapses UNPUBLISHED duplicates of the
    same crossing.

    Errors are logged and swallowed: this is the ledger's ``on_dead_letter``
    hook, and a sink that can raise into the pass would recreate exactly
    the "one bad item stops everything" failure the dead letter exists to
    remove.
    """
    try:
        with session_factory() as session:
            rule = (
                session.execute(
                    select(RefreshRule)  # noqa: workspace-scope
                    .where(RefreshRule.id == _as_uuid(record.key))
                    .with_for_update()
                )
                .scalars()
                .first()
            )
            if rule is not None:
                rule.enabled = False
                rule.updated_at = record.dead_lettered_at
            dedup_key = f"sched-dead-letter:{record.key}:{record.attempts}"
            write_outbox_message(
                session,
                workspace_id=record.workspace_id,
                task_name=CREATE_WEBHOOK_EVENT,
                queue="webhook_events",
                kwargs={
                    "workspace_id": str(record.workspace_id),
                    "event_type": SCHEDULER_DEAD_LETTER_EVENT_TYPE,
                    "payload": {
                        "refresh_rule_id": record.key,
                        "domain": record.domain,
                        "attempts": record.attempts,
                        "last_error": record.last_error,
                        "payload": dict(record.payload or {}),
                    },
                    "dedup_key": dedup_key,
                    "event_id": str(new_uuid7()),
                    "occurred_at": record.dead_lettered_at.isoformat(),
                },
                dedup_key=dedup_key,
                now=record.dead_lettered_at,
            )
            session.commit()
    except Exception:
        logger.exception(
            "scheduler: could not durably record dead letter for rule %s", record.key
        )


def replay_dead_letters(
    session_factory: Callable[[], Session],
    ledger: RetryLedger,
    *,
    keys: Sequence[str] | None = None,
) -> list[str]:
    """Replay tooling: un-park dead letters and re-enable their rules.

    The inverse of :func:`record_dead_letter`, and the reason dead-
    lettering is a safe thing to do automatically: an operator who has
    fixed the underlying cause runs this and the rules resume on the next
    pass with a clean attempt count.

    ``keys=None`` replays everything. Returns the keys actually replayed
    (a key that is not parked is skipped, not an error — replaying "all"
    twice is a normal operator action).
    """
    parked = {record.key for record in ledger.dead_letters()}
    wanted = parked if keys is None else [k for k in keys if k in parked]

    replayed: list[str] = []
    with session_factory() as session:
        for key in wanted:
            # Sanctioned unscoped access on the system seam, exactly like
            # the pass's own due-rule claim: replay is operator tooling
            # over a fleet-wide dead-letter set and is addressed by rule
            # id, which is globally unique. Nothing but `enabled` is read
            # or written, so no row content crosses a tenant boundary.
            rule = (
                session.execute(
                    select(RefreshRule)  # noqa: workspace-scope
                    .where(RefreshRule.id == _as_uuid(key))
                )
                .scalars()
                .first()
            )
            if rule is not None:
                rule.enabled = True
            ledger.replay(key)
            replayed.append(key)
        session.commit()
    if replayed:
        logger.info("scheduler: replayed %d dead-lettered rule(s)", len(replayed))
    return replayed


def fair_queue_policy(settings: Settings) -> tuple[FleetLimits, FairShare]:
    """The two planes' configured policy. Conservative defaults.

    Weights are uniform: this repository has no per-workspace scheduling
    tier to read them from, and inventing one (from plan name, spend, or
    rule count) would be a product decision made in a scheduler. Uniform
    weights are the honest default and already deliver the property the
    contract asks for — no tenant starves another — because deficit round
    robin with equal weights IS fair share. `FairShare.weights` is
    threaded through so a caller with a real tier can supply it without
    touching this module.
    """
    limits = FleetLimits(
        default_domain_concurrency=int(
            getattr(
                settings,
                "SCHEDULER_FAIR_QUEUE_DOMAIN_CONCURRENCY",
                DEFAULT_DOMAIN_CONCURRENCY,
            )
        ),
        fleet_concurrency=int(
            getattr(
                settings,
                "SCHEDULER_FAIR_QUEUE_FLEET_CONCURRENCY",
                DEFAULT_FLEET_CONCURRENCY,
            )
        ),
    )
    return limits, FairShare()


def run_fair_scheduling_pass(
    session_factory: Callable[[], Session],
    *,
    now: datetime,
    batch_limit: int,
    ledger: RetryLedger,
    limits: FleetLimits | None = None,
    share: FairShare | None = None,
    gate: Callable[[ScheduleCandidate], object] | None = None,
    candidate_multiplier: int = 10,
) -> PassOutcome:
    """One fair pass: load, plan under both planes, authorize, fire.

    ``gate`` is the authorization seam and defaults to C3's NON-SPENDING
    entitlement check (`CostAuthorizationService.assert_entitled`). That
    choice is load-bearing: paid work in this engine is authorized and
    reserved PER BATCH inside dispatch, where the domain, transport and
    byte estimate are actually known, so a scheduler that called
    `authorize()` here would reserve money against a request it is not the
    one making — a second budget authority by accident. What the scheduler
    plane legitimately owes is the one refusal that needs no batch: an
    unentitled workspace's rules should not create jobs at all.

    Ordering is fixed and is the contract: freshness urgency orders, the
    two planes admit, the gate authorizes, and only then does anything
    fire. An urgent item that the gate refuses appears in
    ``outcome.denied`` and never in ``outcome.dispatched``.

    **Per-rule isolation is two-sided (EPA B3/F07).** `execute_plan`
    already isolates a failure in-process: the ledger counts it, the pass
    continues, the bound eventually dead-letters it. What it cannot do
    from inside a pure planning module is touch the rule row — so a failed
    rule stayed due and came back as a candidate on the very next poll,
    consuming a fair-share slot per interval on the way to its bound.
    `gate` and `dispatch` are therefore wrapped here so that a **fault**
    (never a denial: a refusal is the system working, and
    `default_denial_reason` is what tells the two apart) also writes the
    durable half — ``consecutive_failures += 1`` and an exponential
    ``next_run_at`` backoff — before the exception is re-raised for the
    ledger to classify. A success clears the counter on the same row.
    """
    with session_factory() as session:
        usage = read_fleet_domain_usage(session, now)
        candidates = load_due_candidates(
            session, now=now, limit=max(batch_limit, batch_limit * candidate_multiplier)
        )
        session.rollback()

    if gate is None:
        service = CostAuthorizationService()

        def gate(candidate: ScheduleCandidate) -> object:  # noqa: F811
            service.assert_entitled(candidate.workspace_id)
            return None

    inner_gate = gate

    def gate_with_backoff(candidate: ScheduleCandidate) -> object:
        try:
            return inner_gate(candidate)
        except Exception as exc:  # noqa: BLE001 - re-raised; classified below
            if default_denial_reason(exc) is None:
                record_rule_failure(
                    session_factory, rule_id=candidate.key, error=exc, now=now
                )
            raise

    def dispatch(candidate: ScheduleCandidate, _grant: object) -> None:
        try:
            with session_factory() as session:
                fired = fire_refresh_rule(session, rule_id=candidate.key, now=now)
        except Exception as exc:  # noqa: BLE001 - re-raised for the ledger
            record_rule_failure(
                session_factory, rule_id=candidate.key, error=exc, now=now
            )
            raise
        if fired:
            clear_rule_failures(session_factory, rule_id=candidate.key)

    outcome = run_pass(
        candidates,
        now=now,
        batch_limit=batch_limit,
        gate=gate_with_backoff,
        dispatch=dispatch,
        ledger=ledger,
        limits=limits,
        usage=usage,
        share=share,
    )
    logger.info(
        "scheduler: fair pass candidates=%d dispatched=%d denied=%d "
        "retried=%d dead_lettered=%d deferred=%d",
        len(candidates),
        len(outcome.dispatched),
        len(outcome.denied),
        len(outcome.retried),
        len(outcome.dead_lettered),
        len(outcome.plan.deferred),
    )
    return outcome


#: Process-wide retry ledger for the fair pass. Module-level because the
#: attempt count must survive from one tick to the next within a process
#: (a per-tick ledger would make "bounded retries" mean "retry forever,
#: one attempt per pass"). It does NOT survive a restart — see the
#: PENDING-MIGRATION note: the durable half of a dead letter is the
#: disabled rule + the outbox event written by `record_dead_letter`.
_FAIR_QUEUE_LEDGER: RetryLedger | None = None


def _fair_queue_ledger(settings: Settings) -> RetryLedger:
    global _FAIR_QUEUE_LEDGER
    if _FAIR_QUEUE_LEDGER is None:
        _FAIR_QUEUE_LEDGER = RetryLedger(
            max_attempts=int(
                getattr(
                    settings, "SCHEDULER_FAIR_QUEUE_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS
                )
            ),
            on_dead_letter=lambda record: record_dead_letter(
                get_system_sessionmaker(), record
            ),
        )
    return _FAIR_QUEUE_LEDGER


def _run_fair_scheduling_tick(settings: Settings, batch_limit: int) -> None:
    """One fair pass on the system sessionmaker; errors logged and swallowed.

    Same posture as `_run_refresh_pass_tick`, which it replaces when
    `SCHEDULER_FAIR_QUEUE_ENABLED` is on: a failed tick is retried on the
    next poll interval and must never crash-loop the scheduler.
    """
    try:
        limits, share = fair_queue_policy(settings)
        run_fair_scheduling_pass(
            get_system_sessionmaker(),
            now=datetime.now(timezone.utc),
            batch_limit=batch_limit,
            ledger=_fair_queue_ledger(settings),
            limits=limits,
            share=share,
        )
    except Exception:
        logger.exception("scheduler: fair scheduling pass failed")


def main() -> None:
    # Audit §L1: refuse to boot when `ENVIRONMENT`/`RAILWAY_ENVIRONMENT_NAME`
    # says "production" and the resolved config still looks local-dev-shaped
    # (placeholder secrets, PGBOUNCER_AUTH_TYPE=trust, the DB bootstrap/owner
    # role as DATABASE_URL). A **no-op** whenever `is_production()` is false,
    # so local runs, `docker compose up` and CI are unaffected.
    #
    # First statement of `main()`, deliberately — the equivalent of the API's
    # import-time call in `apps/api/app/main.py`. The scheduler holds no
    # import-time state worth guarding, but `main()` is the only way this
    # process starts, and failing before the signal handlers/settings/loop
    # means an unsafe production deploy crash-loops visibly instead of
    # quietly enqueueing fleet-wide maintenance work under bad config.
    assert_production_safe()

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # EPA B9 (F22): start beating BEFORE the boot-time passes below, so a
    # scheduler wedged in its first cadence/health pass is still visibly
    # alive rather than indistinguishable from one that never booted.
    heartbeat = _start_scheduler_heartbeat()

    settings = get_settings()
    interval = settings.STRATEGY_STATS_FLUSH_INTERVAL_SECONDS
    refresh_interval = settings.SCHEDULER_POLL_INTERVAL_SECONDS
    refresh_batch_limit = settings.SCHEDULER_CLAIM_BATCH_LIMIT
    partition_create_interval = settings.PARTITION_CREATE_INTERVAL_SECONDS
    daily_rollup_interval = settings.DAILY_ROLLUP_INTERVAL_SECONDS
    retention_interval = settings.RETENTION_INTERVAL_SECONDS
    outbox_drain_interval = settings.OUTBOX_DRAIN_INTERVAL_SECONDS
    outbox_reconcile_interval = settings.OUTBOX_RECONCILE_INTERVAL_SECONDS
    entitlement_refresh_interval = settings.ENTITLEMENT_REFRESH_INTERVAL_SECONDS
    cadence_poll_interval = settings.MAINTENANCE_CADENCE_POLL_INTERVAL_SECONDS
    health_interval = settings.MAINTENANCE_HEALTH_INTERVAL_SECONDS
    ops_snapshot_interval = settings.OPS_SNAPSHOT_INTERVAL_SECONDS
    fair_queue_enabled = bool(getattr(settings, "SCHEDULER_FAIR_QUEUE_ENABLED", False))
    if fair_queue_enabled:
        logger.info(
            "scheduler: due-rule pass is the W4.2 FAIR QUEUE "
            "(two-plane limits + weighted round robin)"
        )

    logger.info(
        "scheduler up (strategy_light_recheck + strategy_stats_flush + "
        "finalize_jobs + recover_stalled_batches + costauth_reservation_sweep "
        "every %ss; "
        "refresh pass every %ss; DURABLE cadences partition_create every %ss / "
        "daily_rollup every %ss / retention_drop every %ss / entitlement_refresh "
        "every %ss polled every %ss from "
        "maintenance_cadences; outbox_drain every %ss; outbox_reconcile every %ss; "
        "maintenance health assertions every %ss; ops snapshot + alert "
        "evaluation every %ss)",
        interval,
        refresh_interval,
        partition_create_interval,
        daily_rollup_interval,
        retention_interval,
        entitlement_refresh_interval,
        cadence_poll_interval,
        outbox_drain_interval,
        outbox_reconcile_interval,
        health_interval,
        ops_snapshot_interval,
    )

    # Run both DB-backed passes once at boot, before the loop: a cadence
    # that is overdue (or has never run) must fire promptly rather than
    # wait a poll interval, and a partition gap must be reported at the
    # first opportunity. Neither can thunder: the cadence claim is atomic
    # and the health pass is read-only.
    _run_durable_cadence_tick(settings)
    _run_health_tick(settings)

    elapsed = 0.0
    refresh_elapsed = 0.0
    cadence_poll_elapsed = 0.0
    health_elapsed = 0.0
    outbox_drain_elapsed = 0.0
    outbox_reconcile_elapsed = 0.0
    # Deliberately NOT run once at boot, unlike the two passes above. The
    # snapshot is the heaviest read in this process (24h + 7d aggregates
    # across `request_attempts`/`price_observations`), and nothing it
    # reports is more urgent in the first quarter-hour after a deploy than
    # in the next one — every rule it evaluates is a slow-moving
    # condition. A crash-looping scheduler would otherwise pay that scan
    # per boot, at exactly the moment the database is least likely to be
    # healthy.
    ops_snapshot_elapsed = 0.0
    while not _shutdown_requested:
        time.sleep(_TICK_SECONDS)
        elapsed += _TICK_SECONDS
        refresh_elapsed += _TICK_SECONDS
        cadence_poll_elapsed += _TICK_SECONDS
        health_elapsed += _TICK_SECONDS
        outbox_drain_elapsed += _TICK_SECONDS
        outbox_reconcile_elapsed += _TICK_SECONDS
        ops_snapshot_elapsed += _TICK_SECONDS
        if elapsed >= interval:
            elapsed = 0.0
            _enqueue_light_recheck()
            _enqueue_stats_flush()
            # Same maintenance tick/knob (SPEC-12 precedent of reusing
            # this cadence): sweep dangling jobs + stalled batches.
            # EPA A3/B2: reap first, finalize second — a job whose last
            # STARTED orphan this sweep closes out becomes finalizable
            # within the same tick.
            _enqueue_reap_stale_targets()
            _enqueue_finalize_jobs()
            _enqueue_recover_stalled()
            _enqueue_redispatch_pending()
            # Same tick/knob again (EPA C3): reap expired cost-
            # authorization leases whose grant has no open operation in
            # the ledger. Minutes-scale by nature, so it belongs here and
            # not among the daily durable cadences.
            _enqueue_costauth_reservation_sweep()
        if refresh_elapsed >= refresh_interval:
            refresh_elapsed = 0.0
            # EPA W4.2: the fair pass REPLACES the SPEC-13 pass on the same
            # cadence and the same batch limit when enabled. Default off,
            # so taking this code changes nothing until an operator says so.
            if fair_queue_enabled:
                _run_fair_scheduling_tick(settings, refresh_batch_limit)
            else:
                _run_refresh_pass_tick(refresh_batch_limit)
        # NOTE: this accumulator only decides how often we ASK the
        # database; the daily deadlines themselves live in
        # `maintenance_cadences`, so resetting it on restart costs at most
        # one poll interval of detection latency, never a cadence.
        if cadence_poll_elapsed >= cadence_poll_interval:
            cadence_poll_elapsed = 0.0
            _run_durable_cadence_tick(settings)
        if health_elapsed >= health_interval:
            health_elapsed = 0.0
            _run_health_tick(settings)
        if outbox_drain_elapsed >= outbox_drain_interval:
            outbox_drain_elapsed = 0.0
            _enqueue_outbox_drain()
        if outbox_reconcile_elapsed >= outbox_reconcile_interval:
            outbox_reconcile_elapsed = 0.0
            _enqueue_outbox_reconcile()
        if ops_snapshot_elapsed >= ops_snapshot_interval:
            ops_snapshot_elapsed = 0.0
            _run_ops_snapshot_tick(settings)

    # The thread is a daemon and `HEARTBEAT_TTL_SECONDS` ages a stale beat
    # out on its own, so this is not load-bearing -- it just makes a
    # deliberately drained scheduler stop claiming to be alive at once
    # instead of for one more TTL.
    if heartbeat is not None:
        heartbeat.stop()

    logger.info("scheduler stopped")


if __name__ == "__main__":
    main()
