"""Every error response carries one top-level `{"error": {...}}` (PLAN §7.4)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.deps import Principal, get_current_principal
from app.main import app
from unit._jobs_fake_session import FakeOrmSession

WORKSPACE_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _principal(scopes: list[str]):
    session = FakeOrmSession()

    def _dep() -> Iterator[tuple[FakeOrmSession, Principal]]:
        yield session, Principal(
            kind="api_key",
            id=uuid.uuid4(),
            role=None,
            scopes=scopes,
            workspace_id=WORKSPACE_ID,
        )

    return _dep


def test_401_has_top_level_error_object(client):
    resp = client.get("/v1/products")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_FAILED"
    assert "message" in resp.json()["error"]


def test_403_has_top_level_error_object(client):
    app.dependency_overrides[get_current_principal] = _principal([])
    resp = client.get("/v1/products")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_404_has_top_level_error_object(client):
    app.dependency_overrides[get_current_principal] = _principal(
        ["products:read"]
    )
    resp = client.get(f"/v1/products/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_legacy_detail_shape_is_preserved(client):
    """1768 existing tests read `detail.error.code` / `detail.code`."""
    app.dependency_overrides[get_current_principal] = _principal(
        ["products:read"]
    )
    resp = client.get(f"/v1/products/{uuid.uuid4()}")
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_request_validation_error_is_enveloped(client):
    app.dependency_overrides[get_current_principal] = _principal(
        ["products:read"]
    )
    resp = client.get("/v1/products/not-a-uuid")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert isinstance(resp.json()["detail"], list)


def test_error_code_is_a_stable_string_for_an_unlabelled_http_error(client):
    from fastapi import HTTPException

    @app.get("/_test_plain_error")
    def _plain():
        raise HTTPException(status_code=418, detail="I am a teapot")

    try:
        resp = client.get("/_test_plain_error")
        assert resp.status_code == 418
        assert resp.json()["error"]["code"] == "HTTP_418"
        assert resp.json()["error"]["message"] == "I am a teapot"
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", "") != "/_test_plain_error"
        ]
