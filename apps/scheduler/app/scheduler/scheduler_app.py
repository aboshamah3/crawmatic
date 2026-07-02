"""Scheduler process entry point.

Boots to a running loop via ``python -m app.scheduler.scheduler_app``
(contracts/service-topology.md). No business logic exists yet in this
skeleton phase (SPEC-01) — later specs add periodic scan scheduling here.
The loop simply stays alive and exits cleanly on SIGTERM/SIGINT so the
container can be stopped by the orchestrator without a crash-loop.
"""

from __future__ import annotations

import logging
import signal
import time
from types import FrameType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler")

_shutdown_requested = False


def _handle_shutdown(signum: int, frame: FrameType | None) -> None:
    global _shutdown_requested
    logger.info("scheduler received signal %s, shutting down", signum)
    _shutdown_requested = True


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    logger.info("scheduler up")
    while not _shutdown_requested:
        time.sleep(1)

    logger.info("scheduler stopped")


if __name__ == "__main__":
    main()
