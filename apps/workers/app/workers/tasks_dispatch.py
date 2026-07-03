"""Thin Celery task that dispatches ``generic_price_spider`` to Scrapyd.

This is a *thin* wrapper: it delegates all auth + idempotency to
``app_shared.scrapyd.ScrapydDispatchClient``. Full scheduler/orchestration
(job batching, target-state) is a later spec — US4 only needs the authenticated,
idempotent single-batch dispatch.
"""

from __future__ import annotations

from app.workers.celery_app import app
from app_shared.scrapyd import ScrapydDispatchClient

# The Scrapy project + spider deployed to the Scrapyd HTTP node (apps/scrapers).
_SCRAPYD_PROJECT = "price_monitor"
_GENERIC_PRICE_SPIDER = "generic_price_spider"


@app.task(name="dispatch.generic_price_spider")
def dispatch_generic_price_spider(
    workspace_id: str,
    scrape_job_id: str,
    match_ids: object,
    mode: str,
    batch_index: int,
) -> str:
    """Schedule one batch of ``generic_price_spider`` and return the jobid.

    Idempotent per ``(scrape_job_id, batch_index)``: an at-least-once Celery
    retry returns the already-persisted jobid without re-scheduling the batch.
    """
    client = ScrapydDispatchClient()
    return client.schedule(
        _SCRAPYD_PROJECT,
        _GENERIC_PRICE_SPIDER,
        workspace_id=workspace_id,
        scrape_job_id=scrape_job_id,
        match_ids=match_ids,
        mode=mode,
        batch_index=batch_index,
    )
