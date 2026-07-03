"""``ScrapeResult`` — the transport item flowing spider -> persistence pipeline.

Per ``data-model.md`` "Transport shapes": a single ``ScrapeResult``
carries the full ``price_observations`` field set + the full
``request_attempts`` field set + the correlation/scoping identifiers
(``workspace_id``/``match_id``/``product_id``/``product_variant_id``/
``competitor_id``/``scrape_job_id``), so
``scrape_core.pipelines.BatchedPersistencePipeline`` can turn one item
into one observation row + one request-attempt row (+ possibly one
``match_current_prices`` upsert) without a second lookup.

A plain ``dataclass`` rather than a ``scrapy.Item`` — pure stdlib, so it
stays importable/constructible without Scrapy installed (unit-testable
off-reactor); Scrapy's item pipeline machinery only needs duck-typed
attribute access, which a dataclass instance already provides.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app_shared.enums import AccessMethod, ExtractionMethod, ScrapeErrorCode, StockStatus

__all__ = ["ScrapeResult"]


@dataclass
class ScrapeResult:
    """One attempted target's full outcome: an observation + a request attempt.

    Required scoping/correlation fields have no default (a caller must
    always supply them); every field that maps to a nullable DB column
    defaults to ``None`` so a failure-path item can be constructed with
    only the fields it actually has.
    """

    # --- Scoping / correlation (never null) ---
    workspace_id: uuid.UUID
    match_id: uuid.UUID
    product_id: uuid.UUID
    product_variant_id: uuid.UUID
    competitor_id: uuid.UUID
    scrape_job_id: uuid.UUID | None

    # --- request_attempts field set ---
    url: str
    access_method: AccessMethod
    attempt_number: int = 1
    proxy_provider_id: uuid.UUID | None = None
    proxy_country: str | None = None
    status_code: int | None = None
    response_time_ms: int | None = None

    # --- price_observations field set ---
    scraped_at: datetime | None = None
    price: Decimal | None = None
    old_price: Decimal | None = None
    currency: str | None = None
    stock_status: StockStatus | None = None
    raw_title: str | None = None
    extraction_method: ExtractionMethod | None = None
    extraction_confidence: Decimal | None = None
    selector_used: str | None = None

    # --- shared outcome (both the observation and the attempt rows) ---
    success: bool = False
    comparable: bool = True
    error_code: ScrapeErrorCode | None = None
    error_message: str | None = None
