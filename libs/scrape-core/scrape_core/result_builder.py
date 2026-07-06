"""Free-function ``ScrapeResult`` construction (SPEC-14 T007,
`contracts/shared-extraction.md`), extracted out of
``apps/scrapers/price_monitor/spiders/generic_price_spider.py``'s
``GenericPriceSpider._build_result`` so both Scrapy projects build the
identical result shape (Constitution Principle I).

Import policy (shared-extraction.md): only ``app_shared.*`` +
``scrape_core.*`` — never ``apps.*``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from app_shared.enums import AccessMethod

from scrape_core.items import ScrapeResult

__all__ = ["build_scrape_result"]


def build_scrape_result(
    target: Any,
    url: str,
    scraped_at: datetime,
    *,
    workspace_id: uuid.UUID,
    scrape_job_id: uuid.UUID | None,
    status_code: int | None,
    success: bool,
    access_method: AccessMethod = AccessMethod.DIRECT_HTTP,
    attempt_number: int = 1,
    proxy_provider_id: uuid.UUID | None = None,
    proxy_country: str | None = None,
    response_time_ms: int | None = None,
    error_code: Any = None,
    error_message: str | None = None,
    comparable: bool = True,
    price: Decimal | None = None,
    candidate_extras: Any = None,
    match_lock_key: str | None = None,
    match_lock_token: str | None = None,
) -> ScrapeResult:
    """Build one attempt's `ScrapeResult` (SPEC-10 US3, T034; extracted SPEC-14 T007).

    ``target`` is any object exposing ``match_id``/``product_id``/
    ``product_variant_id``/``competitor_id``/``domain_strategy_profile_id``
    -- in practice a :class:`scrape_core.targets.SpiderTarget`, but this
    module deliberately takes it duck-typed rather than importing
    ``scrape_core.targets`` itself, avoiding any import-order coupling
    between the two extracted modules.

    ``workspace_id``/``scrape_job_id`` are explicit parameters (this was
    a bound spider method reading ``self.workspace_id``/
    ``self.scrape_job_id`` before the SPEC-14 extraction) — every caller
    (a spider's own thin ``_build_result`` wrapper, or the shared
    admission machinery in ``scrape_core.targets``) supplies its own.

    ``access_method``/``attempt_number``/``proxy_provider_id``/
    ``proxy_country``/``response_time_ms`` default to a plain
    ``DIRECT_HTTP`` first attempt (pre-SPEC-10 callers, and unit
    tests, may call this with only the required kwargs) but every
    SPEC-10 call site now passes the **actual** attempt's values --
    see `_attempt_kwargs_from_meta` (dispatched attempts, `parse`/
    `errback`) and `_DispatchDecision.attempted_method`/
    `attempted_proxy` (never-dispatched rate/proxy/budget skips,
    `start`/`errback`) -- never the previously hardcoded
    `DIRECT_HTTP`. One `ScrapeResult` is emitted per attempt
    (including retries), so the unchanged `BatchedPersistencePipeline`
    writes one `RequestAttempt` row per attempt (FR-012/FR-013/FR-015).

    ``match_lock_key``/``match_lock_token`` (SPEC-11 US2, T020/T022)
    default to ``None`` -- a never-dispatched attempt (rate/proxy/
    budget skip, or a SPEC-11 match-lock collision) never acquired a
    lock, so there is nothing to release. A dispatched attempt passes
    them via `_attempt_kwargs_from_meta` (read back from
    `request.meta`/`response.meta`, stamped by `_request_for`).

    ``domain_strategy_profile_id`` (SPEC-12 US2, T022, contracts/
    consumption.md step 4) is read straight off `target` -- every
    target this spider builds carries the profile id its
    `(competitor_id, url_pattern)` group resolved in `load_targets`
    (`None` only for a hand-built pre-SPEC-12 target, e.g. a unit
    test) -- so every `ScrapeResult` this function emits for a real
    dispatched target threads it through without a second query,
    ready for US5's off-reactor stats recorder.
    """
    kwargs: dict[str, Any] = {}
    if candidate_extras is not None:
        kwargs.update(
            currency=candidate_extras.currency,
            stock_status=candidate_extras.stock,
            raw_title=candidate_extras.raw_title,
            extraction_method=candidate_extras.method,
            extraction_confidence=Decimal(str(candidate_extras.confidence)),
            selector_used=candidate_extras.selector_used,
        )
    return ScrapeResult(
        workspace_id=workspace_id,
        match_id=target.match_id,
        product_id=target.product_id,
        product_variant_id=target.product_variant_id,
        competitor_id=target.competitor_id,
        scrape_job_id=scrape_job_id,
        url=url,
        access_method=access_method,
        attempt_number=attempt_number,
        proxy_provider_id=proxy_provider_id,
        proxy_country=proxy_country,
        status_code=status_code,
        response_time_ms=response_time_ms,
        scraped_at=scraped_at,
        price=price,
        success=success,
        comparable=comparable,
        error_code=error_code,
        error_message=error_message,
        match_lock_key=match_lock_key,
        match_lock_token=match_lock_token,
        domain_strategy_profile_id=target.domain_strategy_profile_id,
        **kwargs,
    )
