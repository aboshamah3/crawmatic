"""``generic_price_spider`` unit tests (SPEC-07 tasks.md T054, FR-006).

Exercises `GenericPriceSpider._request_for` -- the pure request-building
step `start()` delegates to -- directly, with a hand-built `SpiderTarget`.
No DB/Redis/reactor: `load_targets` (the DB-touching half of T054, which
now also reads `Competitor.robots_policy`) is intentionally not exercised
here (would need Postgres); this covers the half that's unit-testable
off-reactor -- that whatever `robots_policy` a target carries reaches
`request.meta["robots_policy"]`, which is what
`scrape_core.robots.RobotsPolicyMiddleware.process_request` actually
reads (defaulting to `RobotsPolicy.RESPECT` only when the key is
missing).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from scrapy.http import HtmlResponse, Request

from pathlib import Path

from app_shared.enums import ExtractionMethod, RobotsPolicy, StockStatus
from app_shared.models.scrape_profiles import ScrapeProfile
from scrape_core.errors import INVALID_PRICE_FORMAT, PRICE_NOT_FOUND

from price_monitor.spiders import generic_price_spider as gps

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "html"


@pytest.fixture()
def spider() -> gps.GenericPriceSpider:
    return gps.GenericPriceSpider(
        workspace_id=str(uuid.uuid4()),
        match_ids=str(uuid.uuid4()),
    )


def _target(robots_policy: RobotsPolicy) -> gps.SpiderTarget:
    return gps.SpiderTarget(
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        url="https://shop.example.com/product/1",
        profile=None,
        robots_policy=robots_policy,
    )


def _target_with_profile(profile: ScrapeProfile) -> gps.SpiderTarget:
    """A target carrying a resolved scrape profile (Task 3.1)."""
    return gps.SpiderTarget(
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        url="https://shop.example.com/product/1",
        profile=profile,
        robots_policy=RobotsPolicy.RESPECT,
    )


@pytest.mark.parametrize(
    "policy",
    [
        RobotsPolicy.RESPECT,
        RobotsPolicy.IGNORE_AFTER_APPROVAL,
        RobotsPolicy.REVIEW_REQUIRED,
    ],
)
def test_request_carries_resolved_robots_policy(
    spider: gps.GenericPriceSpider, policy: RobotsPolicy
) -> None:
    target = _target(policy)

    request = spider._request_for(target)

    assert request.meta["robots_policy"] == policy


def test_request_still_carries_match_id_and_download_slot(
    spider: gps.GenericPriceSpider,
) -> None:
    target = _target(RobotsPolicy.RESPECT)

    request = spider._request_for(target)

    assert request.meta["match_id"] == target.match_id
    assert request.meta["download_slot"] == str(target.match_id)
    assert request.url == target.url


# --- out-of-stock sniff on the failure path -----------------------------------
#
# PLAN_AMAZON_PRICE_FIX_2026-08-09 problem 4: probing all 21 amazon.sa
# matches stuck on PRICE_NOT_FOUND split them in two — 11 pages that DO
# carry a price (a deferred-buybox layout the profile's `price_regex`
# fallback now reaches) and 10 that genuinely have none, all of them
# marked `id="outOfStock"` + "غير متوفر حالياً". Only the second group is a
# state worth showing a user, so `parse` sniffs for it and lets it ride on
# the failure result.

# Trimmed verbatim from the captured `classB_dp_B001OBQ5K8.html` probe page.
_OUT_OF_STOCK_HTML = (
    '<html><body><div id="outOfStock" class="a-box"><div class="a-box-inner">'
    '<div class="a-section a-spacing-small a-text-center">'
    '<span class="a-color-base a-text-bold">غير متوفر حالياً.</span>'
    "<br/>لا نعرف متى أو فيما إذا كان هذا المنتج سيتوفر مرة أخرى"
    "</div></div></div></body></html>"
)

# A deferred-buybox page: no price text anywhere the extractors look, and
# no availability marker either — extraction simply missed.
_NO_PRICE_HTML = (
    '<html><body><div id="buybox"><div id="corePrice_feature_div"></div></div>'
    "</body></html>"
)


def _parse_once(spider: gps.GenericPriceSpider, target: gps.SpiderTarget, html: str) -> Any:
    """Drive `parse` over one hand-built response and return its single item.

    `parse` is an async generator; no reactor, DB or Redis is touched on
    this path (no semaphore/lock meta is stamped, so nothing is released).
    """
    spider._targets_by_match_id[target.match_id] = target
    request = Request(url=target.url, meta={"match_id": target.match_id})
    response = HtmlResponse(
        url=target.url, body=html.encode("utf-8"), encoding="utf-8", request=request
    )

    async def _collect() -> list[Any]:
        return [item async for item in spider.parse(response)]

    items = asyncio.run(_collect())
    assert len(items) == 1
    return items[0]


def test_price_not_found_on_an_out_of_stock_page_records_out_of_stock(
    spider: gps.GenericPriceSpider,
) -> None:
    result = _parse_once(spider, _target(RobotsPolicy.RESPECT), _OUT_OF_STOCK_HTML)

    assert result.success is False
    assert result.error_code == PRICE_NOT_FOUND
    assert result.price is None
    assert result.stock_status == StockStatus.OUT_OF_STOCK


def test_price_not_found_without_a_marker_leaves_stock_status_null(
    spider: gps.GenericPriceSpider,
) -> None:
    """The pre-2026-08-09 behaviour, deliberately unchanged: "extraction
    missed" must stay distinguishable from "the product is unavailable"."""
    result = _parse_once(spider, _target(RobotsPolicy.RESPECT), _NO_PRICE_HTML)

    assert result.success is False
    assert result.error_code == PRICE_NOT_FOUND
    assert result.stock_status is None


# --- EMBEDDED_JSON stock-only candidate, end to end through `parse` -----------
#
# Task 3.1 review fix round 2. `extract_embedded_json` emits a price-less
# OUT_OF_STOCK candidate for a page whose price pointer misses but whose
# availability pointer says the item is unavailable (the real noon shape:
# an empty `offers` array). Everything downstream of that was previously
# asserted in prose only -- `candidate_extras` was exercised by no test at
# all -- so this drives the whole spider path and checks the field the
# `pipelines` out-of-stock upsert actually keys on.


def _no_price_out_of_stock_profile() -> ScrapeProfile:
    """A real `ScrapeProfile`, not a duck-typed stub.

    Constructing the ORM model unattached (no session) exercises the
    actual `*_json_path` columns migration c7e1a9d34b60 added, so a
    column rename would break this test rather than sail past a stub.

    `jsonld_enabled=False` is required, not incidental: the fixture also
    carries a JSON-LD block advertising `"price": 0`, JSON-LD runs first
    in the chain, and `0` is a hit. Leaving it enabled would prove
    nothing about EMBEDDED_JSON.
    """
    return ScrapeProfile(
        name="embedded-json-no-price-fixture-profile",
        jsonld_enabled=False,
        price_json_path="/props/pageProps/product/offers/0/pricing/amount",
        stock_json_path="/props/pageProps/product/availability",
        title_json_path="/props/pageProps/product/title",
    )


def test_price_less_embedded_json_out_of_stock_reaches_the_result_as_out_of_stock(
    spider: gps.GenericPriceSpider,
) -> None:
    """The end-to-end claim behind the round-1 fix, driven not narrated.

    `parse` -> `extract` -> stock-only EMBEDDED_JSON candidate ->
    `validate_candidate` rejects the sentinel price -> the `Rejected`
    branch threads `candidate_extras` -> `result_builder` copies the
    candidate's stock onto the result.
    """
    html = (_FIXTURES_DIR / "embedded_json_next_data_no_price.html").read_text(
        encoding="utf-8"
    )
    target = _target_with_profile(_no_price_out_of_stock_profile())

    result = _parse_once(spider, target, html)

    # The field `pipelines.py`'s out-of-stock upsert branches on, together
    # with `success is False`. Both halves of that predicate are asserted.
    assert result.success is False
    assert result.stock_status == StockStatus.OUT_OF_STOCK
    assert result.price is None

    # `INVALID_PRICE_FORMAT`, NOT `PRICE_NOT_FOUND`, is what proves this
    # came through the `Rejected` branch: `_sniff_out_of_stock` is only
    # ever consulted on the `candidate is None` path, which emits
    # `PRICE_NOT_FOUND`.
    assert result.error_code == INVALID_PRICE_FORMAT
    # Only `candidate_extras` can put a method on the result.
    assert result.extraction_method == ExtractionMethod.EMBEDDED_JSON


def test_the_out_of_stock_sniff_could_not_have_produced_that_result() -> None:
    """Rules out the alternative explanation for the test above.

    If the fixture happened to contain one of `_sniff_out_of_stock`'s
    markers, the previous test would pass even with `candidate_extras`
    broken. It contains none, so the classification can only have come
    from the candidate.
    """
    html = (_FIXTURES_DIR / "embedded_json_next_data_no_price.html").read_text(
        encoding="utf-8"
    )

    assert gps._sniff_out_of_stock(html) is None


@pytest.mark.parametrize(
    "html",
    [
        '<div id="outOfStock" class="a-box"></div>',
        "<span>غير متوفر حالياً.</span>",
        "<p>Currently unavailable</p>",
        "<p>Out of stock</p>",
    ],
)
def test_sniff_recognises_each_marker(html: str) -> None:
    assert gps._sniff_out_of_stock(html) == StockStatus.OUT_OF_STOCK


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<html><body><span>7,990.00 ريال</span></body></html>",
        "<div id='inStock'>متوفر</div>",
    ],
)
def test_sniff_returns_none_when_nothing_says_unavailable(html: str) -> None:
    assert gps._sniff_out_of_stock(html) is None
