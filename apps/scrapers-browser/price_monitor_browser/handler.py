"""scrapy-playwright download handler with a wall-clock bound (2026-09-29, E1).

Why this exists
---------------

scrapy-playwright ignores Scrapy's ``DOWNLOAD_TIMEOUT``. Its own timeouts
cover only ``page.goto`` and the ``PageMethod`` waits; every other await on
the download path is unbounded (0.0.47, ``scrapy_playwright/handler.py``):
``browser.new_context`` / ``context.new_page`` (274-282, 332),
``response.all_headers`` (524), ``page.content`` (541) and ``page.close``
(509, 561). One of them never resolving keeps the request in the engine's
in-progress set forever (``scrapy/core/engine.py`` 87-97, 617), so
``CLOSESPIDER_TIMEOUT`` asks for a close that waits on it forever too. The
same is true at shutdown: ``_close`` awaits ``browser.close`` and
``playwright.stop`` (407-411) with no bound. That is the nightly shape:
~940 targets queued behind one browser process that never finished.

What it changes
---------------

* Every Playwright download is bounded by the request's own effective
  timeout (its ``goto`` timeout plus every ``PageMethod`` timeout, never less
  than ``DOWNLOAD_TIMEOUT``) plus ``BROWSER_DOWNLOAD_HARD_MARGIN_SECONDS``
  (context/page creation, content, close). Past it the request fails with
  :class:`BrowserDownloadTimeoutError`, which the spider's errback classifies
  as ``TIMEOUT`` (by name, ``scrape_core.errors.classify_playwright_exception``)
  -- and which Scrapy's RetryMiddleware does NOT retry, matching how a
  Playwright ``TimeoutError`` has always been treated on this node. The page
  the request opened is then closed, itself under a bound, so its per-context
  page slot is released instead of leaking into the next wedge.
* The inner coroutine is cancelled but NOT awaited: ``asyncio.wait_for``
  would wait for the cancellation to finish, and a coroutine stuck in a
  cleanup await would hold the handler exactly as before.
* The context pool cannot deadlock. The spider names a proxied target's
  context ``proxy:<provider_id>`` next to the unproxied ``default``, and the
  pool is capped (``PLAYWRIGHT_MAX_CONTEXTS``). scrapy-playwright never
  closes a context before shutdown, so a name beyond the cap awaited
  ``context_semaphore`` (264) forever -- while holding ``context_launch_lock``
  (321), which every later request needs as well. Here a request that needs
  a slot first closes IDLE contexts (no page open, none being opened) until
  one frees; a context with a page in flight is never closed under it.
* ``_close`` (handler shutdown) is bounded by
  ``BROWSER_HANDLER_CLOSE_TIMEOUT_SECONDS``; past it the process moves on to
  exit and the out-of-reactor watchdog (``scrape_core.process_watchdog``)
  remains the last line.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from scrapy import Request, Spider
from scrapy.http import Response
from scrapy_playwright.handler import ScrapyPlaywrightDownloadHandler
from scrapy_playwright.page import PageMethod

logger = logging.getLogger(__name__)

DEFAULT_HARD_MARGIN_SECONDS = 30.0
DEFAULT_CLOSE_TIMEOUT_SECONDS = 30.0
DEFAULT_PAGE_CLOSE_TIMEOUT_SECONDS = 10.0
DEFAULT_IDLE_CONTEXT_POLL_SECONDS = 0.25


class BrowserDownloadTimeoutError(Exception):
    """A Playwright download outlived its wall-clock bound.

    Deliberately NOT a subclass of ``twisted.internet.error.TimeoutError``:
    Scrapy's RetryMiddleware retries that type, and a wedged browser
    retried twice is three wedges. The name carries "Timeout" so the
    browser failure classifier maps it to ``TIMEOUT``.
    """


def _drain(task: asyncio.Future[Any]) -> None:
    """Consume an abandoned task's outcome so asyncio never logs
    'exception was never retrieved' for it."""
    if not task.cancelled():
        with suppress(BaseException):
            task.exception()


class BoundedPlaywrightDownloadHandler(ScrapyPlaywrightDownloadHandler):
    """``ScrapyPlaywrightDownloadHandler`` whose downloads and shutdown end."""

    def __init__(self, crawler: Any) -> None:
        super().__init__(crawler)
        self._configure_bounds(crawler.settings)

    def _configure_bounds(self, settings: Any) -> None:
        # Split out of __init__ so a test can build the handler without an
        # installed asyncio reactor (the parent __init__ verifies it).
        self._hard_margin_seconds = settings.getfloat(
            "BROWSER_DOWNLOAD_HARD_MARGIN_SECONDS", DEFAULT_HARD_MARGIN_SECONDS
        )
        self._close_timeout_seconds = settings.getfloat(
            "BROWSER_HANDLER_CLOSE_TIMEOUT_SECONDS", DEFAULT_CLOSE_TIMEOUT_SECONDS
        )
        self._page_close_timeout_seconds = settings.getfloat(
            "BROWSER_PAGE_CLOSE_TIMEOUT_SECONDS", DEFAULT_PAGE_CLOSE_TIMEOUT_SECONDS
        )
        self._download_timeout_seconds = settings.getfloat("DOWNLOAD_TIMEOUT", 180.0)
        self._idle_context_poll_seconds = settings.getfloat(
            "BROWSER_IDLE_CONTEXT_POLL_SECONDS", DEFAULT_IDLE_CONTEXT_POLL_SECONDS
        )
        # Pages opened per in-flight request, keyed by id(request) and
        # popped in `finally`, so a timed-out request's page can be closed.
        # Not kept in request.meta: meta travels into items, logs and
        # errbacks, and a live Page object has no business there.
        self._pages_by_request: dict[int, Any] = {}

    # ── the bound ─────────────────────────────────────────────────────────
    def hard_timeout_for(self, request: Request) -> float:
        """Seconds this request may take end to end before it is abandoned.

        The sum of the timeouts the request itself declares (``goto`` plus
        each ``PageMethod`` that carries one) is the longest a healthy page
        can legitimately take; ``DOWNLOAD_TIMEOUT`` (or the request's own
        ``download_timeout``) is the floor. The margin pays for context and
        page creation, ``page.content()`` and ``page.close()``, none of
        which carry a timeout of their own.
        """
        declared_ms = 0.0
        goto_kwargs = request.meta.get("playwright_page_goto_kwargs") or {}
        goto_timeout = goto_kwargs.get("timeout")
        if goto_timeout is None:
            goto_timeout = getattr(getattr(self, "config", None), "navigation_timeout_ms", None)
        if goto_timeout:
            declared_ms += float(goto_timeout)
        methods = request.meta.get("playwright_page_methods") or ()
        if isinstance(methods, dict):
            methods = methods.values()
        for method in methods:
            if isinstance(method, PageMethod):
                timeout = method.kwargs.get("timeout")
                if timeout:
                    declared_ms += float(timeout)
        floor = float(request.meta.get("download_timeout") or self._download_timeout_seconds)
        return max(declared_ms / 1000.0, floor) + self._hard_margin_seconds

    async def _download_request(self, request: Request, spider: Spider | None = None) -> Response:
        bound = self.hard_timeout_for(request)
        task = asyncio.ensure_future(super()._download_request(request, spider))
        try:
            done, _ = await asyncio.wait({task}, timeout=bound)
            if task in done:
                return task.result()
            task.cancel()
            task.add_done_callback(_drain)
            await self._close_abandoned_page(request)
            logger.error(
                "browser_download.hard_timeout url=%s bound_s=%.0f context=%s -- a "
                "Playwright await never resolved; request abandoned as TIMEOUT",
                request.url,
                bound,
                request.meta.get("playwright_context"),
            )
            raise BrowserDownloadTimeoutError(
                f"browser download exceeded its {bound:.0f}s wall-clock bound: {request.url}"
            )
        finally:
            self._pages_by_request.pop(id(request), None)

    async def _create_page(self, request: Request, spider: Spider) -> Any:
        page = await super()._create_page(request=request, spider=spider)
        self._pages_by_request[id(request)] = page
        return page

    async def _close_abandoned_page(self, request: Request) -> None:
        page = self._pages_by_request.get(id(request))
        if page is None:
            return
        with suppress(Exception):
            if page.is_closed():
                return
        close = asyncio.ensure_future(page.close())
        done, _ = await asyncio.wait({close}, timeout=self._page_close_timeout_seconds)
        if close in done:
            _drain(close)
            return
        close.cancel()
        close.add_done_callback(_drain)
        logger.error(
            "browser_download.page_close_hung url=%s -- the page slot stays taken; "
            "the process watchdog is the backstop",
            request.url,
        )

    # ── the context pool ──────────────────────────────────────────────────
    async def _create_browser_context(
        self, name: str, context_kwargs: dict | None, spider: Spider | None = None
    ) -> Any:
        # Called with `context_launch_lock` held (by `_create_page`), so no
        # other request can create a context meanwhile. Waiting here is
        # still bounded: the whole download sits under `hard_timeout_for`.
        semaphore = getattr(self, "context_semaphore", None)
        if semaphore is not None:
            while semaphore.locked():
                if not await self._close_idle_contexts(keep=name):
                    await asyncio.sleep(self._idle_context_poll_seconds)
        # `Semaphore.acquire()` on an unlocked semaphore returns without
        # yielding, so the slot freed above cannot be taken by anyone else
        # before the parent's acquire.
        return await super()._create_browser_context(name, context_kwargs, spider)

    def _context_is_idle(self, wrapper: Any) -> bool:
        # A page slot is taken (`wrapper.semaphore.acquire()`, handler.py
        # 330) BEFORE `new_page()` and released only by the page's close
        # event, so a full page semaphore means no page is open AND none is
        # being opened. `context.pages` alone would miss the second case
        # and close a context under a request about to use it.
        return (
            not wrapper.persistent
            and not wrapper.context.pages
            and wrapper.semaphore._value >= self.config.max_pages_per_context
        )

    async def _close_idle_contexts(self, *, keep: str) -> int:
        closed = 0
        for name, wrapper in list(self.context_wrappers.items()):
            if name == keep or not self._context_is_idle(wrapper):
                continue
            logger.info(
                "browser_context.close_idle name=%s -- freeing a pool slot for %s", name, keep
            )
            close = asyncio.ensure_future(wrapper.context.close())
            done, _ = await asyncio.wait({close}, timeout=self._page_close_timeout_seconds)
            if close not in done:
                close.cancel()
                close.add_done_callback(_drain)
                continue
            _drain(close)
            # The context's "close" event (handler.py 755) pops the wrapper
            # and releases the pool slot; make sure the slot is gone from
            # the pool even if the event was not delivered.
            if self.context_wrappers.get(name) is wrapper:
                self.context_wrappers.pop(name, None)
                if getattr(self, "context_semaphore", None) is not None:
                    self.context_semaphore.release()
            closed += 1
        return closed

    # ── shutdown ──────────────────────────────────────────────────────────
    async def _close(self) -> None:
        task = asyncio.ensure_future(super()._close())
        done, _ = await asyncio.wait({task}, timeout=self._close_timeout_seconds)
        if task in done:
            task.result()
            return
        task.cancel()
        task.add_done_callback(_drain)
        logger.critical(
            "browser_handler.close_hung after %.0fs -- continuing shutdown without "
            "a clean browser close; the process watchdog reaps anything left",
            self._close_timeout_seconds,
        )
