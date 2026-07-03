"""SPEC-04 US1 wiring smoke test: products/variants routes exist and are
scope-gated (T017-T019 verification).

DB-independent — no live Postgres, no real auth. `require_scopes(...)`
raises its 403 *before* any route handler body runs (`app.deps` T017),
so overriding `get_current_principal` with a fake, DB-less principal is
sufficient to prove the gate itself without touching a session.

Full US4 scope-gating coverage (every route x every scope, via app
route/dependency inspection) lands with T030 in Phase 5 — this test only
proves the SPEC-04 US1 wiring the orchestrator's verify step asks for:
routes registered under `/v1`, and a wrong-scope / missing-auth call is
rejected.
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


def test_products_and_variants_routes_are_registered(client: TestClient) -> None:
    openapi = client.get("/openapi.json").json()
    paths = openapi["paths"]

    assert "/v1/products" in paths
    assert set(paths["/v1/products"]) >= {"post", "get"}

    assert "/v1/products/{product_id}" in paths
    assert set(paths["/v1/products/{product_id}"]) >= {"get", "patch", "delete"}

    assert "/v1/variants" in paths
    assert "get" in paths["/v1/variants"]

    assert "/v1/variants/{variant_id}" in paths
    assert set(paths["/v1/variants/{variant_id}"]) >= {"get", "patch"}

    # No standalone variant create/delete (contracts/api-variants.md).
    assert "post" not in paths["/v1/variants"]
    assert "delete" not in paths["/v1/variants/{variant_id}"]


# --- unauthenticated -> 401 -----------------------------------------------


def test_post_products_with_no_authorization_header_is_401(client: TestClient) -> None:
    resp = client.post("/v1/products", json={"title": "Widget"})
    assert resp.status_code == 401


def test_get_variants_with_no_authorization_header_is_401(client: TestClient) -> None:
    resp = client.get("/v1/variants")
    assert resp.status_code == 401


# --- wrong scope -> 403 ---------------------------------------------------


def test_post_products_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["products:read"]
    )
    resp = client.post(
        "/v1/products",
        json={"title": "Widget", "price": "9.99", "currency": "USD"},
    )
    assert resp.status_code == 403


def test_get_products_without_read_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes([])
    resp = client.get("/v1/products")
    assert resp.status_code == 403


def test_delete_product_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["products:read"]
    )
    resp = client.delete(f"/v1/products/{uuid.uuid4()}")
    assert resp.status_code == 403


def test_patch_variant_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["variants:read"]
    )
    resp = client.patch(f"/v1/variants/{uuid.uuid4()}", json={"title": "x"})
    assert resp.status_code == 403


def test_get_variants_without_read_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes([])
    resp = client.get("/v1/variants")
    assert resp.status_code == 403
