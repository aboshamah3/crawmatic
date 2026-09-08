"""F15 (B7): `app_shared.database` sets a statement timeout + pool-acquire
deadline on the shared engine (`contracts/`, PLAN §7.4 risk P5).

Before F15 `get_engine()` set neither: an unbounded query or an
exhausted pool could hang a caller (and, on the API's event loop,
everyone sharing it) indefinitely. `create_engine` never opens a
connection eagerly (`pool_pre_ping=True` only pings on checkout), so
these tests capture the kwargs `get_engine()` passes to it without
touching a real database.
"""

from __future__ import annotations

import importlib

import app_shared.database as database_mod

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


def _fresh_module_for_env(monkeypatch, **env: str):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(database_mod)


def test_default_statement_timeout_and_pool_acquire_timeout(monkeypatch):
    monkeypatch.delenv("DB_STATEMENT_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("DB_POOL_ACQUIRE_TIMEOUT_SECONDS", raising=False)
    module = importlib.reload(database_mod)

    # API's tighter default (see module docstring): a worker overrides
    # both via its own environment for the plan's 120,000 ms figure.
    assert module.DB_STATEMENT_TIMEOUT_MS == 15000
    assert module.DB_POOL_ACQUIRE_TIMEOUT_SECONDS == 15


def test_timeouts_are_environment_overridable_eg_for_workers(monkeypatch):
    module = _fresh_module_for_env(
        monkeypatch,
        DB_STATEMENT_TIMEOUT_MS="120000",
        DB_POOL_ACQUIRE_TIMEOUT_SECONDS="120",
    )

    assert module.DB_STATEMENT_TIMEOUT_MS == 120000
    assert module.DB_POOL_ACQUIRE_TIMEOUT_SECONDS == 120

    monkeypatch.delenv("DB_STATEMENT_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("DB_POOL_ACQUIRE_TIMEOUT_SECONDS", raising=False)
    importlib.reload(database_mod)


def test_get_engine_passes_statement_timeout_and_pool_timeout(monkeypatch):
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "15000")
    monkeypatch.setenv("DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "15")
    # Never actually assert the RLS role from a fake connection.
    monkeypatch.delenv("RLS_ROLE_ASSERTION", raising=False)

    from app_shared.config import get_settings

    get_settings.cache_clear()
    module = importlib.reload(database_mod)

    captured: dict[str, object] = {}
    real_create_engine = module.create_engine

    def _spy_create_engine(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return real_create_engine(*args, **kwargs)

    monkeypatch.setattr(module, "create_engine", _spy_create_engine)
    module.dispose_engine()

    try:
        module.get_engine()
    finally:
        module.dispose_engine()
        get_settings.cache_clear()
        importlib.reload(database_mod)

    kwargs = captured["kwargs"]
    assert kwargs["pool_timeout"] == 15
    assert kwargs["connect_args"]["options"] == "-c statement_timeout=15000"
    # F15 didn't regress the pre-existing PgBouncer prepared-statement fix.
    assert kwargs["connect_args"]["prepare_threshold"] is None


def test_override_statement_timeout_sets_local_config():
    """`override_statement_timeout` issues a bound, transaction-local
    `SET LOCAL`-equivalent (`set_config(..., true)`) rather than string
    interpolation, mirroring `set_workspace_context`'s own contract --
    a maintenance/sweep task can raise its own ceiling without touching
    the process-wide default other callers share."""

    class _FakeSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def execute(self, statement, params=None):
            self.calls.append((str(statement), params or {}))

    session = _FakeSession()
    with database_mod.override_statement_timeout(session, 300_000):
        pass

    assert len(session.calls) == 1
    sql, params = session.calls[0]
    assert "set_config" in sql
    assert "statement_timeout" in sql
    assert params["timeout_ms"] == "300000"
