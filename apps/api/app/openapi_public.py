"""The customer-facing OpenAPI spec (PLAN §7.4).

The engine's full spec documents surfaces no customer should see: proxy
providers and access policies configure what we are willing to spend on
a fetch, and `/v1/admin` provisions workspaces with a shared secret.
Publishing them would be an information leak and an invitation.

Filtering by **tag** rather than by path prefix means a new internal
router is excluded the moment it is tagged, without anyone remembering
to update a prefix list.

Schemas are pruned to those still reachable from the surviving paths, so
the published document has no dangling `$ref` and no type that only ever
appeared on an internal endpoint.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

#: Router tags never published externally.
INTERNAL_TAGS = frozenset({"proxy-providers", "access-policies", "admin"})

_PUBLIC_TITLE = "Crawmatic API"
_PUBLIC_VERSION = "1.0.0"
_PUBLIC_DESCRIPTION = (
    "Monitor your competitors' prices for your own products.\n\n"
    "Authenticate every request with your workspace API key:\n\n"
    "    Authorization: Bearer ck_your_key_here\n\n"
    "List endpoints are cursor-paginated and return "
    "`{\"items\": [...], \"next_cursor\": ...}`; pass `next_cursor` back as "
    "`?cursor=` to fetch the next page. Errors always carry a top-level "
    "`{\"error\": {\"code\", \"message\"}}` object. Rate limits are 60 read "
    "and 10 write requests per minute per key; exceeding them returns 429 "
    "with a `Retry-After` header."
)


def _prune_schemas(spec: dict[str, Any]) -> dict[str, Any]:
    """Drop component schemas no surviving path can reach.

    Iterates to a fixed point because a kept schema may itself `$ref`
    another one.
    """
    components = spec.get("components", {})
    schemas = components.get("schemas", {})
    if not schemas:
        return spec

    def _refs(blob: Any) -> set[str]:
        text = json.dumps(blob)
        return {
            fragment.split('"')[0]
            for fragment in text.split("#/components/schemas/")[1:]
        }

    keep = _refs(spec.get("paths", {}))
    while True:
        grown = set(keep)
        for name in list(keep):
            if name in schemas:
                grown |= _refs(schemas[name])
        if grown == keep:
            break
        keep = grown

    components["schemas"] = {
        name: body for name, body in schemas.items() if name in keep
    }
    spec["components"] = components
    return spec


def build_public_openapi(app: FastAPI) -> dict[str, Any]:
    """The external spec: internal tags stripped, schemas pruned."""
    spec = get_openapi(
        title=_PUBLIC_TITLE,
        version=_PUBLIC_VERSION,
        description=_PUBLIC_DESCRIPTION,
        routes=app.routes,
    )

    public_paths: dict[str, Any] = {}
    for path, operations in spec.get("paths", {}).items():
        kept = {
            method: operation
            for method, operation in operations.items()
            if not (INTERNAL_TAGS & set(operation.get("tags", [])))
        }
        if kept:
            public_paths[path] = kept
    spec["paths"] = public_paths

    components = spec.setdefault("components", {})
    components.setdefault("securitySchemes", {})["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "description": "Your workspace API key, e.g. `ck_live_a1b2...`.",
    }
    spec["security"] = [{"bearerAuth": []}]

    spec.setdefault("tags", [])
    spec["tags"] = [
        tag for tag in spec.get("tags", []) if tag.get("name") not in INTERNAL_TAGS
    ]

    return _prune_schemas(spec)
