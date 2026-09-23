"""Hard wall-clock deadline for one spider PROCESS (2026-09-23).

`CLOSESPIDER_TIMEOUT` asks the engine to close the spider gracefully, and
the engine then waits for every in-flight download to finish. A spider
wedged INSIDE a download handler (a Playwright navigation or an
impersonated curl transfer that never returns) therefore never closes:
on 2026-09-22 one browser process held the browser node's single slot for
25 h, and on 2026-09-23 four noon batches sat in four of the HTTP node's
eight slots for over an hour producing nothing, while every batch behind
them queued. Scrapyd has no kill-after of its own.

This extension is the backstop the graceful path lacks: `grace` seconds
after the graceful deadline it terminates the process outright, which
frees the Scrapyd slot. Anything unflushed in the wedged process is lost,
but it was not going to be flushed anyway, and the STARTED reaper hands
the batch's targets back to the dispatcher.

Configured from the project's `CLOSESPIDER_TIMEOUT` (so the two deadlines
cannot drift apart) plus `HARD_DEADLINE_GRACE_SECONDS`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from scrapy import signals
from scrapy.exceptions import NotConfigured

logger = logging.getLogger(__name__)

# Distinct from Scrapy's own exit codes so a Scrapyd log reader can tell a
# deadline kill from a crash.
HARD_DEADLINE_EXIT_CODE = 75
DEFAULT_GRACE_SECONDS = 120


class HardDeadlineExtension:
    """Terminate the spider process `timeout + grace` seconds after it opens."""

    def __init__(
        self,
        *,
        timeout_seconds: int,
        grace_seconds: int,
        call_later: Callable[..., Any],
        exit_fn: Callable[[int], Any] = os._exit,
    ) -> None:
        if timeout_seconds <= 0:
            raise NotConfigured("CLOSESPIDER_TIMEOUT is not set; no hard deadline")
        self.deadline_seconds = timeout_seconds + max(grace_seconds, 0)
        self._call_later = call_later
        self._exit = exit_fn
        self._handle: Any = None

    @classmethod
    def from_crawler(cls, crawler: Any) -> HardDeadlineExtension:
        from twisted.internet import reactor

        ext = cls(
            timeout_seconds=crawler.settings.getint("CLOSESPIDER_TIMEOUT", 0),
            grace_seconds=crawler.settings.getint(
                "HARD_DEADLINE_GRACE_SECONDS", DEFAULT_GRACE_SECONDS
            ),
            call_later=reactor.callLater,  # type: ignore[attr-defined]
        )
        crawler.signals.connect(ext.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        return ext

    def spider_opened(self, spider: Any) -> None:
        self._handle = self._call_later(self.deadline_seconds, self._fire, spider)

    def spider_closed(self, spider: Any, reason: str | None = None) -> None:
        if self._handle is not None and self._handle.active():
            self._handle.cancel()
        self._handle = None

    def _fire(self, spider: Any) -> None:
        logger.critical(
            "hard_deadline: spider %s still alive %ds after opening (graceful "
            "CLOSESPIDER_TIMEOUT did not end it); terminating the process to free "
            "the Scrapyd slot",
            getattr(spider, "name", spider),
            self.deadline_seconds,
        )
        self._exit(HARD_DEADLINE_EXIT_CODE)
