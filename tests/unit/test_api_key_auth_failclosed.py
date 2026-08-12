"""API-key auth must fail closed (401) when an `api_keys` row cannot be
materialised, not fail open into a 500 (Phase 4 Connect, Task 1).

`_lookup_api_key_candidates` (`app.deps`) executes a plain `select(ApiKey)`.
`ApiKey.status` is an `enum_column(ApiKeyStatus)` (`app_shared.enums`) whose
`process_result_value` raises `ValueError` for any stored value outside
`{"active", "revoked"}` — e.g. a row corrupted to `status='REVOKED'`
(wrong case) or any other out-of-set string. That `ValueError` is raised
while SQLAlchemy materialises the row, i.e. *before* the
`matched.status != ApiKeyStatus.ACTIVE` check ever runs, so it used to
escape `_authenticate_api_key` entirely and surface as an unhandled 500.

Mirrors `tests/unit/test_deps.py`: monkeypatch-based, no database, drives
`get_current_principal` directly via `next(gen)`. There is no
`conftest.py` in this repo, so fixtures are declared here.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.deps as deps
from app.main import app
from app_shared.enums import ApiKeyStatus

# Unmistakably synthetic — never a real credential.
SYNTHETIC_CREDENTIAL = "ck_test_0000000000000000000000000000"


class _FakeSession:
    def __init__(self) -> None:
        self.committed = False

    def commit(self) -> None:
        self.committed = True


@contextmanager
def _fake_get_session():
    yield _FakeSession()


def _fake_set_workspace_context(session, workspace_id) -> None:  # noqa: ANN001
    session.workspace_id = workspace_id  # type: ignore[attr-defined]


def _drain(gen):
    with pytest.raises(StopIteration):
        next(gen)


class _FakeSettings:
    JWT_SECRET = "test-jwt-secret"
    JWT_ALGORITHM = "HS256"
    API_KEY_LAST_USED_THROTTLE_SECONDS = 60


@pytest.fixture(autouse=True)
def _patch_session_plumbing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "get_session", _fake_get_session)
    monkeypatch.setattr(deps, "set_workspace_context", _fake_set_workspace_context)
    monkeypatch.setattr(deps, "get_redis_client", lambda: object())
    monkeypatch.setattr(deps, "get_settings", lambda: _FakeSettings())


class _FakeApiKey:
    def __init__(self, *, status: ApiKeyStatus, key_hash: str = "irrelevant") -> None:
        self.id = uuid.uuid4()
        self.key_hash = key_hash
        self.status = status
        self.workspace_id = uuid.uuid4()
        self.scopes = ["products:read"]


# --- unit-level: drive the dependency generator directly -----------------


def test_valid_active_api_key_authenticates(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = _FakeApiKey(status=ApiKeyStatus.ACTIVE)
    monkeypatch.setattr(deps, "_lookup_api_key_candidates", lambda prefix: [candidate])
    monkeypatch.setattr(deps, "verify_api_key", lambda credential, key_hash: True)
    monkeypatch.setattr(deps, "get_workspace_status", lambda *a, **k: "active")
    monkeypatch.setattr(deps, "should_write_last_used", lambda *a, **k: False)

    gen = deps.get_current_principal(
        authorization=f"Bearer {SYNTHETIC_CREDENTIAL}", x_workspace_id=None
    )
    session, principal = next(gen)

    assert principal.kind == "api_key"
    assert principal.id == candidate.id
    _drain(gen)


def test_revoked_api_key_status_is_401_auth_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard against over-reach: a cleanly-typed REVOKED status must still 401."""
    candidate = _FakeApiKey(status=ApiKeyStatus.REVOKED)
    monkeypatch.setattr(deps, "_lookup_api_key_candidates", lambda prefix: [candidate])
    monkeypatch.setattr(deps, "verify_api_key", lambda credential, key_hash: True)

    gen = deps.get_current_principal(
        authorization=f"Bearer {SYNTHETIC_CREDENTIAL}", x_workspace_id=None
    )
    with pytest.raises(HTTPException) as exc_info:
        next(gen)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "AUTH_FAILED"


def test_uncoercible_status_on_row_load_is_401_not_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual regression: the row load itself raises ValueError
    (simulating `_AppValidatedEnumString.process_result_value` choking on
    an out-of-set stored value) — this must surface as 401 AUTH_FAILED,
    not propagate as an unhandled exception.
    """

    def _raise_value_error(prefix: str):
        raise ValueError(
            "'REVOKED' is not a valid ApiKeyStatus value (expected one of: active, revoked)"
        )

    monkeypatch.setattr(deps, "_lookup_api_key_candidates", _raise_value_error)

    gen = deps.get_current_principal(
        authorization=f"Bearer {SYNTHETIC_CREDENTIAL}", x_workspace_id=None
    )
    with pytest.raises(HTTPException) as exc_info:
        next(gen)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "AUTH_FAILED"


# --- HTTP-layer: assert the 500-vs-401 distinction where it matters -----


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_http_layer_out_of_set_status_is_401_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_value_error(prefix: str):
        raise ValueError(
            "'REVOKED' is not a valid ApiKeyStatus value (expected one of: active, revoked)"
        )

    monkeypatch.setattr(deps, "_lookup_api_key_candidates", _raise_value_error)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        "/v1/products",
        headers={"Authorization": f"Bearer {SYNTHETIC_CREDENTIAL}"},
    )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_FAILED"
