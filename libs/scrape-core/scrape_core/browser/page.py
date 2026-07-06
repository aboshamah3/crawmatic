"""Playwright page-interaction plan for one target (SPEC-14 T013, US1,
`contracts/browser-spider.md` ``_browser_request_for``).

Builds the ordered ``scrapy_playwright.page.PageMethod`` list + the
effective per-target navigation/wait timeout that
``generic_browser_price_spider._browser_request_for`` stamps onto
``request.meta["playwright_page_methods"]``/goto kwargs. US1 only ever
produces a single wait step (the resolved profile's ``wait_for_selector``,
or an explicit ``wait_for_load_state("networkidle")`` fallback when none
is configured, analyze finding B1) -- the variant ``actions``/``settle``
steps (US3, `scrape_core.browser.variant.parse_variant_config`) are
appended *after* this module's list by the spider, never inside it, so
this module never needs to know about variant config at all (clean
extension point per `browser-spider.md`'s
``[wait_for_selector PageMethod?] + variant PageMethods + settle
PageMethod?`` ordering).
"""

from __future__ import annotations

from typing import Any

from app_shared.config import get_settings

from scrapy_playwright.page import PageMethod

__all__ = ["effective_timeout", "build_page_methods"]


def effective_timeout(target: Any, settings: Any) -> int:
    """The effective navigation/wait timeout in milliseconds for `target` (R10).

    ``target.browser_timeout_ms`` when the resolved scrape profile set
    one, else the process-wide ``Settings.SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS``
    default (env/DB-tunable, Principle IV -- never a hardcoded literal at
    the call site).
    """
    if target.browser_timeout_ms is not None:
        return target.browser_timeout_ms
    return settings.SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS


def build_page_methods(target: Any) -> list[PageMethod]:
    """The base (pre-variant) ordered `PageMethod` list for `target` (US1).

    The resolved profile's ``wait_for_selector`` (if set) becomes a
    ``wait_for_selector`` `PageMethod` bounded by :func:`effective_timeout`.
    When it is *unset* (spec FR-003 / Edge Cases "no wait_for_selector"),
    the "normal load/network settle" default is made explicit (analyze
    B1 fix) rather than relying solely on the ``goto`` load event: an
    equally-bounded ``wait_for_load_state("networkidle")`` `PageMethod` is
    appended instead, so extraction always runs against a settled
    rendered DOM.

    Exactly one wait step is returned here in US1. US3
    (`scrape_core.browser.variant.parse_variant_config`) appends its own
    ``actions``/``settle`` `PageMethod`s *after* this list, in the
    spider's ``_browser_request_for`` -- this function never grows a
    variant-awareness itself.
    """
    settings = get_settings()
    timeout_ms = effective_timeout(target, settings)

    if target.wait_for_selector:
        return [
            PageMethod("wait_for_selector", target.wait_for_selector, timeout=timeout_ms),
        ]
    return [
        PageMethod("wait_for_load_state", "networkidle", timeout=timeout_ms),
    ]
