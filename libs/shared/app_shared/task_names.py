"""Celery task-name string constants.

Task names live here as plain strings so any process — a Scrapyd
spider, the scheduler, the API — can enqueue work via Celery's
``send_task(name, ...)`` without importing ``apps/workers`` itself.
That indirection is the dependency boundary that keeps
scrapy/twisted/playwright out of the worker/API import closures
(Constitution V: Disciplined Scraping Runtime).

No task names are defined yet in this skeleton phase. Later specs add
constants here, e.g.:

    # SCRAPE_PRODUCT = "workers.scrape_product"
    # MATCH_PRODUCTS = "workers.match_products"

This module intentionally imports nothing from ``celery``.
"""
