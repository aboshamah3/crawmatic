"""``generic_browser_price_spider.classify_browser_failure`` ordering unit
tests (SPEC-14 T033, US4, `contracts/browser-safety.md`).

Pure/off-reactor -- no Chromium, no reactor, no DB. Exercises the priority
order the browser spider's `errback` relies on to classify a failed fetch:
SSRF/robots rejections resolve to `BLOCKED` first (via the reused
`scrape_core.errors.classify_exception`/`rejection_registry`, never
re-implemented here), then the US3 variant codes, then a proxy-context
failure resolves to `PROXY_FAILED`, with `classify_playwright_exception`
(US1/T018) as the final catch-all.
"""

from __future__ import annotations

import uuid

import pytest

from app_shared.enums import RobotsPolicy, ScrapeErrorCode

from price_monitor_browser.spiders.generic_browser_price_spider import classify_browser_failure

from scrape_core.browser.variant import VariantConfigError
from scrape_core.robots import RobotsBlockedError
from scrape_core.safety.middleware import SsrfRejectedError
from scrape_core.safety.rejection_registry import mark_rejected
from scrape_core.targets import SpiderTarget


def _target(*, variant_selector_config: dict | None = None) -> SpiderTarget:
    return SpiderTarget(
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        url="https://shop.example.com/product/1",
        profile=None,
        robots_policy=RobotsPolicy.RESPECT,
        variant_selector_config=variant_selector_config,
    )


# --- SSRF/robots -> BLOCKED, checked first -----------------------------------


def test_ssrf_rejected_error_classifies_as_blocked() -> None:
    exc = SsrfRejectedError("unsafe URL rejected pre-fetch")

    assert classify_browser_failure(exc, "shop.example.com") == ScrapeErrorCode.BLOCKED


def test_robots_blocked_error_classifies_as_blocked() -> None:
    exc = RobotsBlockedError("robots_policy=RESPECT: disallowed")

    assert classify_browser_failure(exc, "shop.example.com") == ScrapeErrorCode.BLOCKED


def test_playwright_abort_rejection_registry_hit_classifies_as_blocked() -> None:
    """The per-navigation-hop `PLAYWRIGHT_ABORT_REQUEST` rejection surfaces
    only as a generic Playwright network error with no `error_code` of its
    own (`scrape_core.browser.ssrf` module docstring) -- recognized here via
    the same `rejection_registry` side-channel `abort_unsafe_request` marks."""
    hostname = f"blocked-{uuid.uuid4().hex}.example.com"
    mark_rejected(hostname)
    generic_playwright_error = Exception("net::ERR_FAILED")

    assert classify_browser_failure(generic_playwright_error, hostname) == ScrapeErrorCode.BLOCKED


def test_blocked_takes_priority_over_a_proxied_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when the failed request used a proxied context, an SSRF/robots
    rejection classifies BLOCKED, never PROXY_FAILED."""
    exc = SsrfRejectedError("unsafe URL rejected pre-fetch")

    result = classify_browser_failure(exc, "shop.example.com", used_proxy=True)

    assert result == ScrapeErrorCode.BLOCKED


# --- variant codes (US3, T027), checked next ---------------------------------


def test_variant_config_error_classifies_as_selector_broken() -> None:
    exc = VariantConfigError("variant_selector_config: bad version")

    assert classify_browser_failure(exc, None) == ScrapeErrorCode.SELECTOR_BROKEN


def test_missing_variant_element_classifies_as_variant_not_found() -> None:
    target = _target(variant_selector_config={"version": 1, "actions": [{"type": "click", "selector": "#size-M"}]})
    exc = Exception("Timeout waiting for selector #size-M")

    result = classify_browser_failure(exc, None, target)

    assert result == ScrapeErrorCode.VARIANT_NOT_FOUND


# --- proxy-context failure (US4, T032/T033) ----------------------------------


def test_proxy_shaped_failure_on_a_proxied_context_classifies_as_proxy_failed() -> None:
    exc = Exception("net::ERR_TUNNEL_CONNECTION_FAILED connecting to proxy")

    result = classify_browser_failure(exc, "shop.example.com", used_proxy=True)

    assert result == ScrapeErrorCode.PROXY_FAILED


def test_proxy_shaped_failure_without_a_proxied_context_is_not_proxy_failed() -> None:
    """`used_proxy=False` (the default, an unproxied/default-context request)
    never classifies as `PROXY_FAILED` even if the message happens to mention
    "proxy" -- falls through to the generic Playwright classification."""
    exc = Exception("some error mentioning a proxy incidentally")

    result = classify_browser_failure(exc, "shop.example.com", used_proxy=False)

    assert result == ScrapeErrorCode.PLAYWRIGHT_FAILED


def test_timeout_on_a_proxied_context_still_classifies_as_timeout() -> None:
    """An ordinary navigation timeout through a working proxy must still
    classify TIMEOUT, not PROXY_FAILED -- the proxy-failure heuristic only
    matches a proxy-connect-shaped message."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    exc = PlaywrightTimeoutError("Timeout 30000ms exceeded.")

    result = classify_browser_failure(exc, "shop.example.com", used_proxy=True)

    assert result == ScrapeErrorCode.TIMEOUT


# --- catch-all: classify_playwright_exception (US1, T018) --------------------


def test_unrelated_failure_falls_through_to_playwright_classification() -> None:
    exc = Exception("Target page, context or browser has been closed")

    result = classify_browser_failure(exc, "shop.example.com")

    assert result == ScrapeErrorCode.PLAYWRIGHT_FAILED
