"""Default Scrapy settings for the `price_monitor_browser` (Scrapyd
browser) project.

No spiders or scraping/extraction logic exist in this skeleton phase
(SPEC-01) — see spec Scope Boundary. This module wires the
scrapy-playwright download handlers with a deliberately low browser
concurrency (analyze A1) and imports `scrape_core` to prove the shared
scraping-library dependency boundary (FR-004, FR-009).
"""

import scrape_core  # noqa: F401  # proves libs/scrape-core is importable here

BOT_NAME = "price_monitor_browser"

SPIDER_MODULES = ["price_monitor_browser.spiders"]
NEWSPIDER_MODULE = "price_monitor_browser.spiders"

ROBOTSTXT_OBEY = True

REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
# scrapy-playwright requires the asyncio reactor.
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"

# scrapy-playwright download handlers (browser-driven scraping).
DOWNLOAD_HANDLERS = {
    "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}
PLAYWRIGHT_BROWSER_TYPE = "chromium"

# Low browser concurrency (analyze A1) — each context/page is expensive
# (a real Chromium instance), so this node keeps resource usage bounded:
# at most 2 concurrent requests, sharing at most 1 browser context.
CONCURRENT_REQUESTS = 2
PLAYWRIGHT_MAX_CONTEXTS = 1
