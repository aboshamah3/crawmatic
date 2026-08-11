"""The external OpenAPI spec excludes internal surfaces (PLAN §7.4)."""

from __future__ import annotations

from app.main import app
from app.openapi_public import INTERNAL_TAGS, build_public_openapi


def _spec() -> dict:
    return build_public_openapi(app)


def test_internal_tags_are_the_documented_three():
    assert INTERNAL_TAGS == frozenset(
        {"proxy-providers", "access-policies", "admin"}
    )


def test_admin_paths_are_absent():
    paths = _spec()["paths"]
    assert not [p for p in paths if p.startswith("/v1/admin")]


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
