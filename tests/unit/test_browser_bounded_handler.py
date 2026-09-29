"""The browser download handler cannot wedge (2026-09-29, plan E1.1/E1.2).

These drive the REAL scrapy-playwright download path (``_download_request``
-> ``_create_page`` -> ``_create_browser_context`` -> ``goto`` -> page
methods -> ``page.content`` -> ``page.close``) against in-memory fakes of
Playwright's Browser/BrowserContext/Page, so each test reproduces the exact
await that hangs in production without a real Chromium.

The fakes implement only what the pinned scrapy-playwright 0.0.47 path
touches; a version bump that touches more fails these tests loudly, which is
the point -- the handler subclass overrides private methods of that version.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from scrapy import Request, Spider
from scrapy.settings import Settings
from scrapy_playwright.handler import Config, ScrapyPlaywrightDownloadHandler
from scrapy_playwright.page import PageMethod

from price_monitor_browser.handler import (
    BoundedPlaywrightDownloadHandler,
    BrowserDownloadTimeoutError,
)
from scrape_core.errors import ScrapeErrorCode, classify_playwright_exception

HANG = "hang"


class _Stats:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def inc_value(self, key: str, count: int = 1, start: int = 0) -> None:
        self.values[key] = self.values.get(key, start) + count

    def set_value(self, key: str, value: Any) -> None:
        self.values[key] = value

    def get_value(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class FakePage:
    def __init__(self, context: FakeContext, behaviour: dict[str, str]) -> None:
        self.context = context
        self.behaviour = behaviour
        self.url = "https://shop.example/p/1"
        self._closed = False
        self._handlers: dict[str, list[Any]] = {}

    def on(self, event: str, cb: Any) -> None:
        self._handlers.setdefault(event, []).append(cb)

    def remove_listener(self, event: str, cb: Any) -> None:
        if cb in self._handlers.get(event, []):
            self._handlers[event].remove(cb)

    def set_default_navigation_timeout(self, timeout: float) -> None:
        pass

    async def unroute(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def route(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def _maybe_hang(self, step: str) -> None:
        if self.behaviour.get(step) == HANG:
            await asyncio.get_running_loop().create_future()  # never resolves

    async def goto(self, url: str, **kwargs: Any) -> None:
        await self._maybe_hang("goto")
        return None

    async def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def content(self) -> str:
        await self._maybe_hang("content")
        return "<html><body>ok</body></html>"

    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        await self._maybe_hang("close")
        if self._closed:
            return
        self._closed = True
        self.context.pages.remove(self)
        for cb in self._handlers.get("close", []):
            cb()


class FakeContext:
    def __init__(self, browser: FakeBrowser, kwargs: dict[str, Any]) -> None:
        self.browser = browser
        self.kwargs = kwargs
        self.pages: list[FakePage] = []
        self.closed = False
        self._handlers: dict[str, list[Any]] = {}

    def on(self, event: str, cb: Any) -> None:
        self._handlers.setdefault(event, []).append(cb)

    def set_default_navigation_timeout(self, timeout: float) -> None:
        pass

    async def new_page(self) -> FakePage:
        page = FakePage(self, self.browser.page_behaviour)
        self.pages.append(page)
        return page

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for page in list(self.pages):
            await page.close()
        for cb in self._handlers.get("close", []):
            cb()


class FakeBrowser:
    def __init__(self, page_behaviour: dict[str, str] | None = None) -> None:
        self.page_behaviour = page_behaviour or {}
        self.contexts: list[FakeContext] = []

    async def new_context(self, **kwargs: Any) -> FakeContext:
        ctx = FakeContext(self, kwargs)
        self.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        await asyncio.get_running_loop().create_future()  # a hung browser.close()


class _BrowserType:
    name = "chromium"


def build_handler(
    cls: type[ScrapyPlaywrightDownloadHandler],
    browser: FakeBrowser,
    **overrides: Any,
) -> Any:
    """Assemble a handler the way __init__ would, minus the reactor check."""
    settings = Settings(
        {
            "CONCURRENT_REQUESTS": 2,
            "PLAYWRIGHT_MAX_CONTEXTS": 1,
            "DOWNLOAD_TIMEOUT": 0.2,
            "BROWSER_DOWNLOAD_HARD_MARGIN_SECONDS": 0.2,
            "BROWSER_HANDLER_CLOSE_TIMEOUT_SECONDS": 0.3,
            "BROWSER_PAGE_CLOSE_TIMEOUT_SECONDS": 0.1,
            "BROWSER_IDLE_CONTEXT_POLL_SECONDS": 0.01,
            **overrides,
        }
    )
    handler = cls.__new__(cls)
    handler.stats = _Stats()
    handler.config = Config.from_settings(settings)
    handler.browser_launch_lock = asyncio.Lock()
    handler.context_launch_lock = asyncio.Lock()
    handler.context_wrappers = {}
    if handler.config.max_contexts:
        handler.context_semaphore = asyncio.Semaphore(value=handler.config.max_contexts)
    handler.process_request_headers = None
    handler.abort_request = None
    handler.browser = browser
    handler.browser_type = _BrowserType()
    if hasattr(handler, "_configure_bounds"):
        handler._configure_bounds(settings)
    return handler


def _request(url: str = "https://shop.example/p/1", **meta: Any) -> Request:
    base = {"playwright": True, "playwright_page_goto_kwargs": {"timeout": 100}}
    base.update(meta)
    return Request(url, meta=base)


class _Spider(Spider):
    name = "generic_browser_price_spider"


def run(coro: Any, *, within: float) -> Any:
    """Run `coro` and fail (not hang) if it outlives `within` seconds."""

    async def _bounded() -> Any:
        return await asyncio.wait_for(coro, timeout=within)

    return asyncio.run(_bounded())


# ── E1.1: every download is bounded ─────────────────────────────────────────


@pytest.mark.parametrize("step", ["content", "goto"])
def test_a_never_resolving_await_fails_the_request_as_timeout(step: str) -> None:
    browser = FakeBrowser({step: HANG})
    handler = build_handler(BoundedPlaywrightDownloadHandler, browser)

    with pytest.raises(BrowserDownloadTimeoutError) as info:
        run(handler._download_request(_request(), _Spider()), within=3)

    assert classify_playwright_exception(info.value) is ScrapeErrorCode.TIMEOUT
    # The abandoned page was closed, so its per-context page slot is free.
    assert all(page.is_closed() for ctx in browser.contexts for page in ctx.pages)
    assert browser.contexts[0].pages == []


def test_a_timed_out_download_whose_page_close_also_hangs_still_returns() -> None:
    browser = FakeBrowser({"content": HANG, "close": HANG})
    handler = build_handler(BoundedPlaywrightDownloadHandler, browser)

    with pytest.raises(BrowserDownloadTimeoutError):
        run(handler._download_request(_request(), _Spider()), within=3)


def test_the_unbounded_parent_handler_hangs_on_the_same_page() -> None:
    """Reproduces the production wedge on the stock handler: the same
    never-resolving page.content() holds the download forever."""
    browser = FakeBrowser({"content": HANG})
    handler = build_handler(ScrapyPlaywrightDownloadHandler, browser)

    with pytest.raises(asyncio.TimeoutError):
        run(handler._download_request(_request(), _Spider()), within=1)


def test_a_healthy_download_is_untouched() -> None:
    browser = FakeBrowser()
    handler = build_handler(BoundedPlaywrightDownloadHandler, browser)

    response = run(handler._download_request(_request(), _Spider()), within=3)

    assert b"ok" in response.body
    assert handler._pages_by_request == {}


def test_the_bound_is_the_requests_declared_timeouts_plus_margin() -> None:
    handler = build_handler(
        BoundedPlaywrightDownloadHandler,
        FakeBrowser(),
        DOWNLOAD_TIMEOUT=60,
        BROWSER_DOWNLOAD_HARD_MARGIN_SECONDS=30,
    )
    request = _request(
        playwright_page_goto_kwargs={"timeout": 30_000},
        playwright_page_methods=[PageMethod("wait_for_selector", "#price", timeout=45_000)],
    )
    # 30 s goto + 45 s wait = 75 s declared, above the 60 s floor, + 30 s margin.
    assert handler.hard_timeout_for(request) == pytest.approx(105)
    # A request declaring less than DOWNLOAD_TIMEOUT gets the floor.
    assert handler.hard_timeout_for(_request()) == pytest.approx(90)


def test_a_hung_handler_close_is_bounded() -> None:
    """browser.close() never resolves (handler.py 407-411) -> _close returns."""
    browser = FakeBrowser()
    handler = build_handler(BoundedPlaywrightDownloadHandler, browser)
    handler.playwright_context_manager = None
    handler.playwright = None

    run(handler._close(), within=3)
