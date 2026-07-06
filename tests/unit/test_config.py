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
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
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


def test_missing_encryption_keys_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """ENCRYPTION_KEYS has no default — a misconfigured deployment fails fast (SPEC-10 FR-003)."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ENCRYPTION_KEYS", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_encryption_primary_version_absent_from_keyring_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENCRYPTION_PRIMARY_KEY_VERSION must name a version present in ENCRYPTION_KEYS."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ENCRYPTION_KEYS", "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=")
    monkeypatch.setenv("ENCRYPTION_PRIMARY_KEY_VERSION", "2")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_encryption_keyring_parses_multiple_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A multi-key ring (rotation scenario) parses and validates the primary version."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(
        "ENCRYPTION_KEYS",
        "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=,"
        "2:9AqozIiy37PMTubfj6a0EmQoJfe_bnGqZ1oGqQGZBjM=",
    )
    monkeypatch.setenv("ENCRYPTION_PRIMARY_KEY_VERSION", "2")

    settings = Settings(_env_file=None)

    assert settings.ENCRYPTION_KEYS.startswith("1:")
    assert settings.ENCRYPTION_PRIMARY_KEY_VERSION == 2


def test_access_resolution_cache_ttl_defaults_to_30_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACCESS_RESOLUTION_CACHE_TTL_SECONDS defaults to 30 when unset (SPEC-10 FR-007)."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ACCESS_RESOLUTION_CACHE_TTL_SECONDS", raising=False)

    settings = Settings(_env_file=None)

    assert settings.ACCESS_RESOLUTION_CACHE_TTL_SECONDS == 30


def test_browser_scraping_knobs_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browser-service knobs default per data-model.md §4 (SPEC-14 R9/R10) when unset."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("BROWSER_CONCURRENT_REQUESTS", raising=False)
    monkeypatch.delenv("BROWSER_MAX_CONTEXTS", raising=False)

    settings = Settings(_env_file=None)

    assert settings.SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS == 30000
    assert settings.BROWSER_CONCURRENT_REQUESTS == 2
    assert settings.BROWSER_MAX_CONTEXTS == 1


def test_browser_scraping_knobs_honor_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browser-service knobs are env/DB-tunable, never hardcoded (SPEC-14 Principle IV)."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS", "45000")
    monkeypatch.setenv("BROWSER_CONCURRENT_REQUESTS", "4")
    monkeypatch.setenv("BROWSER_MAX_CONTEXTS", "2")

    settings = Settings(_env_file=None)

    assert settings.SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS == 45000
    assert settings.BROWSER_CONCURRENT_REQUESTS == 4
    assert settings.BROWSER_MAX_CONTEXTS == 2
