"""Unit tests for the auth + workspace-context dependency (SPEC-03 T045,
FR-017/FR-020a/FR-024).

`apps/api/app/deps.py` is the single auth seam for every protected `/v1`
endpoint. These tests exercise its wiring with fakes/monkeypatches — no
live Postgres/Redis — covering: JWT vs api-key credential routing;
missing/expired token -> 401; a suspended cached status -> deny;
SUPER_ADMIN with no `X-Workspace-Id` -> rejected; a non-super principal
assuming a workspace other than its own -> 403; and `require_scopes(...)`
raising 403 when a required scope is missing.

`get_current_principal` is a generator-based FastAPI dependency (it
`yield`s inside a `with get_session() as session:` block) — exercised
here by driving the generator protocol directly (`next(gen)`), with
`get_session`/`set_workspace_context` monkeypatched to an in-memory fake
so no real engine/connection is ever touched.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

import app.deps as deps
from app_shared.enums import ApiKeyStatus, UserRole


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
    """Advance a `get_current_principal` generator past its yield (triggers commit).

    Resuming after the yield runs `session.commit()` and the function body
    ends, which raises `StopIteration` — the normal, expected way a
    generator-based dependency finishes. Anything else propagates.
    """
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


# --- credential-path routing --------------------------------------------


def test_jwt_credential_routes_to_jwt_path_not_api_key_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()

    def _fake_decode(token, *, secret, algorithm):
        assert token == "a.jwt.token"
        return {
            "sub": str(user_id),
            "workspace_id": str(workspace_id),
            "role": "workspace_admin",
            "type": "access",
        }

    def _never_called(*args, **kwargs):
        raise AssertionError("api-key lookup must not be called for a JWT credential")

    monkeypatch.setattr(deps, "decode_access_token", _fake_decode)
    monkeypatch.setattr(deps, "_lookup_api_key_candidates", _never_called)
    monkeypatch.setattr(deps, "get_user_status", lambda *a, **k: "active")
    monkeypatch.setattr(deps, "get_workspace_status", lambda *a, **k: "active")

    gen = deps.get_current_principal(authorization="Bearer a.jwt.token", x_workspace_id=None)
    session, principal = next(gen)

    assert principal.kind == "user"
    assert principal.id == user_id
    assert principal.workspace_id == workspace_id
    assert principal.role == UserRole.WORKSPACE_ADMIN
    _drain(gen)


def test_ck_prefixed_credential_routes_to_api_key_path_not_jwt_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_id = uuid.uuid4()
    key_workspace_id = uuid.uuid4()

    class _FakeApiKey:
        id = key_id
        key_hash = "irrelevant-because-verify_api_key-is-faked"
        status = ApiKeyStatus.ACTIVE
        workspace_id = key_workspace_id
        scopes = ["products:read"]

    def _never_called(*args, **kwargs):
        raise AssertionError("JWT decode must not be called for a ck_-prefixed credential")

    monkeypatch.setattr(deps, "decode_access_token", _never_called)
    monkeypatch.setattr(deps, "_lookup_api_key_candidates", lambda prefix: [_FakeApiKey()])
    monkeypatch.setattr(deps, "verify_api_key", lambda credential, key_hash: True)
    monkeypatch.setattr(deps, "get_workspace_status", lambda *a, **k: "active")
    monkeypatch.setattr(deps, "should_write_last_used", lambda *a, **k: False)

    gen = deps.get_current_principal(authorization="Bearer ck_abcdef1234567890", x_workspace_id=None)
    session, principal = next(gen)

    assert principal.kind == "api_key"
    assert principal.id == key_id
    assert principal.workspace_id == key_workspace_id
    assert principal.scopes == ["products:read"]
    _drain(gen)


# --- missing / expired token -> 401 --------------------------------------


def test_missing_authorization_header_is_401() -> None:
    gen = deps.get_current_principal(authorization=None, x_workspace_id=None)
    with pytest.raises(HTTPException) as exc_info:
        next(gen)
    assert exc_info.value.status_code == 401


def test_non_bearer_authorization_header_is_401() -> None:
    gen = deps.get_current_principal(authorization="Basic abc123", x_workspace_id=None)
    with pytest.raises(HTTPException) as exc_info:
        next(gen)
    assert exc_info.value.status_code == 401


def test_expired_jwt_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_expired(token, *, secret, algorithm):
        raise Exception("simulated ExpiredSignatureError")

    monkeypatch.setattr(deps, "decode_access_token", _raise_expired)

    gen = deps.get_current_principal(authorization="Bearer expired.jwt.token", x_workspace_id=None)
    with pytest.raises(HTTPException) as exc_info:
        next(gen)
    assert exc_info.value.status_code == 401


# --- suspended cached status -> deny -------------------------------------


def test_suspended_user_status_denies_with_401(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()

    monkeypatch.setattr(
        deps,
        "decode_access_token",
        lambda token, *, secret, algorithm: {
            "sub": str(user_id),
            "workspace_id": None,
            "role": "super_admin",
            "type": "access",
        },
    )
    monkeypatch.setattr(deps, "get_user_status", lambda *a, **k: "suspended")

    gen = deps.get_current_principal(authorization="Bearer a.jwt.token", x_workspace_id=None)
    with pytest.raises(HTTPException) as exc_info:
        next(gen)
    assert exc_info.value.status_code == 401


def test_suspended_workspace_status_denies_with_401(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()

    monkeypatch.setattr(
        deps,
        "decode_access_token",
        lambda token, *, secret, algorithm: {
            "sub": str(user_id),
            "workspace_id": str(workspace_id),
            "role": "workspace_admin",
            "type": "access",
        },
    )
    monkeypatch.setattr(deps, "get_user_status", lambda *a, **k: "active")
    monkeypatch.setattr(deps, "get_workspace_status", lambda *a, **k: "suspended")

    gen = deps.get_current_principal(authorization="Bearer a.jwt.token", x_workspace_id=None)
    with pytest.raises(HTTPException) as exc_info:
        next(gen)
    assert exc_info.value.status_code == 401


# --- SUPER_ADMIN workspace assumption ------------------------------------


def test_super_admin_without_x_workspace_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()

    monkeypatch.setattr(
        deps,
        "decode_access_token",
        lambda token, *, secret, algorithm: {
            "sub": str(user_id),
            "workspace_id": None,
            "role": "super_admin",
            "type": "access",
        },
    )
    monkeypatch.setattr(deps, "get_user_status", lambda *a, **k: "active")

    gen = deps.get_current_principal(authorization="Bearer a.jwt.token", x_workspace_id=None)
    with pytest.raises(HTTPException) as exc_info:
        next(gen)
    assert exc_info.value.status_code == 403


def test_super_admin_with_x_workspace_id_is_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    target_workspace_id = uuid.uuid4()

    monkeypatch.setattr(
        deps,
        "decode_access_token",
        lambda token, *, secret, algorithm: {
            "sub": str(user_id),
            "workspace_id": None,
            "role": "super_admin",
            "type": "access",
        },
    )
    monkeypatch.setattr(deps, "get_user_status", lambda *a, **k: "active")
    monkeypatch.setattr(deps, "get_workspace_status", lambda *a, **k: "active")

    gen = deps.get_current_principal(
        authorization="Bearer a.jwt.token", x_workspace_id=str(target_workspace_id)
    )
    session, principal = next(gen)
    assert principal.workspace_id == target_workspace_id
    _drain(gen)


# --- non-super assuming a workspace other than its own -> 403 -----------


def test_non_super_assuming_another_workspace_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    own_workspace_id = uuid.uuid4()
    other_workspace_id = uuid.uuid4()
    assert own_workspace_id != other_workspace_id

    monkeypatch.setattr(
        deps,
        "decode_access_token",
        lambda token, *, secret, algorithm: {
            "sub": str(user_id),
            "workspace_id": str(own_workspace_id),
            "role": "workspace_admin",
            "type": "access",
        },
    )
    monkeypatch.setattr(deps, "get_user_status", lambda *a, **k: "active")
    monkeypatch.setattr(deps, "get_workspace_status", lambda *a, **k: "active")

    gen = deps.get_current_principal(
        authorization="Bearer a.jwt.token", x_workspace_id=str(other_workspace_id)
    )
    with pytest.raises(HTTPException) as exc_info:
        next(gen)
    assert exc_info.value.status_code == 403


def test_non_super_may_explicitly_assume_its_own_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    own_workspace_id = uuid.uuid4()

    monkeypatch.setattr(
        deps,
        "decode_access_token",
        lambda token, *, secret, algorithm: {
            "sub": str(user_id),
            "workspace_id": str(own_workspace_id),
            "role": "read_only",
            "type": "access",
        },
    )
    monkeypatch.setattr(deps, "get_user_status", lambda *a, **k: "active")
    monkeypatch.setattr(deps, "get_workspace_status", lambda *a, **k: "active")

    gen = deps.get_current_principal(
        authorization="Bearer a.jwt.token", x_workspace_id=str(own_workspace_id)
    )
    session, principal = next(gen)
    assert principal.workspace_id == own_workspace_id
    _drain(gen)


# --- require_scopes -------------------------------------------------------


def test_require_scopes_raises_403_when_a_required_scope_is_missing() -> None:
    principal = deps.Principal(
        kind="api_key",
        id=uuid.uuid4(),
        role=None,
        scopes=["products:read"],
        workspace_id=uuid.uuid4(),
    )
    check = deps.require_scopes("products:read", "products:write")

    with pytest.raises(HTTPException) as exc_info:
        check(principal_ctx=(_FakeSession(), principal))
    assert exc_info.value.status_code == 403


def test_require_scopes_passes_when_all_scopes_are_granted() -> None:
    principal = deps.Principal(
        kind="api_key",
        id=uuid.uuid4(),
        role=None,
        scopes=["products:read", "products:write"],
        workspace_id=uuid.uuid4(),
    )
    check = deps.require_scopes("products:read")

    session = _FakeSession()
    result = check(principal_ctx=(session, principal))
    assert result == (session, principal)
