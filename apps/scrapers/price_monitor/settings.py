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

REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
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
DOWNLOADER_MIDDLEWARES = {
    "scrape_core.safety.middleware.SsrfGuardMiddleware": 100,
    "scrape_core.robots.RobotsPolicyMiddleware": 110,
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
