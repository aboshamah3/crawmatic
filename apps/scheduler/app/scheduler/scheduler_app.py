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
from datetime import datetime, timezone
from types import FrameType

from app_shared.config import Settings, get_settings
from app_shared.config_validation import assert_production_safe
from app_shared.database import get_system_sessionmaker
from app_shared.maintenance.cadence import (
    claim_cadence,
    ensure_cadence_rows,
    log_overdue_cadences,
    read_cadence_statuses,
)
from app_shared.maintenance.health import check_maintenance_health, log_health_report
from app_shared.messaging import enqueue
from app_shared.models.maintenance_cadence import (
    CADENCE_DAILY_ROLLUP,
    CADENCE_PARTITION_CREATE,
    CADENCE_RETENTION_DROP,
)
from app_shared.opsmetrics import collect_snapshot, emit_snapshot
from app_shared.task_names import (
    MAINTENANCE_DAILY_ROLLUP,
    MAINTENANCE_PARTITION_CREATE,
    MAINTENANCE_RETENTION_DROP,
    OUTBOX_DRAIN,
    OUTBOX_RECONCILE,
    SCRAPE_FINALIZE_JOBS,
    SCRAPE_RECOVER_STALLED,
    SCRAPE_REDISPATCH_JOBS,
    STRATEGY_LIGHT_RECHECK,
    STRATEGY_STATS_FLUSH,
)

from app.scheduler.refresh import run_refresh_pass

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
_DURABLE_CADENCES = (
    (CADENCE_PARTITION_CREATE, "PARTITION_CREATE_INTERVAL_SECONDS", _enqueue_partition_create),
    (CADENCE_DAILY_ROLLUP, "DAILY_ROLLUP_INTERVAL_SECONDS", _enqueue_daily_rollup),
    (CADENCE_RETENTION_DROP, "RETENTION_INTERVAL_SECONDS", _enqueue_retention_drop),
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

    settings = get_settings()
    interval = settings.STRATEGY_STATS_FLUSH_INTERVAL_SECONDS
    refresh_interval = settings.SCHEDULER_POLL_INTERVAL_SECONDS
    refresh_batch_limit = settings.SCHEDULER_CLAIM_BATCH_LIMIT
    partition_create_interval = settings.PARTITION_CREATE_INTERVAL_SECONDS
    daily_rollup_interval = settings.DAILY_ROLLUP_INTERVAL_SECONDS
    retention_interval = settings.RETENTION_INTERVAL_SECONDS
    outbox_drain_interval = settings.OUTBOX_DRAIN_INTERVAL_SECONDS
    outbox_reconcile_interval = settings.OUTBOX_RECONCILE_INTERVAL_SECONDS
    cadence_poll_interval = settings.MAINTENANCE_CADENCE_POLL_INTERVAL_SECONDS
    health_interval = settings.MAINTENANCE_HEALTH_INTERVAL_SECONDS
    ops_snapshot_interval = settings.OPS_SNAPSHOT_INTERVAL_SECONDS

    logger.info(
        "scheduler up (strategy_light_recheck + strategy_stats_flush + "
        "finalize_jobs + recover_stalled_batches every %ss; "
        "refresh pass every %ss; DURABLE cadences partition_create every %ss / "
        "daily_rollup every %ss / retention_drop every %ss polled every %ss from "
        "maintenance_cadences; outbox_drain every %ss; outbox_reconcile every %ss; "
        "maintenance health assertions every %ss; ops snapshot + alert "
        "evaluation every %ss)",
        interval,
        refresh_interval,
        partition_create_interval,
        daily_rollup_interval,
        retention_interval,
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
            _enqueue_finalize_jobs()
            _enqueue_recover_stalled()
            _enqueue_redispatch_pending()
        if refresh_elapsed >= refresh_interval:
            refresh_elapsed = 0.0
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

    logger.info("scheduler stopped")


if __name__ == "__main__":
    main()
