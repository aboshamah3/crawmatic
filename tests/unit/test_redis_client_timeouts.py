"""F15 (B7): `app_shared.redis_client` reads socket/connect timeouts.

Before F15 the process-wide sync Redis client carried no
`socket_timeout`/`socket_connect_timeout` at all, so a wedged connection
could block a caller indefinitely. These tests assert the client is
constructed with the documented defaults (and that the two new knobs
are environment-overridable) without opening a real socket -- `redis.Redis.
from_url` doesn't connect until a command is issued, so asserting the
constructed client's `connection_pool.connection_kwargs` is enough.
"""

from __future__ import annotations

import importlib

import app_shared.redis_client as redis_client_mod

#: Same required-env shape as `tests/unit/test_config.py`/
#: `tests/unit/test_api_thread_pool.py` -- every field `Settings` has no
#: default for.
_REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://127.0.0.1:1/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def _reload_with_env(monkeypatch, **env: str) -> "redis_client_mod":
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    module = importlib.reload(redis_client_mod)
    return module


def test_default_timeouts_match_documented_defaults(monkeypatch):
    monkeypatch.delenv("REDIS_SOCKET_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("REDIS_CONNECT_TIMEOUT_SECONDS", raising=False)
    module = importlib.reload(redis_client_mod)

    assert module.REDIS_SOCKET_TIMEOUT_SECONDS == 2.0
    assert module.REDIS_CONNECT_TIMEOUT_SECONDS == 1.0


def test_timeouts_are_environment_overridable(monkeypatch):
    module = _reload_with_env(
        monkeypatch,
        REDIS_SOCKET_TIMEOUT_SECONDS="5.5",
        REDIS_CONNECT_TIMEOUT_SECONDS="2.5",
    )

    assert module.REDIS_SOCKET_TIMEOUT_SECONDS == 5.5
    assert module.REDIS_CONNECT_TIMEOUT_SECONDS == 2.5

    # Restore process-wide state for any test that runs after this one
    # and imports the already-loaded module fresh from sys.modules.
    monkeypatch.delenv("REDIS_SOCKET_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("REDIS_CONNECT_TIMEOUT_SECONDS", raising=False)
    importlib.reload(redis_client_mod)


def test_get_redis_client_passes_timeouts_to_the_connection_pool(monkeypatch):
    """`redis.Redis.from_url` never connects until a command is issued,
    so this asserts the constructed pool's kwargs without any network
    access. Needs a full, valid `Settings` (see `_REQUIRED_ENV` above)."""
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    from app_shared import redis_policy as redis_policy_mod

    monkeypatch.setattr(
        redis_policy_mod, "enforce_redis_memory_policy", lambda *_a, **_k: None
    )

    from app_shared.config import get_settings

    get_settings.cache_clear()
    module = importlib.reload(redis_client_mod)
    module.dispose_redis_client()

    client = module.get_redis_client()
    try:
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] == module.REDIS_SOCKET_TIMEOUT_SECONDS
        assert kwargs["socket_connect_timeout"] == module.REDIS_CONNECT_TIMEOUT_SECONDS
        assert kwargs["health_check_interval"] == module._REDIS_HEALTH_CHECK_INTERVAL_SECONDS
    finally:
        module.dispose_redis_client()
        get_settings.cache_clear()
