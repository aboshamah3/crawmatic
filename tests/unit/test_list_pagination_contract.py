"""Every public collection endpoint is cursor-paginated (PLAN §7.4).

A property test rather than a per-router test: a new list endpoint that
forgets the envelope should fail here without anyone adding a case.
"""

from __future__ import annotations

from app.main import app
from app.openapi_public import build_public_openapi

#: Collection GETs that legitimately return a single object, not a list.
_SINGLETON_PATHS = {
    "/health",
    # Build/release provenance (audit §C2) -- a single point-in-time
    # snapshot (git SHA, migration heads), never a list.
    "/version",
    "/openapi-public.json",
    # One computed price-comparison snapshot for the given variant (SPEC-09
    # US1) -- the path's last segment ("price-comparison") isn't a
    # `{param}`, so the naive plural-suffix heuristic misreads it as a
    # collection. It is not one: the response is a single
    # PriceComparisonResponse, never a list.
    "/v1/variants/{variant_id}/price-comparison",
    # Readiness probe (2026-08-20 prelaunch hardening audit) -- one
    # point-in-time {"ready", "checks"} snapshot of the database/Redis
    # dependency state, same shape as `/health`/`/version` above, never a
    # list.
    "/ready",
}


def _public_get_paths() -> dict:
    spec = build_public_openapi(app)
    return {
        path: ops["get"]
        for path, ops in spec["paths"].items()
        if "get" in ops and path not in _SINGLETON_PATHS
    }


def _is_collection(path: str) -> bool:
    """A collection path ends in a plural segment, not a `{param}`."""
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return not last.startswith("{")


def test_every_public_collection_get_accepts_cursor_and_limit():
    offenders = []
    for path, op in _public_get_paths().items():
        if not _is_collection(path):
            continue
        params = {p["name"] for p in op.get("parameters", [])}
        if not {"cursor", "limit"} <= params:
            offenders.append((path, sorted(params)))
    assert not offenders, offenders


def test_every_public_collection_get_returns_the_envelope():
    import json

    spec = build_public_openapi(app)
    schemas = spec["components"]["schemas"]
    offenders = []
    for path, op in _public_get_paths().items():
        if not _is_collection(path):
            continue
        content = (
            op.get("responses", {})
            .get("200", {})
            .get("content", {})
            .get("application/json", {})
        )
        ref = json.dumps(content)
        names = [f.split('"')[0] for f in ref.split("#/components/schemas/")[1:]]
        if not names:
            offenders.append((path, "no schema"))
            continue
        props = schemas.get(names[0], {}).get("properties", {})
        if not {"items", "next_cursor"} <= set(props):
            offenders.append((path, sorted(props)))
    assert not offenders, offenders
