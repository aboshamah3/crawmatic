"""Default Scrapy settings for the `price_monitor_browser` (Scrapyd
browser) project (SPEC-14 T014, `contracts/browser-spider.md`, R9).

Completes the SPEC-01 skeleton for shared-runtime parity with the HTTP
project's settings (`apps/scrapers/price_monitor/settings.py`): the same
batched persistence pipeline, per-competitor robots handling (never the
blanket `ROBOTSTXT_OBEY` switch), and config-driven (never hardcoded)
concurrency/timeout/flush knobs read from `app_shared.config.get_settings()`
(Principle IV) -- while keeping the scrapy-playwright download handlers +
`AsyncioSelectorReactor` this project already had.

**SSRF safety gate (NOT wired here, Constitution §VI NON-NEGOTIABLE):**
`SsrfGuardMiddleware`, `DNS_RESOLVER = SafeResolver`, and
`PLAYWRIGHT_ABORT_REQUEST` are added in T030/T031 (US4) -- a required
MVP safety gate. The browser path MUST NOT be dispatched against real
targets in production until those land, even though this module alone
is importable/testable now.
"""

from app_shared.config import get_settings

import scrape_core  # noqa: F401  # proves libs/scrape-core is importable here

BOT_NAME = "price_monitor_browser"

SPIDER_MODULES = ["price_monitor_browser.spiders"]
NEWSPIDER_MODULE = "price_monitor_browser.spiders"

# Per-competitor robots handling (RESPECT/REVIEW_REQUIRED/IGNORE_AFTER_APPROVAL,
# RobotsPolicyMiddleware) replaces Scrapy's blanket global switch -- never
# both (parity with price_monitor/settings.py).
ROBOTSTXT_OBEY = False

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

ITEM_PIPELINES = {
    "scrape_core.pipelines.BatchedPersistencePipeline": 300,
}

# Per-request (never process-global) robots_policy enforcement (parity
# with price_monitor/settings.py's RobotsPolicyMiddleware priority).
# The SSRF guard (`SsrfGuardMiddleware`, priority 100) is added in T031 --
# left out here deliberately (see module docstring).
DOWNLOADER_MIDDLEWARES = {
    "scrape_core.robots.RobotsPolicyMiddleware": 110,
}

# Config-driven (env/DB-tunable, Principle IV) -- never a hardcoded
# literal here. Low bounded browser concurrency (analyze A1): each
# context/page is an expensive real Chromium instance.
_settings = get_settings()
CONCURRENT_REQUESTS = _settings.BROWSER_CONCURRENT_REQUESTS
PLAYWRIGHT_MAX_CONTEXTS = _settings.BROWSER_MAX_CONTEXTS
PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT = _settings.SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS

# Batched-flush thresholds (contracts/persistence-pipeline.md, parity with
# price_monitor/settings.py) -- read from `Settings`/config, never
# hardcoded literals here.
SCRAPE_FLUSH_MAX_ITEMS = _settings.SCRAPE_FLUSH_MAX_ITEMS
SCRAPE_FLUSH_INTERVAL_SECONDS = _settings.SCRAPE_FLUSH_INTERVAL_SECONDS
