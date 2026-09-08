"""Scrapyd application factory for the `scrapers-browser` node.

Wired via ``application =
price_monitor_browser.scrapyd_app.application`` in
``apps/scrapers-browser/scrapyd.conf`` — parity with
``price_monitor.scrapyd_app`` on the HTTP node; see that module's
docstring for why the ``application`` seam is the one startup hook that
genuinely runs in the long-lived Scrapyd parent process (the 2026-08-03
7.65 GB idle plateau lived outside any spider subprocess).

Note this node also owns Chromium processes, whose memory Scrapy's
``MemoryUsage`` extension cannot see at all — the cgroup-based watchdog
started here is the only guard that counts them, since cgroup accounting
covers the whole container.
"""

from __future__ import annotations

import logging
from typing import Any

from scrapyd.app import application as scrapyd_application

from app_shared.heartbeat import HeartbeatEmitter, PeriodicHeartbeat, default_instance_id
from app_shared.memory_watchdog import start_memory_watchdog
from app_shared.redis_client import get_redis_client

logger = logging.getLogger(__name__)


def _start_scraper_heartbeat() -> None:
    """`heartbeat:scraper:<node>` every 30s (EPA B9, F22).

    Best-effort, exactly like `start_memory_watchdog` above: a Redis
    client or settings failure at daemon startup must never keep this
    Scrapyd node from coming up. Shares the "scraper" service name with
    the HTTP node (`price_monitor.scrapyd_app`) — both are Scrapyd nodes
    in the same fleet `/ready` aggregates by instance id, not by node
    type.
    """
    try:
        client = get_redis_client()
        PeriodicHeartbeat(
            HeartbeatEmitter(client, service="scraper", instance_id=default_instance_id())
        ).start()
    except Exception:  # noqa: BLE001 - best-effort, see docstring
        logger.warning("scraper heartbeat did not start", exc_info=True)


def application(config: Any) -> Any:
    """Start the container memory watchdog and the scraper heartbeat, then
    build Scrapyd's app.

    The watchdog is a no-op unless ``WATCHDOG_MEMORY_LIMIT_MB`` is set
    (Railway: 3072 for this service); neither it nor the heartbeat ever
    raises, so a failure to start either can never keep the node from
    coming up.
    """
    start_memory_watchdog("scrapers-browser")
    _start_scraper_heartbeat()
    return scrapyd_application(config)
