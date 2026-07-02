"""Unit tests for env-driven configuration (contracts/environment.md, T042).

Asserts the two behaviors `app_shared/config.py` must provide (FR-017,
FR-018):

1. A missing required environment variable makes ``Settings`` fail fast
   with a clear validation error instead of starting half-configured.
2. ``SCRAPYD_HTTP_URLS``/``SCRAPYD_BROWSER_URLS`` are parsed as pools —
   comma-separated, and still a list even with a single URL.

``_env_file=None`` is passed to every ``Settings(...)`` call so these
tests only see the environment variables set explicitly within the test,
never a developer's local ``.env``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app_shared.config import Settings

REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
}


def test_missing_required_var_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A required variable left unset raises instead of starting half-configured."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    # Leave DATABASE_URL unset to trigger the fail-fast path.
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_scrapyd_pools_parse_single_and_multi_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comma-separated Scrapyd URLs are pools even when length 1 (FR-018)."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SCRAPYD_HTTP_URLS", "http://scrapers:6800")
    monkeypatch.setenv(
        "SCRAPYD_BROWSER_URLS", "http://scrapers-browser-a:6800,http://scrapers-browser-b:6800"
    )

    settings = Settings(_env_file=None)

    assert settings.SCRAPYD_HTTP_URLS == ["http://scrapers:6800"]
    assert settings.SCRAPYD_BROWSER_URLS == [
        "http://scrapers-browser-a:6800",
        "http://scrapers-browser-b:6800",
    ]
