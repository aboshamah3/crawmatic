"""Competitors/matches scope-gating unit tests (SPEC-05 US4 T028, FR-015/SC-008).

Mirrors `tests/unit/test_catalog_scope_gating.py` (SPEC-04) for the two
new route families — competitors (`competitors:read/write`) and matches
(`matches:read/write`, including `POST /v1/matches/bulk-upsert` which
requires `matches:write`).

Two independent proofs per route:

1. **Static** — inspect `app.routes` (via `original_router.routes` for
   the wrapped `_IncludedRouter`s FastAPI produces here) and read the
   `scopes` free variable closed over by `require_scopes(...)`'s
   returned `_check` callable, without ever making a request.
2. **Behavioral** — a real `TestClient` call with
   `app.dependency_overrides[get_current_principal]` swapped for a fake,
   DB-less principal lacking the required scope -> `403`.
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
    ("POST", "/v1/competitors", ("competitors:write",)),
    ("GET", "/v1/competitors", ("competitors:read",)),
    ("GET", "/v1/competitors/{competitor_id}", ("competitors:read",)),
    ("PATCH", "/v1/competitors/{competitor_id}", ("competitors:write",)),
    ("DELETE", "/v1/competitors/{competitor_id}", ("competitors:write",)),
    ("POST", "/v1/matches", ("matches:write",)),
    ("GET", "/v1/matches", ("matches:read",)),
    ("GET", "/v1/matches/{match_id}", ("matches:read",)),
    ("PATCH", "/v1/matches/{match_id}", ("matches:write",)),
    ("DELETE", "/v1/matches/{match_id}", ("matches:write",)),
    ("POST", "/v1/matches/bulk-upsert", ("matches:write",)),
]


@pytest.mark.parametrize("method,path,expected", _EXPECTED_STATIC_SCOPES)
def test_route_declares_expected_require_scopes(
    method: str, path: str, expected: tuple[str, ...]
) -> None:
    route = _route(path, method)
    assert _required_scopes(route) == expected


def test_every_competitors_matches_route_declares_some_require_scopes() -> None:
    """No competitors/matches route is accidentally left unguarded (declares
    *no* `require_scopes(...)` dependency at all)."""
    for method, path, _expected in _EXPECTED_STATIC_SCOPES:
        route = _route(path, method)
        assert _required_scopes(route) is not None, f"{method} {path} has no require_scopes"


# --- behavioral: TestClient wrong-scope -> 403 -------------------------------


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


def test_post_matches_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["matches:read"]
    )
    resp = client.post(
        "/v1/matches",
        json={
            "product_variant_id": str(uuid.uuid4()),
            "competitor_id": str(uuid.uuid4()),
            "competitor_url": "https://competitor.example.com/products/widget",
        },
    )
    assert resp.status_code == 403


def test_get_matches_without_read_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes([])
    resp = client.get("/v1/matches")
    assert resp.status_code == 403


def test_get_match_by_id_without_read_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes([])
    resp = client.get(f"/v1/matches/{uuid.uuid4()}")
    assert resp.status_code == 403


def test_patch_match_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["matches:read"]
    )
    resp = client.patch(f"/v1/matches/{uuid.uuid4()}", json={"priority": "HIGH"})
    assert resp.status_code == 403


def test_delete_match_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["matches:read"]
    )
    resp = client.delete(f"/v1/matches/{uuid.uuid4()}")
    assert resp.status_code == 403


def test_bulk_upsert_matches_without_write_scope_is_403(client: TestClient) -> None:
    """`POST /v1/matches/bulk-upsert` requires `matches:write` — a
    `matches:read`-only principal is refused before the handler (and thus
    before any session/DB use) runs."""
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["matches:read"]
    )
    resp = client.post("/v1/matches/bulk-upsert", json={"matches": []})
    assert resp.status_code == 403
