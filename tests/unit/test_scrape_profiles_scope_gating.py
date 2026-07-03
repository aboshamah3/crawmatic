"""Scrape-profiles scope-gating unit tests (SPEC-06 US1 T025, FR-004, SC-007).

Mirrors `tests/unit/test_matches_scope_gating.py` (SPEC-05) for the new
`/v1/scrape-profiles` route family (`scrape_profiles:read/write`).

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


def _iter_api_routes() -> Iterator[APIRoute]:
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from original_router.routes
        elif isinstance(route, APIRoute):
            yield route


def _required_scopes(route: APIRoute) -> tuple[str, ...] | None:
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

_EXPECTED_STATIC_SCOPES: list[tuple[str, str, tuple[str, ...]]] = [
    ("POST", "/v1/scrape-profiles", ("scrape_profiles:write",)),
    ("GET", "/v1/scrape-profiles", ("scrape_profiles:read",)),
    ("GET", "/v1/scrape-profiles/{profile_id}", ("scrape_profiles:read",)),
    ("PATCH", "/v1/scrape-profiles/{profile_id}", ("scrape_profiles:write",)),
    ("DELETE", "/v1/scrape-profiles/{profile_id}", ("scrape_profiles:write",)),
    ("POST", "/v1/scrape-profiles/bulk-upsert", ("scrape_profiles:write",)),
]


@pytest.mark.parametrize("method,path,expected", _EXPECTED_STATIC_SCOPES)
def test_route_declares_expected_require_scopes(
    method: str, path: str, expected: tuple[str, ...]
) -> None:
    route = _route(path, method)
    assert _required_scopes(route) == expected


def test_every_scrape_profiles_route_declares_some_require_scopes() -> None:
    for method, path, _expected in _EXPECTED_STATIC_SCOPES:
        route = _route(path, method)
        assert _required_scopes(route) is not None, f"{method} {path} has no require_scopes"


# --- behavioral: TestClient wrong-scope -> 403 -------------------------------


def test_post_scrape_profiles_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["scrape_profiles:read"]
    )
    resp = client.post("/v1/scrape-profiles", json={"name": "acme-profile"})
    assert resp.status_code == 403


def test_get_scrape_profiles_without_read_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes([])
    resp = client.get("/v1/scrape-profiles")
    assert resp.status_code == 403


def test_get_scrape_profile_by_id_without_read_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes([])
    resp = client.get(f"/v1/scrape-profiles/{uuid.uuid4()}")
    assert resp.status_code == 403


def test_patch_scrape_profile_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["scrape_profiles:read"]
    )
    resp = client.patch(f"/v1/scrape-profiles/{uuid.uuid4()}", json={"name": "x"})
    assert resp.status_code == 403


def test_delete_scrape_profile_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["scrape_profiles:read"]
    )
    resp = client.delete(f"/v1/scrape-profiles/{uuid.uuid4()}")
    assert resp.status_code == 403


def test_bulk_upsert_scrape_profiles_without_write_scope_is_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["scrape_profiles:read"]
    )
    resp = client.post("/v1/scrape-profiles/bulk-upsert", json={"profiles": []})
    assert resp.status_code == 403
