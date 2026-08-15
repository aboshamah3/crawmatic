"""`GET /version` — release/migration provenance (audit §C2, required fix #2).

Unauthenticated like `/health`; unlike `/health` it reads the DB (one
system table, `alembic_version`), so these tests override the router's
own `_get_db_session` dependency with a tiny fake rather than a real
engine/session, and monkeypatch `app.routers.version._code_migration_head`
directly to decouple from the repo's actual current migration head.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import version


class _FakeRow:
    def __init__(self, value: str) -> None:
        self._value = value

    def __getitem__(self, index: int) -> str:
        assert index == 0
        return self._value


class _FakeResult:
    def __init__(self, row: _FakeRow | None) -> None:
        self._row = row

    def first(self) -> _FakeRow | None:
        return self._row


class _FakeSession:
    def __init__(self, *, row: _FakeRow | None = None, raises: Exception | None = None) -> None:
        self._row = row
        self._raises = raises

    def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
        if self._raises is not None:
            raise self._raises
        return _FakeResult(self._row)


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _override_session(session: _FakeSession) -> None:
    def _fake_dependency() -> Iterator[_FakeSession]:
        yield session

    app.dependency_overrides[version._get_db_session] = _fake_dependency


def test_version_requires_no_auth_header(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version, "_code_migration_head", lambda: "abc123")
    _override_session(_FakeSession(row=_FakeRow("abc123")))

    resp = client.get("/version")

    assert resp.status_code == 200


def test_version_reports_matching_heads(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version, "_code_migration_head", lambda: "abc123")
    _override_session(_FakeSession(row=_FakeRow("abc123")))

    body = client.get("/version").json()

    assert body["code_migration_head"] == "abc123"
    assert body["db_migration_head"] == "abc123"
    assert body["migration_heads_match"] is True
    assert body["db_error"] is None


def test_version_reports_mismatched_heads(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version, "_code_migration_head", lambda: "abc123")
    _override_session(_FakeSession(row=_FakeRow("older999")))

    body = client.get("/version").json()

    assert body["migration_heads_match"] is False


def test_version_degrades_gracefully_when_db_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(version, "_code_migration_head", lambda: "abc123")
    _override_session(_FakeSession(raises=RuntimeError("connection refused")))

    resp = client.get("/version")
    body = resp.json()

    assert resp.status_code == 200
    assert body["db_migration_head"] is None
    assert body["migration_heads_match"] is None
    # The exception class name is reported; the raw message (which could
    # carry a hostname/credential in a real deployment) is never echoed back.
    assert body["db_error"] == "RuntimeError"
    assert "connection refused" not in resp.text


def test_version_reports_null_when_code_head_unresolvable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(version, "_code_migration_head", lambda: None)
    _override_session(_FakeSession(row=_FakeRow("abc123")))

    body = client.get("/version").json()

    assert body["code_migration_head"] is None
    assert body["migration_heads_match"] is None


def test_git_sha_prefers_git_sha_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_SHA", "own-sha")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "railway-sha")
    assert version._git_sha() == "own-sha"


def test_git_sha_falls_back_to_railway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "railway-sha")
    assert version._git_sha() == "railway-sha"


def test_git_sha_falls_back_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    assert version._git_sha() == "unknown"


def test_version_path_excluded_from_admin_internal_tags() -> None:
    """Sanity: `/version` isn't accidentally tagged as an internal surface."""
    from app.openapi_public import INTERNAL_TAGS

    assert "version" not in INTERNAL_TAGS
