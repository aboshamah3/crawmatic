"""FastAPI application for the `api` service.

SPEC-01 (contracts/health.md) established a single, unauthenticated,
dependency-free liveness endpoint that MUST NOT touch the database,
Redis, or Scrapyd, and MUST NOT construct a per-request DB engine — any
readiness variant reuses the process-wide lazy engine from
``app_shared.database`` instead (FR-020). ``/health`` still holds to
that.

SPEC-03 adds the `/v1/auth/*` router (login/refresh/logout,
contracts/api-auth.md, US1) and the `/v1/api-keys` router
(contracts/api-keys.md, US2, guarded by the `apps.api.app.deps` auth
seam) — those endpoints do use the shared lazy DB/Redis singletons
(never a per-request engine), consistent with the same FR-020
discipline.

SPEC-04 US1 adds the `/v1/products` and `/v1/variants` routers
(contracts/api-products.md, contracts/api-variants.md) — product/variant
CRUD with the default-variant guarantee, on the same auth seam and
FR-020 discipline.

SPEC-04 US3 adds the `/v1/product-groups` router
(contracts/api-product-groups.md) — named product/variant grouping,
reusing the `products:write`/`variants:write` scopes (no new scope),
same auth seam.

SPEC-05 US1 adds the `/v1/competitors` router
(contracts/api-competitors.md) — competitor CRUD with domain uniqueness
per workspace, on the same auth seam and FR-020 discipline, gated by the
existing `competitors:read`/`competitors:write` scopes (no new scope).
"""

from __future__ import annotations

from fastapi import FastAPI

from app.routers import api_keys, auth, competitors, product_groups, products, variants

app = FastAPI(title="crawmatic-api")

app.include_router(auth.router)
app.include_router(api_keys.router)
app.include_router(products.router)
app.include_router(variants.router)
app.include_router(product_groups.router)
app.include_router(competitors.router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. Returns 200 whenever the process is serving."""
    return {"status": "ok"}
