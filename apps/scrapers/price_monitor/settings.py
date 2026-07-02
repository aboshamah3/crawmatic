"""Default Scrapy settings for the `price_monitor` (Scrapyd HTTP) project.

No spiders or scraping/extraction logic exist in this skeleton phase
(SPEC-01) — see spec Scope Boundary. This module establishes Scrapy
defaults only and imports `scrape_core` to prove the shared-library
dependency boundary (FR-004): this Scrapyd node may depend on
`scrape_core`, never the reverse.
"""

import scrape_core  # noqa: F401  # proves libs/scrape-core is importable here

BOT_NAME = "price_monitor"

SPIDER_MODULES = ["price_monitor.spiders"]
NEWSPIDER_MODULE = "price_monitor.spiders"

ROBOTSTXT_OBEY = True

REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"
