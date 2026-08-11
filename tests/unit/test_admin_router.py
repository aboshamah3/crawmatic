"""`/v1/admin/workspaces` — SaaS provisioning surface (PLAN §7.1)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

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


def test_provision_returns_workspace_id_and_plaintext_key(client, session):
    resp = client.post(
        "/v1/admin/workspaces",
        json={"name": "Acme Store", "external_ref": "proj_123"},
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert uuid.UUID(body["workspace_id"])
    assert body["api_key"].startswith("ck_")
    assert body["external_ref"] == "proj_123"


def test_provision_persists_workspace_and_api_key(client, session):
    client.post(
        "/v1/admin/workspaces",
        json={"name": "Acme Store", "external_ref": "proj_123"},
        headers=SERVICE_HEADERS,
    )
    added_types = {type(obj).__name__ for obj in session.added}
    assert "Workspace" in added_types
    assert "ApiKey" in added_types


def test_provision_key_hash_is_stored_not_plaintext(client, session):
    resp = client.post(
        "/v1/admin/workspaces",
        json={"name": "Acme Store", "external_ref": "proj_124"},
        headers=SERVICE_HEADERS,
    )
    plaintext = resp.json()["api_key"]
    keys = [o for o in session.added if type(o).__name__ == "ApiKey"]
    assert len(keys) == 1
    assert keys[0].key_hash != plaintext
    assert plaintext.startswith(keys[0].key_prefix)


def test_provision_rejects_extra_fields(client):
    resp = client.post(
        "/v1/admin/workspaces",
        json={"name": "Acme", "external_ref": "p1", "sneaky": True},
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 422


def test_provision_requires_external_ref(client):
    resp = client.post(
        "/v1/admin/workspaces", json={"name": "Acme"}, headers=SERVICE_HEADERS
    )
    assert resp.status_code == 422


def test_archive_sets_status_suspended(client, session):
    """`WorkspaceStatus` has no `ARCHIVED` member (only ACTIVE/SUSPENDED) —
    see `app.routers.admin` module docstring for the substitution."""
    from app_shared.enums import WorkspaceStatus
    from app_shared.models.identity import Workspace

    ws = Workspace(
        id=uuid.uuid4(), name="Acme", slug="acme", status=WorkspaceStatus.ACTIVE
    )
    session.seed(ws)

    resp = client.post(
        f"/v1/admin/workspaces/{ws.id}/archive", headers=SERVICE_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "suspended"
    assert ws.status == WorkspaceStatus.SUSPENDED


def test_archive_unknown_workspace_is_404(client, session):
    resp = client.post(
        f"/v1/admin/workspaces/{uuid.uuid4()}/archive", headers=SERVICE_HEADERS
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_admin_routes_require_the_service_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the dependency override the real seam must refuse.

    `require_service_token` isn't overridden here (only
    `admin.get_admin_session` would be, and this test doesn't even set
    that) -- it calls the real `app.service_auth.get_settings`, so that
    one call is monkeypatched the same way `test_service_auth.py` does,
    to avoid needing a full `Settings()` (DATABASE_URL, REDIS_URL, ...)
    in the unit-test environment.
    """

    class _Settings:
        SAAS_SERVICE_TOKEN = "s3cret-service-token"

    monkeypatch.setattr("app.service_auth.get_settings", lambda: _Settings())
    app.dependency_overrides.clear()
    with TestClient(app) as bare:
        resp = bare.post(
            "/v1/admin/workspaces", json={"name": "x", "external_ref": "y"}
        )
    assert resp.status_code == 401
