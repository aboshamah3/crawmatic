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
from app_shared.task_names import STRATEGY_LIGHT_RECHECK, STRATEGY_STATS_FLUSH

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

    logger.info(
        "scheduler up (strategy_light_recheck + strategy_stats_flush every %ss; "
        "refresh pass every %ss)",
        interval,
        refresh_interval,
    )
    elapsed = 0.0
    refresh_elapsed = 0.0
    while not _shutdown_requested:
        time.sleep(_TICK_SECONDS)
        elapsed += _TICK_SECONDS
        refresh_elapsed += _TICK_SECONDS
        if elapsed >= interval:
            elapsed = 0.0
            _enqueue_light_recheck()
            _enqueue_stats_flush()
        if refresh_elapsed >= refresh_interval:
            refresh_elapsed = 0.0
            _run_refresh_pass_tick(refresh_batch_limit)

    logger.info("scheduler stopped")


if __name__ == "__main__":
    main()
