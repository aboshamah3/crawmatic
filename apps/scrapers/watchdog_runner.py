"""Scrapyd crawl runner for the HTTP node that arms the process watchdog.

The stock ``scrapyd.runner`` with one addition, made before Twisted or
Scrapy is imported: ``scrape_core.process_watchdog.arm_crawl_watchdog``
puts the crawl in its own process group and starts a daemon thread that
kills that group once the crawl has outlived its graceful
``CLOSESPIDER_TIMEOUT`` and the in-reactor hard deadline by
``SCRAPE_PROCESS_WATCHDOG_EXTRA_SECONDS`` (2026-09-29, E1.3). The in-reactor
deadline cannot fire while the reactor is blocked -- the shape of an
impersonated curl transfer that never returns, which held four of this
node's eight slots for over an hour on 2026-09-23.

Referenced by ``scrapyd.conf`` ``runner = watchdog_runner``; importable
because Scrapyd spawns crawl subprocesses with this directory as cwd
(``python -m`` puts cwd on ``sys.path``), exactly like the browser node's
``asyncio_runner``.
"""

from scrape_core.process_watchdog import arm_crawl_watchdog

arm_crawl_watchdog("http")

from scrapyd.runner import main  # noqa: E402  (must import after arming)

if __name__ == "__main__":
    main()
