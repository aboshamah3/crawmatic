"""Workspace-scoped API-key management on the admin surface.

`POST/GET /v1/admin/workspaces/{workspace_id}/api-keys` +
`DELETE .../{api_key_id}` (phase4-connect Task 2). The SaaS control
plane holds only the static service token and each project's
`cmWorkspaceId` -- no JWT user -- so these live on the existing
service-token-gated admin router rather than the JWT-only
`/v1/api-keys` router. Follows `test_admin_router.py`'s fixtures
exactly: `TestClient(app)`, `require_service_token` and
`admin.get_admin_session` dependency overrides, `FakeOrmSession`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import admin
from app.service_auth import require_service_token
from unit._jobs_fake_session import FakeOrmSession

SERVICE_HEADERS = {"Authorization": "Bearer test-service-token"}


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def session() -> FakeOrmSession:
    return FakeOrmSession()


@pytest.fixture(autouse=True)
def _authorized(session: FakeOrmSession) -> None:
    app.dependency_overrides[require_service_token] = lambda: None
    app.dependency_overrides[admin.get_admin_session] = lambda: session


def _seed_workspace(session: FakeOrmSession) -> uuid.UUID:
    from app_shared.enums import WorkspaceStatus
    from app_shared.models.identity import Workspace

    ws = Workspace(
        id=uuid.uuid4(),
        name="Acme",
        slug=f"acme-{uuid.uuid4()}",
        status=WorkspaceStatus.ACTIVE,
    )
    session.seed(ws)
    return ws.id


def _seed_api_key(session: FakeOrmSession, workspace_id: uuid.UUID, **overrides):
    from app_shared.enums import ApiKeyStatus
    from app_shared.models.identity import ApiKey

    now = datetime.now(timezone.utc)
    base = dict(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name="existing-key",
        key_prefix="ck_abcdef",
        key_hash="deadbeefdeadbeef",
        scopes=["products:read"],
        status=ApiKeyStatus.ACTIVE,
        created_at=now,
        updated_at=now,
        last_used_at=None,
        revoked_at=None,
    )
    base.update(overrides)
    api_key = ApiKey(**base)
    session.seed(api_key)
    return api_key


# --- create ---------------------------------------------------------------


def test_create_returns_201_with_ck_prefixed_plaintext_key_once(client, session):
    workspace_id = _seed_workspace(session)
    resp = client.post(
        f"/v1/admin/workspaces/{workspace_id}/api-keys",
        json={"name": "storefront-key"},
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert uuid.UUID(body["id"])
    assert body["api_key"].startswith("ck_")
    assert body["status"] == "active"
    assert "key_hash" not in body


def test_create_persists_hash_not_plaintext(client, session):
    workspace_id = _seed_workspace(session)
    resp = client.post(
        f"/v1/admin/workspaces/{workspace_id}/api-keys",
        json={"name": "storefront-key"},
        headers=SERVICE_HEADERS,
    )
    plaintext = resp.json()["api_key"]
    keys = [o for o in session.added if type(o).__name__ == "ApiKey"]
    assert len(keys) == 1
    assert keys[0].key_hash != plaintext
    assert plaintext.startswith(keys[0].key_prefix)


def test_create_honours_supplied_name(client, session):
    workspace_id = _seed_workspace(session)
    resp = client.post(
        f"/v1/admin/workspaces/{workspace_id}/api-keys",
        json={"name": "my custom name"},
        headers=SERVICE_HEADERS,
    )
    assert resp.json()["name"] == "my custom name"


def test_create_uses_a_default_scope_set_when_scopes_omitted(client, session):
    workspace_id = _seed_workspace(session)
    resp = client.post(
        f"/v1/admin/workspaces/{workspace_id}/api-keys",
        json={"name": "storefront-key"},
        headers=SERVICE_HEADERS,
    )
    scopes = resp.json()["scopes"]
    assert scopes
    assert "products:read" in scopes


def test_create_honours_supplied_scopes(client, session):
    workspace_id = _seed_workspace(session)
    resp = client.post(
        f"/v1/admin/workspaces/{workspace_id}/api-keys",
        json={"name": "narrow-key", "scopes": ["products:read"]},
        headers=SERVICE_HEADERS,
    )
    assert resp.json()["scopes"] == ["products:read"]


def test_create_rejects_an_unknown_scope(client, session):
    workspace_id = _seed_workspace(session)
    resp = client.post(
        f"/v1/admin/workspaces/{workspace_id}/api-keys",
        json={"name": "bad-key", "scopes": ["not_a_real_scope"]},
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 422


def test_create_against_an_unknown_workspace_is_404(client, session):
    resp = client.post(
        f"/v1/admin/workspaces/{uuid.uuid4()}/api-keys",
        json={"name": "storefront-key"},
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 404


# --- list ------------------------------------------------------------------


def test_list_returns_items_without_key_hash_or_plaintext(client, session):
    workspace_id = _seed_workspace(session)
    _seed_api_key(session, workspace_id, name="k1")

    resp = client.get(
        f"/v1/admin/workspaces/{workspace_id}/api-keys", headers=SERVICE_HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert set(item) == {
        "id",
        "name",
        "key_prefix",
        "scopes",
        "status",
        "last_used_at",
        "revoked_at",
        "created_at",
    }


def test_list_is_scoped_to_the_path_workspace(client, session):
    workspace_id = _seed_workspace(session)
    other_workspace_id = _seed_workspace(session)
    _seed_api_key(session, workspace_id, name="mine")
    _seed_api_key(session, other_workspace_id, name="not-mine")

    resp = client.get(
        f"/v1/admin/workspaces/{workspace_id}/api-keys", headers=SERVICE_HEADERS
    )
    names = [item["name"] for item in resp.json()["items"]]
    assert names == ["mine"]


# --- revoke ------------------------------------------------------------------


def test_revoke_returns_204_and_sets_status_revoked_plus_revoked_at(client, session):
    workspace_id = _seed_workspace(session)
    api_key = _seed_api_key(session, workspace_id)

    resp = client.delete(
        f"/v1/admin/workspaces/{workspace_id}/api-keys/{api_key.id}",
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 204
    assert resp.content == b""
    assert api_key.status == "revoked"
    assert api_key.revoked_at is not None


def test_revoke_a_missing_key_is_204(client, session):
    workspace_id = _seed_workspace(session)
    resp = client.delete(
        f"/v1/admin/workspaces/{workspace_id}/api-keys/{uuid.uuid4()}",
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 204


def test_revoke_an_already_revoked_key_is_204(client, session):
    from app_shared.enums import ApiKeyStatus

    workspace_id = _seed_workspace(session)
    api_key = _seed_api_key(
        session,
        workspace_id,
        status=ApiKeyStatus.REVOKED,
        revoked_at=datetime.now(timezone.utc),
    )
    resp = client.delete(
        f"/v1/admin/workspaces/{workspace_id}/api-keys/{api_key.id}",
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 204


def test_revoke_a_key_belonging_to_a_different_workspace_is_204_not_404(
    client, session
):
    """The brief's **Interfaces** section is the exact-shapes contract Task 4
    codes against, and it is explicit: DELETE is IDEMPOTENT and a key
    belonging to another workspace returns 204, not 404 -- "Do NOT invent
    a 404 here" -- matching the existing `DELETE /v1/api-keys/{id}`
    convention (api_keys.py:162-164), which leaks no existence
    information. (Steps 2 and 4 elsewhere in the brief say "404" for this
    same case; that contradicts the Interfaces section, so the
    Interfaces section -- the one with the explicit rationale and the one
    Task 4 actually codes against -- wins. See task-2-report.md.)"""
    workspace_id = _seed_workspace(session)
    other_workspace_id = _seed_workspace(session)
    api_key = _seed_api_key(session, other_workspace_id)

    resp = client.delete(
        f"/v1/admin/workspaces/{workspace_id}/api-keys/{api_key.id}",
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 204
    assert api_key.status == "active"  # untouched -- belongs to the other workspace


# --- auth --------------------------------------------------------------------


def test_all_three_routes_require_the_service_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the override, the real `require_service_token` seam must
    refuse all three routes (mirrors
    `test_admin_router.test_admin_routes_require_the_service_token`)."""

    class _Settings:
        SAAS_SERVICE_TOKEN = "s3cret-service-token"

    monkeypatch.setattr("app.service_auth.get_settings", lambda: _Settings())
    app.dependency_overrides.clear()
    workspace_id = uuid.uuid4()
    api_key_id = uuid.uuid4()
    with TestClient(app) as bare:
        create_resp = bare.post(
            f"/v1/admin/workspaces/{workspace_id}/api-keys", json={"name": "x"}
        )
        list_resp = bare.get(f"/v1/admin/workspaces/{workspace_id}/api-keys")
        delete_resp = bare.delete(
            f"/v1/admin/workspaces/{workspace_id}/api-keys/{api_key_id}"
        )
    assert create_resp.status_code == 401
    assert list_resp.status_code == 401
    assert delete_resp.status_code == 401
