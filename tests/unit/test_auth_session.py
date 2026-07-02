"""Unit tests for the auth-role DB session helper (SPEC-03 T009, [analyze C1]).

``get_auth_session()`` is the pre-auth, BYPASSRLS credential-lookup path
(user-by-email at login, api-key-by-prefix at machine auth). Under
``FORCE ROW LEVEL SECURITY`` (applied to ``users``/``api_keys`` by
``emit_rls_policy``), a non-BYPASSRLS role with no workspace context
returns **zero rows** for these lookups — so silently falling back to
``DATABASE_URL`` (the pooler role) when ``AUTH_DATABASE_URL`` is unset
would make login/API-key auth appear to fail with "wrong credentials"
while actually being a configuration bug. This module asserts the
fail-fast contract instead: no DB connection is ever attempted; the
absence of ``AUTH_DATABASE_URL`` raises immediately and clearly.
"""

from __future__ import annotations

import pytest

import app_shared.database as database


def test_get_auth_session_raises_when_auth_database_url_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No AUTH_DATABASE_URL -> get_auth_session() fails fast, never falls back."""
    monkeypatch.setattr(database, "_auth_engine", None, raising=False)
    monkeypatch.setattr(database, "_auth_sessionmaker", None, raising=False)

    class _FakeSettings:
        AUTH_DATABASE_URL: str | None = None

    monkeypatch.setattr(database, "get_settings", lambda: _FakeSettings())

    with pytest.raises(RuntimeError, match="AUTH_DATABASE_URL"):
        with database.get_auth_session():
            pass  # pragma: no cover - must never be reached

    # Never silently created an engine bound to DATABASE_URL (or anything else).
    assert database._auth_engine is None


def test_get_auth_engine_raises_when_auth_database_url_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_auth_engine() itself is the raising point — no lazy DB connect attempted."""
    monkeypatch.setattr(database, "_auth_engine", None, raising=False)

    class _FakeSettings:
        AUTH_DATABASE_URL: str | None = None

    monkeypatch.setattr(database, "get_settings", lambda: _FakeSettings())

    with pytest.raises(RuntimeError, match="AUTH_DATABASE_URL"):
        database.get_auth_engine()

    assert database._auth_engine is None


def test_get_auth_engine_builds_engine_from_auth_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AUTH_DATABASE_URL IS set, get_auth_engine() builds an engine from it
    (not from DATABASE_URL) — captured via a stubbed create_engine, no real connect.
    """
    monkeypatch.setattr(database, "_auth_engine", None, raising=False)
    captured: dict[str, object] = {}

    def _fake_create_engine(url: str, **kwargs: object) -> object:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    class _FakeSettings:
        AUTH_DATABASE_URL: str | None = (
            "postgresql+psycopg://crawmatic_auth:crawmatic@pgbouncer:6432/crawmatic"
        )

    monkeypatch.setattr(database, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(database, "create_engine", _fake_create_engine)

    engine = database.get_auth_engine()

    assert engine is not None
    assert captured["url"] == _FakeSettings.AUTH_DATABASE_URL
    assert captured["kwargs"] == {
        "pool_pre_ping": True,
        "connect_args": {"prepare_threshold": None},
    }

    monkeypatch.setattr(database, "_auth_engine", None, raising=False)
