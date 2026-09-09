"""Connector-scoped API keys minted for WordPress connectors (audit P0.3).

`POST /v1/admin/workspaces/{workspace_id}/connector-keys` mints a
narrowly-scoped key (`CONNECTOR_SCOPES` -- read catalog, manage
price comparisons, competitors and matches, and run the plugin's own
live price check -- never the full `BOOTSTRAP_SCOPES`) for a
store connector that lives in WordPress. `POST
/v1/admin/api-keys/{key_id}/revoke` revokes any admin-minted key by id.
Follows `test_admin_api_keys.py`'s fixtures exactly: `TestClient(app)`,
`require_service_token`/`admin.get_admin_session` dependency overrides,
`FakeOrmSession`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import admin
from app.routers.admin import CONNECTOR_SCOPES
from app.service_auth import require_service_token
from app_shared.enums import ApiKeyStatus
from unit._jobs_fake_session import FakeOrmSession

SERVICE_HEADERS = {"Authorization": "Bearer test-service-token"}


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def session() -> FakeOrmSession:
    return FakeOrmSession()


@pytest.fixture(autouse=True)
def _authorized(session: FakeOrmSession) -> None:
    app.dependency_overrides[require_service_token] = lambda: None
    app.dependency_overrides[admin.get_admin_session] = lambda: session


@pytest.fixture()
def admin_client(session: FakeOrmSession) -> TestClient:
    client = TestClient(app)
    client.headers.update(SERVICE_HEADERS)
    return client


@pytest.fixture()
def workspace(session: FakeOrmSession):
    from app_shared.enums import WorkspaceStatus
    from app_shared.models.identity import Workspace

    ws = Workspace(
        id=uuid.uuid4(),
        name="Acme",
        slug=f"acme-{uuid.uuid4()}",
        status=WorkspaceStatus.ACTIVE,
    )
    session.seed(ws)
    return ws


def _get_api_key(session: FakeOrmSession, key_id: str):
    """Session lookup helper -- scans `session.added` (mirrors
    `test_admin_api_keys.py`'s `[o for o in session.added if
    type(o).__name__ == "ApiKey"]` idiom) for the minted row."""
    target = uuid.UUID(str(key_id))
    for obj in session.added:
        if type(obj).__name__ == "ApiKey" and obj.id == target:
            return obj
    raise AssertionError(f"no ApiKey with id {key_id} in session.added")


def test_connector_key_minted_with_the_deliberate_r03_scope_set(
    admin_client, session, workspace
):
    """The minted set is exactly `CONNECTOR_SCOPES` -- live check included.

    This test used to assert `"jobs:write" not in key.scopes`, which pinned
    the PRE-R03 decision: the connector key deliberately carried no jobs
    scope at all. Review R03 (2026-09-09) took the opposite decision and
    granted `jobs:read` + `jobs:write`, because the plugin's shipped
    "refresh prices now" action posts `POST /v1/variants/{id}/rescrape`
    (`jobs:write`) and then polls `GET /v1/jobs/{job_id}` (`jobs:read`) --
    so a correctly paired plugin 403'd on that feature, on every store.

    So the pin FLIPS rather than disappears, and it still guards from both
    sides: the two live-check scopes must be present, and the destructive /
    SaaS-worker scopes must still be absent (`jobs:cancel` terminalizes rows
    nobody can get back; `webhooks:write` is worker business). Widening for
    one feature must not become widening by habit -- the same pairing
    `tests/integration/test_api_contract.py` now pins route-side.

    What makes the grant defensible is the blast-radius bound, not the scope
    name: both routes resolve every row through
    `scoped_get(..., principal.workspace_id)`, so a key minted for one
    workspace cannot start or poll work in another (proved from both sides
    in `tests/unit/test_connector_key_live_checks.py`). And it does not
    widen keys already minted -- `create_connector_key` COPIES the list onto
    the `api_keys` row -- so stores paired before the deploy keep the old
    set until reissued; see `docs/ops/CONNECTOR_KEY_REISSUE.md`.
    """
    resp = admin_client.post(
        f"/v1/admin/workspaces/{workspace.id}/connector-keys",
        json={"connector": "woo"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["api_key"].startswith(body["key_prefix"])
    key = _get_api_key(session, body["key_id"])
    assert key.scopes == CONNECTOR_SCOPES
    assert "alerts:read" in key.scopes
    assert {"jobs:read", "jobs:write"} <= set(key.scopes), (
        "the connector key lost the R03 live-check scopes -- the plugin's "
        "'refresh prices now' would 403 on every store"
    )
    assert "jobs:cancel" not in key.scopes
    assert "webhooks:write" not in key.scopes


def test_connector_key_name_includes_connector_and_workspace(
    admin_client, session, workspace
):
    resp = admin_client.post(
        f"/v1/admin/workspaces/{workspace.id}/connector-keys",
        json={"connector": "woo"},
    )
    key = _get_api_key(session, resp.json()["key_id"])
    assert key.name == f"connector:woo:{workspace.id}"


def test_connector_key_hash_is_stored_not_plaintext(admin_client, session, workspace):
    resp = admin_client.post(
        f"/v1/admin/workspaces/{workspace.id}/connector-keys",
        json={"connector": "woo"},
    )
    body = resp.json()
    key = _get_api_key(session, body["key_id"])
    assert key.key_hash != body["api_key"]
    assert body["api_key"].startswith(key.key_prefix)


def test_connector_key_revoke(admin_client, session, workspace):
    minted = admin_client.post(
        f"/v1/admin/workspaces/{workspace.id}/connector-keys",
        json={"connector": "woo"},
    ).json()

    resp = admin_client.post(f"/v1/admin/api-keys/{minted['key_id']}/revoke")
    assert resp.status_code == 200
    body = resp.json()
    assert body["key_id"] == minted["key_id"]
    assert body["status"] == "revoked"
    assert _get_api_key(session, minted["key_id"]).status == ApiKeyStatus.REVOKED


def test_connector_key_unknown_workspace_404(admin_client):
    resp = admin_client.post(
        "/v1/admin/workspaces/00000000-0000-0000-0000-000000000000/connector-keys",
        json={"connector": "woo"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_revoke_unknown_key_is_404(admin_client):
    resp = admin_client.post(f"/v1/admin/api-keys/{uuid.uuid4()}/revoke")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_connector_routes_require_the_service_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Settings:
        SAAS_SERVICE_TOKEN = "s3cret-service-token"

    monkeypatch.setattr("app.service_auth.get_settings", lambda: _Settings())
    app.dependency_overrides.clear()
    workspace_id = uuid.uuid4()
    key_id = uuid.uuid4()
    with TestClient(app) as bare:
        create_resp = bare.post(
            f"/v1/admin/workspaces/{workspace_id}/connector-keys",
            json={"connector": "woo"},
        )
        revoke_resp = bare.post(f"/v1/admin/api-keys/{key_id}/revoke")
    assert create_resp.status_code == 401
    assert revoke_resp.status_code == 401
