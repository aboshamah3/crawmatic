"""Default Scrapy settings for the `price_monitor_browser` (Scrapyd
browser) project (SPEC-14 T014, `contracts/browser-spider.md`, R9).

Completes the SPEC-01 skeleton for shared-runtime parity with the HTTP
project's settings (`apps/scrapers/price_monitor/settings.py`): the same
batched persistence pipeline, per-competitor robots handling (never the
blanket `ROBOTSTXT_OBEY` switch), and config-driven (never hardcoded)
concurrency/timeout/flush knobs read from `app_shared.config.get_settings()`
(Principle IV) -- while keeping the scrapy-playwright download handlers +
`AsyncioSelectorReactor` this project already had.

**SSRF safety gate (T031, US4, `contracts/browser-safety.md`, Constitution
§VI NON-NEGOTIABLE)**: three layers, mirroring the HTTP project's
`price_monitor/settings.py` plus one browser-specific addition --

* `SsrfGuardMiddleware` (priority 100, same as HTTP) -- pre-fetch scheme/
  userinfo guard; re-applied to every request Scrapy's downloader itself
  handles (the *original* Scrapy request only -- see the next bullet for
  why that isn't enough alone here).
* `DNS_RESOLVER = SafeResolver` -- defense-in-depth for any *non-Playwright*
  request this project still issues directly (e.g. `RobotsPolicyMiddleware`'s
  own robots.txt fetch, `contracts/browser-safety.md` "Robots"). Does
  **not** cover Playwright navigations themselves: `scrapy-playwright`
  never consults Scrapy's `DNS_RESOLVER` (Chromium resolves DNS itself),
  so `SafeResolver` alone would leave every browser navigation unchecked.
* `PLAYWRIGHT_ABORT_REQUEST = scrape_core.browser.ssrf.abort_unsafe_request`
  -- the browser-specific per-navigation-hop resolved-IP guard that closes
  that exact gap: scrapy-playwright invokes it for **every** Playwright
  request, including each redirect hop Chromium follows internally
  (bypassing `RedirectMiddleware`/`SsrfGuardMiddleware` entirely for that
  hop) -- see `scrape_core.browser.ssrf` module docstring.

**Connection-time egress guard (READY F01, plan task A1)**: the three
layers above are all *check-then-connect* — and for the browser, the
route hook only ever sees the FIRST request of a redirect chain (Chromium
follows `Location` inside its own network stack). `PLAYWRIGHT_LAUNCH_OPTIONS`
below therefore launches Chromium behind
`scrape_core.browser.egress_guard.EgressGuard` on loopback, with
`--proxy-bypass-list=<-loopback>` so not even a loopback IP literal
escapes it. Every connection the browser opens — each redirect hop,
sub-resource, worker, popup and WebSocket — is validated and then dialed
**on the address the guard itself resolved**, which is what closes DNS
rebinding. The layers above stay wired as defense in depth. See that
module's docstring.

**Resource-blocking policy (EPA B6)**: the same `PLAYWRIGHT_ABORT_REQUEST`
hook now also carries the sub-resource cost/category block policy
(`app_shared.profiles.browser_resource_policy` -- default blocklist:
image/media/font/stylesheet by type, ads/analytics/social by host,
`document` never blocked) for every non-navigation Playwright request.
No setting here changed to wire this in -- it lives inside
`abort_unsafe_request` itself, strictly AFTER the URL-safety check for
that request, never in place of it (that module's docstring).
"""

from app_shared.config import get_settings

import scrape_core  # noqa: F401  # proves libs/scrape-core is importable here
from scrape_core.browser.egress_guard import ensure_process_guard

# Config-driven (env/DB-tunable, Principle IV) -- never a hardcoded literal
# in this module. Read once, at the top, because the egress-guard wiring
# below needs it before the tuning block further down does.
_settings = get_settings()

BOT_NAME = "price_monitor_browser"

SPIDER_MODULES = ["price_monitor_browser.spiders"]
NEWSPIDER_MODULE = "price_monitor_browser.spiders"

# Per-competitor robots handling (RESPECT/REVIEW_REQUIRED/IGNORE_AFTER_APPROVAL,
# RobotsPolicyMiddleware) replaces Scrapy's blanket global switch -- never
# both (parity with price_monitor/settings.py).
ROBOTSTXT_OBEY = False

# Connect-time SSRF defense (defense-in-depth, contracts/browser-safety.md
# "SSRF" layer 2's sibling note) for any non-Playwright request this
# project issues directly (e.g. robots.txt fetches) -- parity with
# price_monitor/settings.py. Does NOT cover Playwright navigations
# themselves (see PLAYWRIGHT_ABORT_REQUEST below and the module docstring).
DNS_RESOLVER = "scrape_core.safety.resolver.SafeResolver"

REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"

# Parity with price_monitor/settings.py, and it must also reach the
# *browser* -- headless Chromium's own default UA advertises
# "HeadlessChrome", which amazon.sa answers with a bot-challenge page
# carrying none of the product markup (verified 2026-07-27: the same URL
# with this UA returns the real product page + price, while the headless
# default times out waiting for `#productTitle`).
#
# Scrapy's USER_AGENT does NOT reach Playwright navigations, so
# `_browser_request_for` stamps it onto every request's
# `playwright_context_kwargs` instead. Deliberately NOT via
# `PLAYWRIGHT_CONTEXTS`: a configured startup context makes *each* of the
# two download handlers (http + https) launch its own browser at spider
# open, and on this single-process node that wedged the spider before it
# issued a single request (verified 2026-07-27 -- "Launching browser
# chromium" twice, then 0 pages crawled forever).
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en",
}

# scrapy-playwright requires the asyncio reactor.
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"

# scrapy-playwright download handlers (browser-driven scraping).
DOWNLOAD_HANDLERS = {
    "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}
PLAYWRIGHT_BROWSER_TYPE = "chromium"

# Per-navigation-hop resolved-IP SSRF guard (T030/T031, Constitution §VI
# NON-NEGOTIABLE) -- scrapy-playwright calls this for every Playwright
# request, including each redirect hop Chromium follows internally (see
# `scrape_core.browser.ssrf` module docstring for why this is required in
# addition to `SsrfGuardMiddleware`/`DNS_RESOLVER` below).
PLAYWRIGHT_ABORT_REQUEST = "scrape_core.browser.ssrf.abort_unsafe_request"

# Connection-time egress guard (READY F01) -- see the module docstring for
# why the route hook above cannot be the enforcement point on its own.
#
# Started HERE, at settings import, rather than at spider open: the launch
# argument has to name the port, and scrapy-playwright launches the browser
# from the download handler with whatever `PLAYWRIGHT_LAUNCH_OPTIONS` said
# at import time. `ensure_process_guard()` is idempotent (one listener per
# process however often this module is imported) and runs the listener on
# its own event loop in a daemon thread, so the AsyncioSelectorReactor is
# untouched.
#
# `--proxy-bypass-list=<-loopback>`: the `<-loopback>` token REMOVES
# Chromium's built-in "never proxy localhost/127.0.0.1/[::1]" rule. Without
# it the one destination class the guard most needs to refuse is the one
# class Chromium would dial directly.
#
# A failure to start is deliberately fatal (`ensure_process_guard` does not
# swallow): a browser node that silently fell back to route-hook-only
# coverage is the exact state F01 exists to end.
if _settings.BROWSER_EGRESS_GUARD_ENABLED:
    _egress_guard = ensure_process_guard()
    PLAYWRIGHT_LAUNCH_OPTIONS = {
        "args": [
            f"--proxy-server=http://127.0.0.1:{_egress_guard.port}",
            "--proxy-bypass-list=<-loopback>",
        ]
    }
else:
    _egress_guard = None
    PLAYWRIGHT_LAUNCH_OPTIONS = {}

ITEM_PIPELINES = {
    "scrape_core.pipelines.BatchedPersistencePipeline": 300,
}

# Pre-fetch scheme/userinfo SSRF guard (priority 100, same priority as
# price_monitor/settings.py) ordered ahead of per-request robots handling
# (110) so a rejection short-circuits before any other request processing
# -- exact parity with the HTTP project's middleware ordering.
DOWNLOADER_MIDDLEWARES = {
    "scrape_core.safety.middleware.SsrfGuardMiddleware": 100,
    "scrape_core.robots.RobotsPolicyMiddleware": 110,
    # See the HTTP project's settings for why 120: a bot interstitial
    # served with HTTP 200 must become a retryable failure, not a
    # PRICE_NOT_FOUND. The browser spider has no retry ladder of its own,
    # so here it simply surfaces as BLOCKED instead of a bogus
    # "no price on the page" (scrape_core.blocking).
    "scrape_core.blocking.BlockDetectionMiddleware": 120,
    # EPA C4 (READY-005): the physical network-operation ledger boundary,
    # at the same priority as the HTTP project for the same two reasons
    # (see that settings module and the middleware's own docstring). On
    # this node the middleware ALSO turns B6b's per-response
    # sub-resource measurements into buffered CHILD operation rows
    # (`parent_operation_id` = the navigation), which is what closes the
    # canary's 68-recorded / 147-actual request-count gap. It never
    # touches B6's resource-blocking policy: that runs on the Playwright
    # request side (`PLAYWRIGHT_ABORT_REQUEST`, url-safety strictly
    # first, then the block policy), and an aborted asset produces no
    # response event for this middleware to account for.
    "scrape_core.netledger_middleware.NetLedgerMiddleware": 130,
}

# EPA C4: see the HTTP project's settings for why this switch is
# auditable rather than a silent degrade.
NETLEDGER_ENABLED = True

# Low bounded browser concurrency (analyze A1): each context/page is an
# expensive real Chromium instance. Values from `_settings` (read at the
# top of this module), never hardcoded literals here.
CONCURRENT_REQUESTS = _settings.BROWSER_CONCURRENT_REQUESTS
PLAYWRIGHT_MAX_CONTEXTS = _settings.BROWSER_MAX_CONTEXTS
PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT = _settings.SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS

# Per-response download bounds (READY-013-c, parity with
# price_monitor/settings.py). Caps all three response-bomb shapes from
# one knob -- an over-large declared Content-Length, a lying/unbounded
# stream, and a decompression bomb -- plus the wall-clock companion.
# Values from `Settings` (env-tunable), never hardcoded literals here.
DOWNLOAD_MAXSIZE = _settings.SCRAPE_DOWNLOAD_MAXSIZE_BYTES
DOWNLOAD_WARNSIZE = _settings.SCRAPE_DOWNLOAD_WARNSIZE_BYTES
DOWNLOAD_TIMEOUT = _settings.SCRAPE_DOWNLOAD_TIMEOUT_SECONDS

# Batched-flush thresholds (contracts/persistence-pipeline.md, parity with
# price_monitor/settings.py) -- read from `Settings`/config, never
# hardcoded literals here.
SCRAPE_FLUSH_MAX_ITEMS = _settings.SCRAPE_FLUSH_MAX_ITEMS
SCRAPE_FLUSH_INTERVAL_SECONDS = _settings.SCRAPE_FLUSH_INTERVAL_SECONDS

# Durable result spool (EPA F05, plan task B1) -- the SQLite/WAL file
# every `ScrapeResult` is written to BEFORE it enters the in-memory flush
# buffer, so a failed flush or a killed container replays instead of
# losing work. A per-HOST fact (which volume this container mounted), read
# from `Settings` like everything else here, never a hardcoded literal.
SCRAPE_RESULT_SPOOL_PATH = str(_settings.SCRAPE_RESULT_SPOOL_PATH)
SCRAPE_FLUSH_MAX_PENDING_BATCHES = _settings.SCRAPE_FLUSH_MAX_PENDING_BATCHES
SCRAPE_FLUSH_QUARANTINE_AFTER = _settings.SCRAPE_FLUSH_QUARANTINE_AFTER


# Per-spider-process memory ceiling (2026-08-03 memory-leak hardening,
# parity with price_monitor/settings.py). Scrapy's `MemoryUsage`
# extension measures only *this* process's RSS -- Chromium runs as a
# separate process tree, so on this node the setting bounds the Scrapy
# side and the container-wide backstop (`price_monitor_browser.scrapyd_app`,
# WATCHDOG_MEMORY_LIMIT_MB) is what catches a leaking browser. Values
# from `Settings` (env-tunable), never hardcoded literals here.
MEMUSAGE_ENABLED = True
MEMUSAGE_LIMIT_MB = _settings.SCRAPE_MEMUSAGE_LIMIT_MB
MEMUSAGE_WARNING_MB = _settings.SCRAPE_MEMUSAGE_WARNING_MB
MEMUSAGE_CHECK_INTERVAL_SECONDS = _settings.SCRAPE_MEMUSAGE_CHECK_INTERVAL_SECONDS
