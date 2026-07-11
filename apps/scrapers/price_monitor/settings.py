"""Default Scrapy settings for the `price_monitor` (Scrapyd HTTP) project.

SPEC-07 US1: wires the batched persistence pipeline
(`scrape_core.pipelines.BatchedPersistencePipeline`, contracts/
persistence-pipeline.md) and disables Scrapy's process-global
`ROBOTSTXT_OBEY` — robots handling is per-competitor
(`RobotsPolicyMiddleware`, US2), never this blanket switch. This module
also imports `scrape_core` to prove the shared-library dependency
boundary (FR-004): this Scrapyd node may depend on `scrape_core`, never
the reverse.

SPEC-07 US2: installs `SafeResolver` as the process's `DNS_RESOLVER`
(connect-time SSRF defense, defeats DNS rebinding) and registers
`SsrfGuardMiddleware` + `RobotsPolicyMiddleware` in
`DOWNLOADER_MIDDLEWARES` (contracts/fetch-url-safety.md,
contracts/robots-middleware.md) — every fetch is safety-checked before
body download and robots-policy-checked per competitor.
"""

from app_shared.config import get_settings

import scrape_core  # noqa: F401  # proves libs/scrape-core is importable here

BOT_NAME = "price_monitor"

SPIDER_MODULES = ["price_monitor.spiders"]
NEWSPIDER_MODULE = "price_monitor.spiders"

# Per-competitor robots handling (RESPECT/REVIEW_REQUIRED/IGNORE_AFTER_APPROVAL,
# US2's RobotsPolicyMiddleware) replaces Scrapy's blanket global switch —
# never both (contracts/robots-middleware.md).
ROBOTSTXT_OBEY = False

# Connect-time SSRF defense (contracts/fetch-url-safety.md, research D2):
# resolves then refuses to hand back a private/loopback/link-local/
# reserved/multicast/unspecified IP, so a connection can never proceed
# to an internal address even via DNS rebinding after request-build time.
DNS_RESOLVER = "scrape_core.safety.resolver.SafeResolver"

# Scrapy's default "Scrapy/x.y (+https://scrapy.org)" User-Agent is a
# canonical bot-block target -- live amazon.sa answers it with HTTP 500
# and jarir.com 403s the equally-default python-requests UA (verified
# 2026-07-11), so real fetches need a realistic browser UA. Also the UA
# `RobotsPolicyMiddleware` matches robots.txt rules against (both sites'
# robots.txt allow our product-page paths for this agent).
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en",
}

REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
# Scrapyd's daemon process installs the classic epoll reactor before
# spawning each crawl subprocess, and a Twisted reactor can never be
# swapped once installed. Scrapy's own default_settings.py requests the
# asyncio reactor unconditionally (even with no project-level override),
# which crashed every crawl with "The installed reactor ... does not
# match the requested one" before it could send a single request — so
# this must explicitly match Scrapyd's already-installed reactor, not
# simply omit the setting.
TWISTED_REACTOR = "twisted.internet.epollreactor.EPollReactor"
FEED_EXPORT_ENCODING = "utf-8"

ITEM_PIPELINES = {
    "scrape_core.pipelines.BatchedPersistencePipeline": 300,
}

# Pre-fetch scheme/userinfo SSRF guard (re-applied to every redirect hop
# since process_request runs for every request, including each one
# RedirectMiddleware re-emits) and per-request (never process-global)
# robots_policy enforcement (contracts/fetch-url-safety.md,
# contracts/robots-middleware.md). Ordered ahead of Scrapy's built-in
# RetryMiddleware/HttpCompressionMiddleware (default 550) so a rejection
# short-circuits before other request-processing runs.
#
# SPEC-10 US2 (contracts/spider-integration.md §5): Scrapy's built-in
# HttpProxyMiddleware reads `request.meta["proxy"]` (set by
# `generic_price_spider._request_for` for a proxied `AttemptPlan`) and
# routes the connection through it -- kept at Scrapy's own default
# priority (750) for this middleware, well after the SSRF guard/robots
# middlewares above so the *target* URL is still DNS-re-resolved
# (SafeResolver, below) and every redirect hop re-validated before a
# proxy is even considered; the SSRF guard's scheme/userinfo checks
# operate on the target URL, not the proxy, so their relative ordering
# versus HttpProxyMiddleware doesn't matter for FR-005 fetch-time safety.
DOWNLOADER_MIDDLEWARES = {
    "scrape_core.safety.middleware.SsrfGuardMiddleware": 100,
    "scrape_core.robots.RobotsPolicyMiddleware": 110,
    "scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware": 750,
}

# Small per-process pool through PgBouncer (contracts/reactor-safe-db.md) —
# a Scrapyd node runs many spiders per process; keep concurrency modest so
# the pool never needs to be large.
CONCURRENT_REQUESTS = 16
CONCURRENT_REQUESTS_PER_DOMAIN = 4
REACTOR_THREADPOOL_MAXSIZE = 20

# Batched-flush thresholds (contracts/persistence-pipeline.md) — read from
# `Settings`/config (env/DB-tunable), never hardcoded literals here.
_settings = get_settings()
SCRAPE_FLUSH_MAX_ITEMS = _settings.SCRAPE_FLUSH_MAX_ITEMS
SCRAPE_FLUSH_INTERVAL_SECONDS = _settings.SCRAPE_FLUSH_INTERVAL_SECONDS
