"""Playwright page-interaction plan for one target (SPEC-14 T013/T026,
US1/US3, `contracts/browser-spider.md` ``_browser_request_for``).

Builds the ordered ``scrapy_playwright.page.PageMethod`` list + the
effective per-target navigation/wait timeout that
``generic_browser_price_spider._browser_request_for`` stamps onto
``request.meta["playwright_page_methods"]``/goto kwargs, in contract
order (`browser-spider.md`): the resolved profile's ``wait_for_selector``
(or an explicit ``wait_for_load_state("networkidle")`` fallback when none
is configured, analyze finding B1) -- **then** (T026, US3) the target's
``variant_selector_config`` ``actions``/``settle`` steps, translated by
``scrape_core.browser.variant.parse_variant_config`` off the target's
already-resolved ``match_variant_values`` (`scrape_core.targets.load_targets`,
T025). ``config is None`` -> no variant steps appended -- US1 behavior
(exactly one wait step) is unchanged for a target with no variant config.
"""

from __future__ import annotations

from typing import Any

from app_shared.config import get_settings

from scrapy_playwright.page import PageMethod

from scrape_core.browser.variant import parse_variant_config

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
    """The full ordered `PageMethod` list for `target` (US1 base wait +
    US3 variant interaction), in contract order (`browser-spider.md`):
    profile ``wait_for_selector`` (if set) -> variant ``actions`` (if
    ``variant_selector_config`` is set) -> variant ``settle`` (if set).

    The resolved profile's ``wait_for_selector`` (if set) becomes a
    ``wait_for_selector`` `PageMethod` bounded by :func:`effective_timeout`.
    When it is *unset* (spec FR-003 / Edge Cases "no wait_for_selector"),
    the "normal load/network settle" default is made explicit (analyze
    B1 fix) rather than relying solely on the ``goto`` load event: an
    equally-bounded ``wait_for_load_state("networkidle")`` `PageMethod` is
    appended instead, so extraction always runs against a settled
    rendered DOM. Exactly one wait step is produced by this base half in
    every case.

    US3: `target.variant_selector_config` is then translated by
    :func:`~scrape_core.browser.variant.parse_variant_config` against
    `target.match_variant_values` (already resolved off-reactor by
    `load_targets`, T025) and appended -- `config is None` contributes no
    extra steps, so a target with no variant config keeps the exact US1
    single-wait-step list. A raised `VariantConfigError` here propagates
    to the caller (`_browser_request_for`) uncaught -- the spider's own
    pre-fetch guard (T027) is expected to have already validated the
    config before this point is ever reached during a real dispatch, but
    this function itself makes no attempt to swallow the error.
    """
    settings = get_settings()
    timeout_ms = effective_timeout(target, settings)

    if target.wait_for_selector:
        methods: list[PageMethod] = [
            PageMethod("wait_for_selector", target.wait_for_selector, timeout=timeout_ms),
        ]
    else:
        methods = [
            PageMethod("wait_for_load_state", "networkidle", timeout=timeout_ms),
        ]

    methods.extend(
        parse_variant_config(
            target.variant_selector_config,
            target.match_variant_values,
            timeout_ms=timeout_ms,
        )
    )
    return methods
