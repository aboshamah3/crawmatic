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
just-finalized job. A genuine ``celery beat``-style scheduler is a later
spec's concern (the "SPEC-13 beat" referenced in ``scrape_core.pipelines``);
this bare interval loop is deliberately minimal until then. The loop
still exits cleanly on SIGTERM/SIGINT so the container can be stopped by
the orchestrator without a crash-loop.
"""

from __future__ import annotations

import logging
import signal
import time
from types import FrameType

from app_shared.config import get_settings
from app_shared.messaging import enqueue
from app_shared.task_names import STRATEGY_LIGHT_RECHECK, STRATEGY_STATS_FLUSH

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


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    settings = get_settings()
    interval = settings.STRATEGY_STATS_FLUSH_INTERVAL_SECONDS

    logger.info(
        "scheduler up (strategy_light_recheck + strategy_stats_flush every %ss)", interval
    )
    elapsed = 0.0
    while not _shutdown_requested:
        time.sleep(_TICK_SECONDS)
        elapsed += _TICK_SECONDS
        if elapsed >= interval:
            elapsed = 0.0
            _enqueue_light_recheck()
            _enqueue_stats_flush()

    logger.info("scheduler stopped")


if __name__ == "__main__":
    main()
