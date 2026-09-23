"""The browser Scrapyd project must cap every spider process (2026-09-23).

The browser node runs `max_proc=1`. A Playwright process that wedges holds
that one slot forever -- on 2026-09-22 one did for 25 h and again on
2026-09-23 for over an hour, and every amazon browser escalation queued
behind it. The STARTED reaper cannot recover this: it keys off
`started_at`, which a spider that never claimed its targets never wrote.
So the project sets Scrapy's `CLOSESPIDER_TIMEOUT`, from config (Principle
IV), and keeps it below the reaper horizon so a capped batch is over
before its targets are handed back to the dispatcher.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def _load_browser_settings(monkeypatch: pytest.MonkeyPatch):
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    from app_shared.config import get_settings

    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    spec = importlib.util.find_spec("price_monitor_browser.settings")
    if spec is None or spec.loader is None:
        pytest.skip("price_monitor_browser is not importable in this environment")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_browser_project_caps_each_spider_process(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _load_browser_settings(monkeypatch)
    from app_shared.config import get_settings

    cfg = get_settings()
    assert settings.CLOSESPIDER_TIMEOUT == cfg.SCRAPE_BROWSER_SPIDER_MAX_RUNTIME_SECONDS
    assert settings.CLOSESPIDER_TIMEOUT > 0


def test_the_cap_sits_below_the_started_reaper_horizon(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    from app_shared.config import get_settings

    cfg = get_settings()
    # A batch the cap closes must be over BEFORE the reaper reverts its
    # targets to PENDING, or the same URLs get fetched twice.
    assert cfg.SCRAPE_BROWSER_SPIDER_MAX_RUNTIME_SECONDS < cfg.SCRAPE_STARTED_REAP_AFTER_SECONDS
