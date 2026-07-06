"""``scrape_core.browser.page`` unit tests (SPEC-14 T013, US1,
`contracts/browser-spider.md` ``_browser_request_for``).

Pure/off-reactor -- no Chromium, no reactor, no DB. Exercises
``effective_timeout`` (target override vs. the `Settings` default) and
``build_page_methods`` (the resolved profile's `wait_for_selector` as a
`wait_for_selector` `PageMethod`, or the explicit `wait_for_load_state`
("networkidle") fallback when none is configured -- analyze finding B1,
so extraction always runs against a settled rendered DOM).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app_shared.enums import RobotsPolicy

from scrape_core.browser.page import build_page_methods, effective_timeout
from scrape_core.targets import SpiderTarget


def _target(*, wait_for_selector: str | None = None, browser_timeout_ms: int | None = None) -> SpiderTarget:
    return SpiderTarget(
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        url="https://shop.example.com/product/1",
        profile=None,
        robots_policy=RobotsPolicy.RESPECT,
        wait_for_selector=wait_for_selector,
        browser_timeout_ms=browser_timeout_ms,
    )


# --- effective_timeout -------------------------------------------------------


def test_effective_timeout_uses_target_override_when_set() -> None:
    target = _target(browser_timeout_ms=5000)
    settings = SimpleNamespace(SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS=30000)

    assert effective_timeout(target, settings) == 5000


def test_effective_timeout_falls_back_to_settings_default_when_unset() -> None:
    target = _target(browser_timeout_ms=None)
    settings = SimpleNamespace(SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS=30000)

    assert effective_timeout(target, settings) == 30000


# --- build_page_methods -------------------------------------------------------


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate ``build_page_methods`` from the real (env-driven, lru_cached)
    ``get_settings()`` -- it only needs ``SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS``."""
    import scrape_core.browser.page as page_module

    monkeypatch.setattr(
        page_module,
        "get_settings",
        lambda: SimpleNamespace(SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS=30000),
    )


def test_build_page_methods_with_wait_for_selector() -> None:
    target = _target(wait_for_selector=".price", browser_timeout_ms=9000)

    methods = build_page_methods(target)

    assert len(methods) == 1
    assert methods[0].method == "wait_for_selector"
    assert methods[0].args == (".price",)
    assert methods[0].kwargs == {"timeout": 9000}


def test_build_page_methods_without_wait_for_selector_appends_networkidle_settle() -> None:
    """Analyze B1: no configured selector -- an explicit networkidle wait
    stands in for the default load/settle behavior, bounded by the
    effective timeout, rather than relying on the goto load event alone."""
    target = _target(wait_for_selector=None, browser_timeout_ms=None)

    methods = build_page_methods(target)

    assert len(methods) == 1
    assert methods[0].method == "wait_for_load_state"
    assert methods[0].args == ("networkidle",)
    assert methods[0].kwargs == {"timeout": 30000}


def test_build_page_methods_uses_effective_timeout_for_selector_wait() -> None:
    target = _target(wait_for_selector=".price", browser_timeout_ms=None)

    methods = build_page_methods(target)

    assert methods[0].kwargs == {"timeout": 30000}
