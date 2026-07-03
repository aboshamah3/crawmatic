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

SPEC-05 US2 adds the `/v1/matches` router (contracts/api-matches.md) —
single-record match CRUD with save-time URL-safety validation
(`app_shared.url_safety`) and versioned URL normalization/pattern
derivation (`app_shared.url_pattern`), on the same auth seam and FR-020
discipline, gated by the existing `matches:read`/`matches:write` scopes
(no new scope). `POST /v1/matches/bulk-upsert` (US3) lands in a later
phase of this feature.

SPEC-06 US1 adds the `/v1/scrape-profiles` router
(contracts/api-scrape-profiles.md) — dual-scope (own + global read,
own-only write) extraction-profile CRUD + `POST
/v1/scrape-profiles/bulk-upsert`, on the same auth seam and FR-020
discipline, gated by the new `scrape_profiles:read`/`scrape_profiles:write`
scopes. `PUT /v1/scrape-profiles/workspace-default` (assignment, US2)
lands in a later phase of this feature.

SPEC-08 US1 adds the `/v1/jobs` router (contracts/api-jobs.md) —
`POST /v1/jobs/run/match/{id}` (create + dispatch a single-match scrape
job) plus `GET /v1/jobs/{id}` / `GET /v1/jobs/{id}/results` (status +
per-target outcomes), on the same auth seam and FR-020 discipline,
gated by the new `jobs:read`/`jobs:write` scopes. Job creation
delegates to `app_shared.jobs.service`; dispatch is enqueued through
`app_shared.messaging` — this router never imports `apps/workers`.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.routers import (
    api_keys,
    auth,
    competitors,
    jobs,
    matches,
    product_groups,
    products,
    scrape_profiles,
    variants,
)

app = FastAPI(title="crawmatic-api")

app.include_router(auth.router)
app.include_router(api_keys.router)
app.include_router(products.router)
app.include_router(variants.router)
app.include_router(product_groups.router)
app.include_router(competitors.router)
app.include_router(matches.router)
app.include_router(scrape_profiles.router)
app.include_router(jobs.router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. Returns 200 whenever the process is serving."""
    return {"status": "ok"}
