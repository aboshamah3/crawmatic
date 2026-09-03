"""`configure_thread_pool` — bound the API's request thread pool to the DB
pool (H2, production-readiness audit).

Test 1 exercises the pure function directly, inside an anyio event loop
(the limiter it touches is per-event-loop, so this can't run at plain
module/sync scope).

Test 2 exercises it wired into the real app: `TestClient(app)` used as a
context manager fires FastAPI's startup event, which should call
`configure_thread_pool(get_settings())` exactly as `app.main` wires it.
Constructing `Settings` requires a full set of otherwise-required env
vars (`DATABASE_URL` etc., `app_shared.config.Settings`) that a bare unit
test environment doesn't have, so this test supplies them via
`monkeypatch.setenv` the same way `tests/unit/test_config.py` does, and
clears `get_settings`'s `lru_cache` before and after so it picks up the
patched environment instead of a value cached by an earlier test/process.
"""

from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient

from app_shared.config import Settings, get_settings

from app.thread_pool import configure_thread_pool

#: Same required-env shape as `tests/unit/test_config.py::REQUIRED_ENV` —
#: every field `Settings` has no default for.
_REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def test_configure_thread_pool_sets_limiter_to_api_thread_pool_size() -> None:
    settings = Settings(_env_file=None, API_THREAD_POOL_SIZE=8, **_REQUIRED_ENV)

    async def _run() -> int:
        returned = configure_thread_pool(settings)
        limiter = anyio.to_thread.current_default_thread_limiter()
        assert limiter.total_tokens == 8
        return returned

    result = anyio.run(_run)

    assert result == 8


def test_configure_thread_pool_uses_the_configured_value_not_a_hardcoded_one() -> None:
    settings = Settings(_env_file=None, API_THREAD_POOL_SIZE=3, **_REQUIRED_ENV)

    async def _run() -> None:
        configure_thread_pool(settings)
        limiter = anyio.to_thread.current_default_thread_limiter()
        assert limiter.total_tokens == 3

    anyio.run(_run)


def test_app_startup_configures_the_limiter_from_get_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TestClient(app)` as a context manager fires the real startup hook.

    The hook runs `configure_thread_pool(get_settings())` on the event loop
    of `TestClient`'s own background portal thread (see
    `starlette.testclient.TestClient.__enter__` — it drives the app via
    `anyio.from_thread.start_blocking_portal`), which is a *different*
    thread than this test runs on. `anyio.to_thread.current_default_thread_
    limiter()` reads a per-thread contextvar and raises `NoEventLoopError`
    when called with no event loop running in the calling thread, so it
    can't be read directly from here — instead, `client.portal.call(...)`
    (the same primitive the client itself uses to drive the app) runs the
    read on the portal's thread/loop and marshals the resulting object back.
    """
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("API_THREAD_POOL_SIZE", "8")
    get_settings.cache_clear()
    try:
        from app.main import app

        with TestClient(app) as client:
            limiter = client.portal.call(anyio.to_thread.current_default_thread_limiter)
            assert limiter.total_tokens == get_settings().API_THREAD_POOL_SIZE
            assert limiter.total_tokens == 8
    finally:
        get_settings.cache_clear()
