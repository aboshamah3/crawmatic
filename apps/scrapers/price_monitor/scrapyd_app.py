"""Scrapyd application factory for the `scrapers` (HTTP) node.

Wired via ``application = price_monitor.scrapyd_app.application`` in
``apps/scrapers/scrapyd.conf`` (Scrapyd's own default is
``scrapyd.app.application``, which this delegates to unchanged). Its only
job is to give the long-lived Scrapyd **parent** process a startup hook.

Why this hook and not another: on 2026-08-03 this service sat at a
7.65 GB plateau for ~10 hours *while idle*, i.e. with no spider
subprocess alive — so the leak lives in the twistd daemon that Scrapyd
runs forever, not in a crawl. Scrapy's ``MemoryUsage`` extension
(``price_monitor/settings.py``) only ever sees a spider subprocess, so it
could not have caught it. Of the available seams, ``application`` is the
only one Scrapyd evaluates exactly once, in that parent process, before
the reactor starts:

* ``runner`` (how ``asyncio_runner`` is wired on the browser node) runs
  per *crawl subprocess* — wrong process, and once per job.
* ``docker-entrypoint.sh`` ``exec``s ``scrapyd``, so a shell-level
  background process would be a sibling that cannot terminate PID 1.
* ``sitecustomize`` would fire in every subprocess too, and silently.

The module is importable process-wide (``price_monitor`` is an installed
workspace package), so it does not depend on the daemon's ``sys.path``
containing the working directory.
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
    start_memory_watchdog("scrapers")
    return scrapyd_application(config)
