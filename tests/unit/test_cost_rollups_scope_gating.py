"""Scope-gating unit tests for `GET /v1/cost-rollups` (EPA C6).

Mirrors `tests/unit/test_matches_scope_gating.py`: a static proof that
the route declares `require_scopes("cost_rollups:read")` and nothing
looser, plus a behavioral proof that a principal lacking the scope gets
403 BEFORE the route handler (and therefore before any session use)
runs, and that a principal WITH the scope reaches the handler.

This is the "tenant breakdown requires the authorized scope" proof named
in the C6 plan's Step 1 failing-tests list.
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
    """Flatten `app.routes`, unwrapping `_IncludedRouter` wrappers to reach
    the real `APIRoute` objects (this FastAPI version wraps every
    `include_router(...)` call) -- same helper as
    `test_matches_scope_gating.py`."""
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


class _RejectingSession:
    """Must never be touched -- `require_scopes` rejects before the route
    handler (and thus before any session use) runs in the 403 case."""


class _EmptySession:
    """A session whose every `.execute(...)` returns zero rows -- enough
    to prove the handler is REACHED (the scope check passed) without
    needing a real database."""

    def execute(self, *_a, **_k):
        return _EmptyResult()


class _EmptyResult:
    def scalars(self) -> "_EmptyResult":
        return self

    def all(self) -> list:
        return []


def _fake_principal_with_scopes(scopes: list[str], *, session=None):
    session = session or _RejectingSession()

    def _dependency() -> Iterator[tuple[object, Principal]]:
        yield session, Principal(
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


def test_route_declares_cost_rollups_read_and_nothing_looser() -> None:
    route = _route("/v1/cost-rollups", "GET")
    assert _required_scopes(route) == ("cost_rollups:read",)


def test_get_cost_rollups_without_scope_is_403(client: TestClient) -> None:
    """No scope at all -- and, distinctly, holding an ADJACENT scope
    (`jobs:read`) must not be enough either: this is a narrow,
    separately-grantable capability, not folded into a broader one."""
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes([])
    resp = client.get("/v1/cost-rollups")
    assert resp.status_code == 403


def test_get_cost_rollups_with_an_unrelated_scope_is_still_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["jobs:read", "products:read"]
    )
    resp = client.get("/v1/cost-rollups")
    assert resp.status_code == 403


def test_get_cost_rollups_with_the_scope_reaches_the_handler(client: TestClient) -> None:
    """With `cost_rollups:read`, the request passes the scope gate and
    reaches the route handler (proven by a 200 + well-formed empty
    envelope from a DB-less fake session) -- never a 403."""
    app.dependency_overrides[get_current_principal] = _fake_principal_with_scopes(
        ["cost_rollups:read"], session=_EmptySession()
    )
    resp = client.get("/v1/cost-rollups")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": [], "next_cursor": None}
