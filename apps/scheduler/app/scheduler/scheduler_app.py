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
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone
from types import FrameType

from app_shared.config import get_settings
from app_shared.database import get_system_sessionmaker
from app_shared.messaging import enqueue
from app_shared.task_names import (
    MAINTENANCE_DAILY_ROLLUP,
    MAINTENANCE_PARTITION_CREATE,
    MAINTENANCE_RETENTION_DROP,
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


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    settings = get_settings()
    interval = settings.STRATEGY_STATS_FLUSH_INTERVAL_SECONDS
    refresh_interval = settings.SCHEDULER_POLL_INTERVAL_SECONDS
    refresh_batch_limit = settings.SCHEDULER_CLAIM_BATCH_LIMIT
    partition_create_interval = settings.PARTITION_CREATE_INTERVAL_SECONDS
    daily_rollup_interval = settings.DAILY_ROLLUP_INTERVAL_SECONDS
    retention_interval = settings.RETENTION_INTERVAL_SECONDS

    logger.info(
        "scheduler up (strategy_light_recheck + strategy_stats_flush every %ss; "
        "refresh pass every %ss; partition_create every %ss; daily_rollup every %ss; "
        "retention_drop every %ss)",
        interval,
        refresh_interval,
        partition_create_interval,
        daily_rollup_interval,
        retention_interval,
    )
    elapsed = 0.0
    refresh_elapsed = 0.0
    partition_create_elapsed = 0.0
    daily_rollup_elapsed = 0.0
    retention_elapsed = 0.0
    while not _shutdown_requested:
        time.sleep(_TICK_SECONDS)
        elapsed += _TICK_SECONDS
        refresh_elapsed += _TICK_SECONDS
        partition_create_elapsed += _TICK_SECONDS
        daily_rollup_elapsed += _TICK_SECONDS
        retention_elapsed += _TICK_SECONDS
        if elapsed >= interval:
            elapsed = 0.0
            _enqueue_light_recheck()
            _enqueue_stats_flush()
        if refresh_elapsed >= refresh_interval:
            refresh_elapsed = 0.0
            _run_refresh_pass_tick(refresh_batch_limit)
        if partition_create_elapsed >= partition_create_interval:
            partition_create_elapsed = 0.0
            _enqueue_partition_create()
        if daily_rollup_elapsed >= daily_rollup_interval:
            daily_rollup_elapsed = 0.0
            _enqueue_daily_rollup()
        if retention_elapsed >= retention_interval:
            retention_elapsed = 0.0
            _enqueue_retention_drop()

    logger.info("scheduler stopped")


if __name__ == "__main__":
    main()
