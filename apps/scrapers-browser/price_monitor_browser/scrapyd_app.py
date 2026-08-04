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

from typing import Any

from scrapyd.app import application as scrapyd_application

from app_shared.memory_watchdog import start_memory_watchdog


def application(config: Any) -> Any:
    """Start the container memory watchdog, then build Scrapyd's app.

    No-op unless ``WATCHDOG_MEMORY_LIMIT_MB`` is set (Railway: 3072 for
    this service); the watchdog never raises, so a failure to start it
    can never keep the node from coming up.
    """
    start_memory_watchdog("scrapers-browser")
    return scrapyd_application(config)
