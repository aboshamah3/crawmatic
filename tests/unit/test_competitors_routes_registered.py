"""SPEC-05 US1 wiring smoke test: `/v1/competitors` routes exist and are
scope-gated (T010-T012 verification).

DB-independent — no live Postgres, no real auth. `require_scopes(...)`
raises its 403 *before* any route handler body runs (`app.deps`), so
overriding `get_current_principal` with a fake, DB-less principal is
sufficient to prove the gate itself without touching a session. Mirrors
`tests/unit/test_catalog_routes_registered.py` (SPEC-04 US1).

Full US4 scope-gating coverage (every competitors/matches route x every
scope, via app route/dependency inspection) lands with T028 in Phase 6 —
this test only proves the US1 wiring: routes registered under `/v1`, and
a wrong-scope / missing-auth call is rejected before touching the DB.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.deps import Principal, get_current_principal
from app.main import app


class _FakeSession:
    """A session stand-in that must never be touched — `require_scopes`
    rejects before the route handler (and thus before any session use)
    runs, for every case exercised here."""


def _fake_principal_with_scopes(scopes: list[str]):
    def _dependency() -> Iterator[tuple[_FakeSession, Principal]]:
        yield _FakeSession(), Principal(
            kind="api_key",
            id=uuid.uuid4(),
            role=None,
            scopes=scopes,
            workspace_id=uuid.uuid4(),
        )

    return _dependency


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# --- routes registered -------------------------------------------------


def test_competitors_routes_are_registered(client: TestClient) -> None:
    openapi = client.get("/openapi.json").json()
    paths = openapi["paths"]

    assert "/v1/competitors" in paths
    assert set(paths["/v1/competitors"]) >= {"post", "get"}

    assert "/v1/competitors/{competitor_id}" in paths
    assert set(paths["/v1/competitors/{competitor_id}"]) >= {"get", "patch", "delete"}


# --- unauthenticated -> 401 -----------------------------------------------


def test_post_competitors_with_no_authorization_header_is_401(client: TestClient) -> None:
    resp = client.post("/v1/competitors", json={"name": "Acme", "domain": "acme.example.com"})
    assert resp.status_code == 401


def test_get_competitors_with_no_authorization_header_is_401(client: TestClient) -> None:
    resp = client.get("/v1/competitors")
    assert resp.status_code == 401


# --- wrong scope -> 403 ---------------------------------------------------


def test_post_competitors_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["competitors:read"]
    )
    resp = client.post(
        "/v1/competitors", json={"name": "Acme", "domain": "acme.example.com"}
    )
    assert resp.status_code == 403


def test_get_competitors_without_read_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes([])
    resp = client.get("/v1/competitors")
    assert resp.status_code == 403


def test_get_competitor_by_id_without_read_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes([])
    resp = client.get(f"/v1/competitors/{uuid.uuid4()}")
    assert resp.status_code == 403


def test_patch_competitor_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["competitors:read"]
    )
    resp = client.patch(f"/v1/competitors/{uuid.uuid4()}", json={"name": "x"})
    assert resp.status_code == 403


def test_delete_competitor_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["competitors:read"]
    )
    resp = client.delete(f"/v1/competitors/{uuid.uuid4()}")
    assert resp.status_code == 403
