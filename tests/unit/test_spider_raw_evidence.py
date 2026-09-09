"""The last link of C5's evidence chain: a spider actually hands the page
bytes over, so `offer_raw_evidence_hash` is populated END TO END (EPA C5
continuation, F19).

`tests/unit/test_offer_propagation.py` proves the persistence half — given
an item carrying `raw_evidence`, `_flush_batch` stores the bytes and keeps
a hash that resolves. That test could pass forever while the live path
stayed NULL, because nothing produced the bytes. These tests close that:

1. `GenericPriceSpider.parse` puts `response.body` on EVERY result it
   emits — success and failure alike.
2. Those exact bytes, run through the real `_flush_batch`, come back out
   of `resolve_hash`. This is the assertion that would have caught a
   spider-to-pipeline mismatch (a decoded string instead of bytes, say),
   which no test on either side alone can see.
3. The durable result spool tolerates the new field instead of raising
   `TypeError` on every scrape — the failure this wiring would otherwise
   have caused the moment a producer set it.

Both spiders' `parse` are async generators driven over a hand-built
response; no reactor, no Chromium, no DB, no Redis (no semaphore/lock
meta is stamped, so nothing is released).
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from scrapy.http import HtmlResponse, Request

from app_shared.enums import RobotsPolicy
from app_shared.observations.evidence_store import compute_hash, resolve_hash
from scrape_core import pipelines as pipelines_mod
from scrape_core.items import ScrapeResult
from scrape_core.result_spool import ResultSpool

from price_monitor.spiders import generic_price_spider as gps

#: A page with a plain, extractable price — the SUCCESS path, so the
#: assertions below are not accidentally only about failure rows.
PRICED_HTML = (
    '<html><head><script type="application/ld+json">'
    '{"@type": "Product", "offers": {"@type": "Offer", "price": "99.00", '
    '"priceCurrency": "SAR"}}'
    "</script></head><body></body></html>"
)

#: A page with no price anywhere — the FAILURE path. An evidence hash
#: that only appeared on successes would be missing exactly where a human
#: most wants to replay the page.
NO_PRICE_HTML = '<html><body><div id="buybox"></div></body></html>'


@pytest.fixture()
def spider() -> gps.GenericPriceSpider:
    return gps.GenericPriceSpider(
        workspace_id=str(uuid.uuid4()),
        match_ids=str(uuid.uuid4()),
    )


def _target() -> gps.SpiderTarget:
    return gps.SpiderTarget(
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        url="https://shop.example.com/product/1",
        profile=None,
        robots_policy=RobotsPolicy.RESPECT,
    )


def _parse_once(
    spider: gps.GenericPriceSpider, target: gps.SpiderTarget, html: str
) -> Any:
    spider._targets_by_match_id[target.match_id] = target
    request = Request(url=target.url, meta={"match_id": target.match_id})
    response = HtmlResponse(
        url=target.url, body=html.encode("utf-8"), encoding="utf-8", request=request
    )

    async def _collect() -> list[Any]:
        return [item async for item in spider.parse(response)]

    items = asyncio.run(_collect())
    assert len(items) == 1
    return items[0], response.body


# --------------------------------------------------------------------------
# 1. The spider hands the bytes over
# --------------------------------------------------------------------------


def test_a_successful_parse_carries_the_response_body(
    spider: gps.GenericPriceSpider,
) -> None:
    result, body = _parse_once(spider, _target(), PRICED_HTML)

    assert result.success is True
    assert result.raw_evidence == body
    # Bytes, never the decoded string: a content address must name what
    # actually arrived on the wire, and a decoded-then-re-encoded page is
    # a different byte sequence.
    assert isinstance(result.raw_evidence, bytes)


def test_a_failed_parse_carries_the_response_body_too(
    spider: gps.GenericPriceSpider,
) -> None:
    result, body = _parse_once(spider, _target(), NO_PRICE_HTML)

    assert result.success is False
    assert result.raw_evidence == body


def test_the_bytes_are_the_response_object_s_own_not_a_copy(
    spider: gps.GenericPriceSpider,
) -> None:
    """Siblings riding one fetch share one `bytes` object, so a fan-out
    costs one page of memory, not N."""
    result, body = _parse_once(spider, _target(), PRICED_HTML)
    assert result.raw_evidence is body


# --------------------------------------------------------------------------
# 2. End to end: those bytes become a hash that resolves
# --------------------------------------------------------------------------


class _FakeResult:
    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return []

    def first(self) -> None:
        return None

    def scalar_one_or_none(self) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add_all(self, items: Any) -> None:
        self.added.extend(items)

    def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        return _FakeResult()


class _FakeWorkspaceTxn:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self, workspace_id: Any) -> "_FakeWorkspaceTxn":
        return self

    def __enter__(self) -> _FakeSession:
        return self._session

    def __exit__(self, *exc_info: Any) -> bool:
        return False


class _FakeSettings:
    PRICE_ANALYSIS_DEDUP_TTL_SECONDS = 21600
    STRATEGY_STATS_KEY_TTL_SECONDS = 3600
    STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD = 0.85

    def __init__(self, evidence_store_dir: str | None) -> None:
        self.EVIDENCE_STORE_DIR = evidence_store_dir


class _FakeRedis:
    def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        return True


def _install_fakes(monkeypatch: Any, store_dir: str | None) -> _FakeSession:
    session = _FakeSession()
    monkeypatch.setattr(pipelines_mod, "workspace_txn", _FakeWorkspaceTxn(session))
    monkeypatch.setattr(pipelines_mod, "write_outbox_message", lambda *a, **k: None)
    monkeypatch.setattr(pipelines_mod, "get_settings", lambda: _FakeSettings(store_dir))
    monkeypatch.setattr(pipelines_mod, "get_redis_client", lambda: _FakeRedis())
    monkeypatch.setattr(
        pipelines_mod,
        "_insert_ignoring_replays",
        lambda _session, _model, instances, _keys: session.added.extend(instances),
    )
    return session


def test_the_page_a_spider_read_is_replayable_from_the_persisted_row(
    spider: gps.GenericPriceSpider, monkeypatch: Any, tmp_path: Path
) -> None:
    """The whole point of F19's evidence half, in one assertion."""
    result, body = _parse_once(spider, _target(), PRICED_HTML)
    # The pipeline is workspace-scoped; the spider's own workspace id is
    # a string on the spider, so use the item's.
    session = _install_fakes(monkeypatch, str(tmp_path))

    pipelines_mod._flush_batch(result.workspace_id, [result])

    observation = [o for o in session.added if type(o).__name__ == "PriceObservation"][0]
    assert observation.offer_raw_evidence_hash == compute_hash(body)
    assert resolve_hash(observation.offer_raw_evidence_hash, store_dir=tmp_path) == body


def test_without_a_configured_store_the_spider_s_bytes_produce_no_hash(
    spider: gps.GenericPriceSpider, monkeypatch: Any, tmp_path: Path
) -> None:
    """An unmounted volume leaves NULL, never a dangling content address."""
    result, _body = _parse_once(spider, _target(), PRICED_HTML)
    session = _install_fakes(monkeypatch, None)

    pipelines_mod._flush_batch(result.workspace_id, [result])

    observation = [o for o in session.added if type(o).__name__ == "PriceObservation"][0]
    assert observation.offer_raw_evidence_hash is None
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# 3. The durable spool survives the new field
# --------------------------------------------------------------------------


def _spoolable_result(**overrides: Any) -> ScrapeResult:
    from app_shared.enums import AccessMethod

    payload: dict[str, Any] = {
        "workspace_id": uuid.uuid4(),
        "match_id": uuid.uuid4(),
        "product_id": uuid.uuid4(),
        "product_variant_id": uuid.uuid4(),
        "competitor_id": uuid.uuid4(),
        "scrape_job_id": None,
        "url": "https://shop.example.com/product/1",
        "access_method": AccessMethod.DIRECT_HTTP,
        "success": True,
        "price": Decimal("99.00"),
    }
    payload.update(overrides)
    return ScrapeResult(**payload)


def test_spooling_a_result_with_raw_evidence_does_not_raise(tmp_path: Path) -> None:
    """Before this wiring the spool raised `TypeError: result spool cannot
    serialize <class 'bytes'>` — on EVERY item, because every item is
    spooled before it is buffered."""
    spool = ResultSpool(tmp_path / "spool.sqlite")

    ids = spool.append_batch([_spoolable_result(raw_evidence=b"<html></html>")])

    assert len(ids) == 1


def test_a_replayed_result_keeps_its_price_and_drops_its_evidence(
    tmp_path: Path,
) -> None:
    """The spool makes the OBSERVATION durable, not the page.

    A replay only happens when the flush that would have STORED the blob
    failed, so the bytes would have nothing to point at anyway — NULL is
    the honest value, and it costs a third of a local SQLite file per
    page not to pretend otherwise.
    """
    spool = ResultSpool(tmp_path / "spool.sqlite")
    ids = spool.append_batch([_spoolable_result(raw_evidence=b"<html></html>")])

    (restored,) = spool.load(ids)

    assert restored.result.price == Decimal("99.00")
    assert restored.result.raw_evidence is None
    assert restored.result.offer is None


# --------------------------------------------------------------------------
# 4. The browser spider does the same, and its bytes are the RENDERED DOM
# --------------------------------------------------------------------------


def _browser_parse_once(html: str) -> Any:
    from scrape_core.targets import SpiderTarget

    from price_monitor_browser.spiders import generic_browser_price_spider as gbps

    spider = gbps.GenericBrowserPriceSpider(
        workspace_id=str(uuid.uuid4()), match_ids=str(uuid.uuid4())
    )
    target = SpiderTarget(
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        url="https://shop.example.com/product/1",
        profile=None,
        robots_policy=RobotsPolicy.RESPECT,
    )
    spider._targets_by_match_id[target.match_id] = target
    request = Request(url=target.url, meta={"match_id": target.match_id})
    response = HtmlResponse(
        url=target.url, body=html.encode("utf-8"), encoding="utf-8", request=request
    )

    async def _collect() -> list[Any]:
        return [item async for item in spider.parse(response)]

    items = asyncio.run(_collect())
    assert len(items) == 1
    return items[0], response.body


def test_the_browser_spider_carries_the_rendered_dom(monkeypatch: Any, tmp_path: Path) -> None:
    """On this spider `response.body` is scrapy-playwright's POST-JS DOM,
    which is the right evidence precisely because it is what the extractor
    read — the pre-render HTML would replay to a different page."""
    result, body = _browser_parse_once(PRICED_HTML)

    assert result.success is True
    assert result.raw_evidence is body

    session = _install_fakes(monkeypatch, str(tmp_path))
    pipelines_mod._flush_batch(result.workspace_id, [result])

    observation = [o for o in session.added if type(o).__name__ == "PriceObservation"][0]
    assert resolve_hash(observation.offer_raw_evidence_hash, store_dir=tmp_path) == body


def test_the_browser_spider_carries_it_on_a_failure_too() -> None:
    result, body = _browser_parse_once(NO_PRICE_HTML)

    assert result.success is False
    assert result.raw_evidence == body
