"""Same-URL fetch dedup + phantom-browser-step tests (2026-08-11 proxy-cost
Fixes 1 and 2a, PLAN_PROXY_COST_REDUCTION.md).

Covers the three pure seams off-reactor (no DB/Redis):

- `group_targets_for_dedup` / `_dedup_group_key` — what may and may not
  share a fetch;
- `parse` fan-out — one response producing one `ScrapeResult` per sibling
  with per-match identity but identical outcome fields, including the
  lock-mirroring rule;
- `errback`'s PLAYWRIGHT_PROXY conversion to STOP — the HTTP spider must
  never dispatch the terminal browser-fallback intent as a plain HTTP
  re-download.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from scrapy.http import HtmlResponse, Request

from app_shared.access.engine import AttemptPlan
from app_shared.enums import AccessMethod, RobotsPolicy, VariantStrategy
from scrape_core.limiter import LockGrant
from scrape_core.targets import group_targets_for_dedup

from price_monitor.spiders import generic_price_spider as gps


@pytest.fixture()
def spider() -> gps.GenericPriceSpider:
    return gps.GenericPriceSpider(
        workspace_id=str(uuid.uuid4()),
        match_ids=str(uuid.uuid4()),
    )


_URL = "https://shop.example.com/product/1"


def _profile(**overrides: Any) -> SimpleNamespace:
    """A duck-typed resolved profile row -- only the fields the dedup key
    and `parse` read (`variant_strategy`/`id`, `validation_rules`/
    `confidence_rules` for the parse path)."""
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "variant_strategy": VariantStrategy.PAGE_SINGLE_PRICE,
        "validation_rules": None,
        "confidence_rules": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _target(
    *,
    url: str = _URL,
    competitor_id: uuid.UUID | None = None,
    profile: Any = None,
    access_policy: Any = None,
    variant_selector_config: dict | None = None,
) -> gps.SpiderTarget:
    return gps.SpiderTarget(
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=competitor_id or uuid.uuid4(),
        url=url,
        profile=profile,
        robots_policy=RobotsPolicy.RESPECT,
        access_policy=access_policy,
        variant_selector_config=variant_selector_config,
    )


# --- grouping ---------------------------------------------------------------


def test_same_fetch_targets_fold_onto_one_fetcher() -> None:
    competitor = uuid.uuid4()
    profile = _profile()
    a = _target(competitor_id=competitor, profile=profile)
    b = _target(competitor_id=competitor, profile=profile)
    c = _target(competitor_id=competitor, profile=profile)

    fetchers = group_targets_for_dedup([a, b, c])

    assert fetchers == [a]
    assert a.sibling_targets == [b, c]


def test_different_url_competitor_profile_or_policy_never_group() -> None:
    competitor = uuid.uuid4()
    profile = _profile()
    base = _target(competitor_id=competitor, profile=profile)
    other_url = _target(competitor_id=competitor, profile=profile, url=_URL + "?v=2")
    other_competitor = _target(profile=profile)
    other_profile = _target(competitor_id=competitor, profile=_profile())
    other_policy = _target(
        competitor_id=competitor, profile=profile, access_policy=SimpleNamespace(id=uuid.uuid4())
    )

    fetchers = group_targets_for_dedup(
        [base, other_url, other_competitor, other_profile, other_policy]
    )

    assert len(fetchers) == 5
    assert all(not t.sibling_targets for t in fetchers)


def test_variant_aware_targets_are_never_grouped() -> None:
    competitor = uuid.uuid4()
    variant_profile = _profile(variant_strategy=VariantStrategy.HTML_VARIANT_TABLE)
    a = _target(competitor_id=competitor, profile=variant_profile)
    b = _target(competitor_id=competitor, profile=variant_profile)
    config_profile = _profile()
    c = _target(
        competitor_id=competitor, profile=config_profile, variant_selector_config={"actions": []}
    )
    d = _target(
        competitor_id=competitor, profile=config_profile, variant_selector_config={"actions": []}
    )

    fetchers = group_targets_for_dedup([a, b, c, d])

    assert len(fetchers) == 4
    assert all(not t.sibling_targets for t in fetchers)


# --- parse fan-out ----------------------------------------------------------

_PRICED_HTML = (
    '<html><body><script type="application/ld+json">'
    '{"@type": "Product", "name": "Widget", "offers": '
    '{"@type": "Offer", "price": "199.00", "priceCurrency": "SAR"}}'
    "</script></body></html>"
)


def _run_parse(
    spider: gps.GenericPriceSpider, fetcher: gps.SpiderTarget, meta_extra: dict | None = None
) -> list[Any]:
    spider._targets_by_match_id[fetcher.match_id] = fetcher
    for sibling in fetcher.sibling_targets:
        spider._targets_by_match_id[sibling.match_id] = sibling
    meta = {"match_id": fetcher.match_id, **(meta_extra or {})}
    request = Request(url=fetcher.url, meta=meta)
    response = HtmlResponse(
        url=fetcher.url, body=_PRICED_HTML.encode("utf-8"), encoding="utf-8", request=request
    )

    async def _collect() -> list[Any]:
        return [item async for item in spider.parse(response)]

    return asyncio.run(_collect())


def test_parse_fans_one_response_out_to_every_sibling(spider: gps.GenericPriceSpider) -> None:
    competitor = uuid.uuid4()
    profile = _profile()
    fetcher = _target(competitor_id=competitor, profile=profile)
    sib1 = _target(competitor_id=competitor, profile=profile)
    sib2 = _target(competitor_id=competitor, profile=profile)
    assert group_targets_for_dedup([fetcher, sib1, sib2]) == [fetcher]

    items = _run_parse(spider, fetcher)

    assert len(items) == 3
    by_match = {item.match_id: item for item in items}
    assert set(by_match) == {fetcher.match_id, sib1.match_id, sib2.match_id}
    for source in (fetcher, sib1, sib2):
        item = by_match[source.match_id]
        assert item.success is True
        assert str(item.price) == "199.00"
        assert item.product_id == source.product_id
        assert item.product_variant_id == source.product_variant_id
        assert item.competitor_id == source.competitor_id


def test_sibling_rows_mirror_the_fetcher_lock_rule(spider: gps.GenericPriceSpider) -> None:
    competitor = uuid.uuid4()
    profile = _profile()
    fetcher = _target(competitor_id=competitor, profile=profile)
    sibling = _target(competitor_id=competitor, profile=profile)
    group_targets_for_dedup([fetcher, sibling])
    sib_lock = LockGrant(key=f"lock:scrape:x:{sibling.match_id}", token="tok-sib")
    spider._sibling_locks[sibling.match_id] = sib_lock

    items = _run_parse(
        spider,
        fetcher,
        meta_extra={"match_lock_key": "lock:scrape:x:fetcher", "match_lock_token": "tok-f"},
    )

    by_match = {item.match_id: item for item in items}
    assert by_match[fetcher.match_id].match_lock_key == "lock:scrape:x:fetcher"
    assert by_match[sibling.match_id].match_lock_key == sib_lock.key
    assert by_match[sibling.match_id].match_lock_token == sib_lock.token
    # carried onto a terminal row => consumed
    assert sibling.match_id not in spider._sibling_locks


def test_sibling_rows_withhold_their_locks_when_the_fetcher_row_does(
    spider: gps.GenericPriceSpider,
) -> None:
    competitor = uuid.uuid4()
    profile = _profile()
    fetcher = _target(competitor_id=competitor, profile=profile)
    sibling = _target(competitor_id=competitor, profile=profile)
    group_targets_for_dedup([fetcher, sibling])
    sib_lock = LockGrant(key=f"lock:scrape:x:{sibling.match_id}", token="tok-sib")
    spider._sibling_locks[sibling.match_id] = sib_lock

    # no lock meta on the fetcher's request => the fetcher row carries no
    # lock (the retry-pending shape) => sibling rows must not either, and
    # the sibling lock must survive for the retry's fan-out.
    items = _run_parse(spider, fetcher)

    by_match = {item.match_id: item for item in items}
    assert by_match[sibling.match_id].match_lock_key is None
    assert spider._sibling_locks[sibling.match_id] is sib_lock


# --- errback: PLAYWRIGHT_PROXY intent must never dispatch here --------------


def test_errback_stops_instead_of_dispatching_a_phantom_browser_attempt(
    spider: gps.GenericPriceSpider, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(profile=_profile())
    spider._targets_by_match_id[target.match_id] = target

    def _playwright_decision(*args: Any, **kwargs: Any) -> gps._DispatchDecision:
        return gps._DispatchDecision(
            plan=AttemptPlan(access_method=AccessMethod.PLAYWRIGHT_PROXY, use_proxy=True),
            proxy=None,
        )

    monkeypatch.setattr(gps, "_prepare_dispatch", _playwright_decision)

    # `await_in_thread` rides Twisted's `deferToThread`, which needs a
    # running reactor -- under plain `asyncio.run` it would hang. The
    # seam is irrelevant to what this test asserts, so call through
    # inline.
    async def _inline(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    monkeypatch.setattr(gps, "await_in_thread", _inline)

    async def _fail_if_dispatched(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("phantom PLAYWRIGHT_PROXY attempt was dispatched by the HTTP spider")

    monkeypatch.setattr(spider, "_dispatch", _fail_if_dispatched)

    request = Request(
        url=target.url,
        meta={
            "match_id": target.match_id,
            "attempt_number": 3,
            "access_method": AccessMethod.PROXY_HTTP,
            "match_lock_key": "lock:scrape:x:f",
            "match_lock_token": "tok",
        },
    )
    failure = SimpleNamespace(request=request, value=TimeoutError("fetch timed out"))

    async def _collect() -> list[Any]:
        return [item async for item in spider.errback(failure)]

    items = asyncio.run(_collect())

    # exactly the failed attempt's own terminal row -- no retry, no
    # never-dispatched skip row -- and it releases the held lock.
    assert len(items) == 1
    assert items[0].success is False
    assert items[0].attempt_number == 3
    assert items[0].match_lock_key == "lock:scrape:x:f"
