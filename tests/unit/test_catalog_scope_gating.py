"""Catalog scope-gating unit tests (SPEC-04 T030, FR-016/SC-005).

Every catalog route (products, variants, product-groups) must declare
the correct `require_scopes(...)` — read-family routes require
`<resource>:read`, write-family routes require `<resource>:write`, and
`POST /v1/product-groups/{id}/items` additionally requires
`variants:write` when the item being added is a variant (checked in the
handler body, since the required scope set there depends on the request
payload, not just the route — see `routers/product_groups.py`).

Two independent proofs per route family:

1. **Static** — inspect `app.routes` (via `original_router.routes` for
   the wrapped `_IncludedRouter`s FastAPI produces here) and read the
   `scopes` free variable closed over by `require_scopes(...)`'s
   returned `_check` callable, without ever making a request.
2. **Behavioral** — a real `TestClient` call with
   `app.dependency_overrides[get_current_principal]` swapped for a fake,
   DB-less principal carrying every *other* scope but the one required
   -> `403` (mirrors the dependency-override pattern from
   `tests/unit/test_deps.py` / `tests/unit/test_catalog_routes_registered.py`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.deps import Principal, get_current_principal
from app.main import app


# --- shared plumbing --------------------------------------------------------


def _iter_api_routes() -> Iterator[APIRoute]:
    """Flatten `app.routes`, unwrapping `_IncludedRouter` wrappers to reach
    the real `APIRoute` objects (this FastAPI version wraps every
    `include_router(...)` call)."""
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from original_router.routes
        elif isinstance(route, APIRoute):
            yield route


def _required_scopes(route: APIRoute) -> tuple[str, ...] | None:
    """Read the `scopes` free variable closed over by a
    `Depends(require_scopes(...))` dependency on this route, if any."""
    for dep in route.dependant.dependencies:
        call = dep.call
        freevars = getattr(call.__code__, "co_freevars", ())
        if "scopes" in freevars and call.__closure__:
            idx = freevars.index("scopes")
            return call.__closure__[idx].cell_contents
    return None


def _route(path: str, method: str) -> APIRoute:
    method = method.upper()
    for route in _iter_api_routes():
        if route.path == path and method in route.methods:
            return route
    raise AssertionError(f"no route found for {method} {path}")


class _FakeSession:
    """Must never be touched — `require_scopes` rejects before the route
    handler (and thus before any session use) runs in every 403 case here."""


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


# --- static: declared require_scopes per route ------------------------------

# (method, path, expected required scopes)
_EXPECTED_STATIC_SCOPES: list[tuple[str, str, tuple[str, ...]]] = [
    ("POST", "/v1/products", ("products:write",)),
    ("GET", "/v1/products", ("products:read",)),
    ("GET", "/v1/products/{product_id}", ("products:read",)),
    ("PATCH", "/v1/products/{product_id}", ("products:write",)),
    ("DELETE", "/v1/products/{product_id}", ("products:write",)),
    ("POST", "/v1/products/bulk-upsert", ("products:write",)),
    ("GET", "/v1/variants", ("variants:read",)),
    ("GET", "/v1/variants/{variant_id}", ("variants:read",)),
    ("PATCH", "/v1/variants/{variant_id}", ("variants:write",)),
    ("POST", "/v1/variants/bulk-upsert", ("variants:write",)),
    ("POST", "/v1/product-groups", ("products:write",)),
    ("GET", "/v1/product-groups", ("products:read",)),
    ("GET", "/v1/product-groups/{group_id}", ("products:read",)),
    ("PATCH", "/v1/product-groups/{group_id}", ("products:write",)),
    ("DELETE", "/v1/product-groups/{group_id}", ("products:write",)),
    ("POST", "/v1/product-groups/{group_id}/items", ("products:write",)),
    ("DELETE", "/v1/product-groups/{group_id}/items/{item_id}", ("products:write",)),
]


@pytest.mark.parametrize("method,path,expected", _EXPECTED_STATIC_SCOPES)
def test_route_declares_expected_require_scopes(
    method: str, path: str, expected: tuple[str, ...]
) -> None:
    route = _route(path, method)
    assert _required_scopes(route) == expected


def test_every_catalog_route_declares_some_require_scopes() -> None:
    """No catalog route is accidentally left unguarded (declares *no*
    `require_scopes(...)` dependency at all)."""
    for method, path, _expected in _EXPECTED_STATIC_SCOPES:
        route = _route(path, method)
        assert _required_scopes(route) is not None, f"{method} {path} has no require_scopes"


# --- behavioral: TestClient wrong-scope -> 403 -------------------------------


def test_post_products_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["products:read"]
    )
    resp = client.post(
        "/v1/products", json={"title": "Widget", "price": "9.99", "currency": "USD"}
    )
    assert resp.status_code == 403


def test_get_products_without_read_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes([])
    resp = client.get("/v1/products")
    assert resp.status_code == 403


def test_bulk_upsert_products_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["products:read"]
    )
    resp = client.post("/v1/products/bulk-upsert", json={"products": []})
    assert resp.status_code == 403


def test_get_variants_without_read_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes([])
    resp = client.get("/v1/variants")
    assert resp.status_code == 403


def test_patch_variant_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["variants:read"]
    )
    resp = client.patch(f"/v1/variants/{uuid.uuid4()}", json={"title": "x"})
    assert resp.status_code == 403


def test_bulk_upsert_variants_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["variants:read"]
    )
    resp = client.post("/v1/variants/bulk-upsert", json={"variants": []})
    assert resp.status_code == 403


# --- behavioral: product-groups ---------------------------------------------


def test_post_product_groups_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["products:read"]
    )
    resp = client.post("/v1/product-groups", json={"name": "Group A"})
    assert resp.status_code == 403


def test_get_product_groups_without_read_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes([])
    resp = client.get("/v1/product-groups")
    assert resp.status_code == 403


def test_get_product_group_by_id_without_read_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes([])
    resp = client.get(f"/v1/product-groups/{uuid.uuid4()}")
    assert resp.status_code == 403


def test_patch_product_group_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["products:read"]
    )
    resp = client.patch(f"/v1/product-groups/{uuid.uuid4()}", json={"name": "x"})
    assert resp.status_code == 403


def test_delete_product_group_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["products:read"]
    )
    resp = client.delete(f"/v1/product-groups/{uuid.uuid4()}")
    assert resp.status_code == 403


def test_delete_product_group_item_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["products:read"]
    )
    resp = client.delete(
        f"/v1/product-groups/{uuid.uuid4()}/items/{uuid.uuid4()}"
    )
    assert resp.status_code == 403


def test_add_product_item_without_products_write_scope_is_403(client: TestClient) -> None:
    """Adding a *product* item requires `products:write` — absent -> 403
    (rejected before the handler body/session is ever touched)."""
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["products:read", "variants:write"]
    )
    resp = client.post(
        f"/v1/product-groups/{uuid.uuid4()}/items",
        json={"product_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 403


def test_add_variant_item_with_products_write_but_without_variants_write_is_403(
    client: TestClient,
) -> None:
    """Adding a *variant* item ALSO requires `variants:write`
    (`contracts/api-product-groups.md`) — a principal with only
    `products:write` is refused, even though the route-level
    `require_scopes("products:write")` dependency itself is satisfied.
    """
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["products:write"]
    )
    resp = client.post(
        f"/v1/product-groups/{uuid.uuid4()}/items",
        json={"product_variant_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["detail"]["error"]["code"] == "FORBIDDEN"
