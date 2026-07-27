"""Scrapyd crawl runner that installs the asyncio reactor FIRST.

scrapy-playwright requires ``AsyncioSelectorReactor``
(``price_monitor_browser.settings.TWISTED_REACTOR``), but the stock
``scrapyd.runner`` import path ends up with Twisted's default epoll
reactor already installed by the time Scrapy checks the setting, so
every crawl on this node died ~1s in with a reactor mismatch (same
failure mode the HTTP node hit and worked around by capitulating to
``EPollReactor`` -- see ``apps/scrapers/price_monitor/settings.py``;
this node cannot capitulate, Playwright genuinely needs asyncio).

Referenced by ``scrapyd.conf`` ``runner = asyncio_runner``; importable
because Scrapyd spawns crawl subprocesses with this directory as cwd
(``python -m`` puts cwd on ``sys.path``).
"""

from twisted.internet import asyncioreactor

asyncioreactor.install()

from scrapyd.runner import main  # noqa: E402  (must import after install)

if __name__ == "__main__":
    main()
