"""The external OpenAPI spec excludes internal surfaces (PLAN §7.4)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.openapi_public import INTERNAL_TAGS, build_public_openapi


def _spec() -> dict:
    return build_public_openapi(app)


def test_default_openapi_json_is_disabled():
    """The default `FastAPI(...)` spec route would publish the FULL spec
    (admin, proxy-providers, access-policies included) unauthenticated --
    it must not exist at all; only `/openapi-public.json` is served."""
    client = TestClient(app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 404


def test_openapi_public_json_excludes_internal_routes():
    client = TestClient(app)
    resp = client.get("/openapi-public.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert not [p for p in paths if p.startswith("/v1/admin")]
    assert not [p for p in paths if p.startswith("/v1/proxy-providers")]
    assert not [p for p in paths if p.startswith("/v1/access-policies")]


def test_docs_page_is_served_from_the_public_spec():
    client = TestClient(app)
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "openapi-public.json" in resp.text


def test_internal_tags_are_the_documented_three():
    assert INTERNAL_TAGS == frozenset(
        {"proxy-providers", "access-policies", "admin"}
    )


def test_admin_paths_are_absent():
    paths = _spec()["paths"]
    assert not [p for p in paths if p.startswith("/v1/admin")]


def test_admin_workspace_api_key_paths_are_absent():
    """phase4-connect Task 2: the SaaS-control-plane key-management routes
    (`POST/GET /v1/admin/workspaces/{workspace_id}/api-keys`,
    `DELETE .../{api_key_id}`) live on the `admin`-tagged router
    precisely so they inherit this exclusion -- a router with the
    `/v1/admin` prefix but no `admin` tag would silently publish itself
    here instead. `test_admin_paths_are_absent` already covers this by
    prefix; this asserts the exact new paths explicitly."""
    paths = _spec()["paths"]
    assert "/v1/admin/workspaces/{workspace_id}/api-keys" not in paths
    assert "/v1/admin/workspaces/{workspace_id}/api-keys/{api_key_id}" not in paths


def test_proxy_provider_paths_are_absent():
    paths = _spec()["paths"]
    assert not [p for p in paths if p.startswith("/v1/proxy-providers")]


def test_access_policy_paths_are_absent():
    paths = _spec()["paths"]
    assert not [p for p in paths if p.startswith("/v1/access-policies")]


def test_the_public_product_surface_is_present():
    paths = _spec()["paths"]
    for expected in (
        "/v1/products",
        "/v1/competitors",
        "/v1/matches",
        "/v1/refresh-rules",
    ):
        assert expected in paths, expected


def test_spec_has_bearer_security_scheme_documented():
    spec = _spec()
    schemes = spec.get("components", {}).get("securitySchemes", {})
    assert "bearerAuth" in schemes
    assert schemes["bearerAuth"]["scheme"] == "bearer"


def test_no_orphan_schema_references_remain():
    """Stripping paths must not leave a $ref pointing at a removed schema."""
    import json

    spec = _spec()
    blob = json.dumps(spec)
    defined = set(spec.get("components", {}).get("schemas", {}))
    referenced = {
        part.split('"')[0]
        for part in blob.split("#/components/schemas/")[1:]
    }
    assert referenced <= defined, referenced - defined
