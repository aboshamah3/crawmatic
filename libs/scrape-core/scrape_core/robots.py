"""`RobotsPolicyMiddleware` — per-request robots handling (`contracts/robots-middleware.md`, research D7).

Resolves `robots_policy` (`app_shared.enums.RobotsPolicy`) **per
request** from the competitor config the spider loaded — attached to
`request.meta["robots_policy"]` — never Scrapy's process-global
`ROBOTSTXT_OBEY` (which stays `False` in `price_monitor/settings.py`).
A request with no explicit policy defaults to the conservative
`RESPECT` (matching `Competitor.robots_policy`'s own default).

| `robots_policy`         | Behavior                                              |
|--------------------------|--------------------------------------------------------|
| `RESPECT`                | fetch/parse robots.txt; a disallowed path is skipped/recorded (`BLOCKED`) |
| `IGNORE_AFTER_APPROVAL`  | fetch regardless (no robots.txt lookup at all)          |
| `REVIEW_REQUIRED`        | not yet approved -> always skip/record (conservative)   |

Reactor safety: the two "no IO" policies (`IGNORE_AFTER_APPROVAL`,
`REVIEW_REQUIRED`) are decided synchronously in `process_request` (no
blocking call is ever made). `RESPECT` may need to fetch robots.txt (a
cache miss) — that work is offloaded through
`scrape_core.db.run_in_thread` (the same seam `pipelines.py` uses for
DB IO), so `process_request` never blocks the reactor thread itself.
The actual decision logic lives in `_decide_respect`, a small pure/
synchronous method callable directly (bypassing the Twisted seam) — the
seam a unit test exercises without needing a running reactor.

The robots fetcher is injectable (`robots_fetcher: Callable[[str], str
| None]`) so fixture tests supply a canned robots.txt body with no
network call (FR-021, contracts/robots-middleware.md "Testability").
"""

from __future__ import annotations

import logging
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from scrapy.exceptions import IgnoreRequest

from app_shared.enums import RobotsPolicy

from scrape_core.db import run_in_thread
from scrape_core.errors import ROBOTS_BLOCKED_ERROR_CODE

__all__ = ["RobotsPolicyMiddleware", "RobotsBlockedError", "default_robots_fetcher"]

logger = logging.getLogger(__name__)

RobotsFetcher = Callable[[str], "str | None"]


class RobotsBlockedError(IgnoreRequest):
    """Raised when the per-request `robots_policy` blocks a target (`BLOCKED`)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = ROBOTS_BLOCKED_ERROR_CODE


def default_robots_fetcher(robots_url: str) -> str | None:
    """Best-effort robots.txt fetch for real (non-fixture) runs.

    Never called by unit tests (a fixture-backed fetcher is always
    injected there, per contracts/robots-middleware.md "Testability").
    Must only ever be invoked off the reactor thread (`process_request`
    below always calls this via `run_in_thread`). Returns `None` (fail
    open — no robots.txt means "allow", the conventional robots.txt
    absence semantics) on any fetch error.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(robots_url, timeout=5) as response:  # noqa: S310
            return response.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - best-effort only, never raise from here
        return None


class RobotsPolicyMiddleware:
    """Scrapy downloader middleware: per-request `robots_policy` enforcement."""

    def __init__(
        self,
        robots_fetcher: RobotsFetcher | None = None,
        user_agent: str = "price_monitor",
    ) -> None:
        self._robots_fetcher: RobotsFetcher = robots_fetcher or default_robots_fetcher
        self._user_agent = user_agent
        self._cache: dict[str, RobotFileParser | None] = {}

    @classmethod
    def from_crawler(cls, crawler: Any) -> "RobotsPolicyMiddleware":
        user_agent = crawler.settings.get("USER_AGENT") or "price_monitor"
        return cls(user_agent=user_agent)

    def process_request(self, request: Any, spider: Any) -> Any:
        policy = request.meta.get("robots_policy", RobotsPolicy.RESPECT)

        if policy == RobotsPolicy.IGNORE_AFTER_APPROVAL:
            # No IO at all -- synchronous, reactor-safe by construction.
            return None

        if policy == RobotsPolicy.REVIEW_REQUIRED:
            # No IO -- conservative "not yet approved" skip, synchronous.
            raise RobotsBlockedError(
                f"robots_policy=REVIEW_REQUIRED (not yet approved to fetch): {request.url}"
            )

        # RESPECT: may require a robots.txt fetch (cache miss) -> offload
        # off the reactor thread through the established run_in_thread seam.
        return run_in_thread(self._decide_respect, request.url, self._user_agent)

    def _decide_respect(self, url: str, user_agent: str) -> None:
        """Pure decision core for `RESPECT` -- cache-aware, synchronous.

        Safe to call directly (bypassing `run_in_thread`) in unit tests:
        with an injected fixture `robots_fetcher` there is no real IO, so
        no reactor is needed to exercise this logic.
        """
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        if origin not in self._cache:
            body = self._robots_fetcher(f"{origin}/robots.txt")
            self._cache[origin] = self._parse(body)

        parser = self._cache[origin]
        if parser is not None and not parser.can_fetch(user_agent, url):
            raise RobotsBlockedError(
                f"robots_policy=RESPECT: disallowed by robots.txt: {url}"
            )
        return None

    @staticmethod
    def _parse(body: str | None) -> RobotFileParser | None:
        if body is None:
            # No robots.txt (fetch failed / 404) -- conventional semantics:
            # absence means "allow everything".
            return None
        parser = RobotFileParser()
        parser.parse(body.splitlines())
        return parser
