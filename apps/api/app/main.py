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

SPEC-09 US2 adds the `/v1/alerts/current` (+`/{variant_id}`) and
`/v1/alert-events` routers (contracts/api-alerts.md) — cursor-paginated,
filterable reads over `variant_alert_states`/`price_alert_events`, on
the same auth seam and FR-020 discipline, gated by the existing
`alerts:read` scope (no new scope; `/v1/variants/{id}/price-comparison`,
US1, already uses it). Never imports `apps/workers`.

SPEC-10 US1 adds the `/v1/proxy-providers` and `/v1/access-policies`
routers (both dual-scope: own + global read, own-only write, mirroring
`/v1/scrape-profiles`) and the `/v1/domain-access-rules` router
(tenant-only, mirroring `/v1/competitors`) — `contracts/api-access.md` —
on the same auth seam and FR-020 discipline, gated by the new
`proxy_providers:read/write`, `access_policies:read/write`, and
`domain_rules:read/write` scopes. Proxy passwords are encrypted at rest
and never returned in plaintext (`has_password` only, SC-003).

SPEC-12 US3 adds the `/v1/strategy/discovery-runs` router
(`contracts/discovery.md`, `contracts/api-and-observability.md`) —
operator-triggered domain strategy discovery (`POST`, 3-10 `sample_urls`,
422 out-of-bounds) plus cursor-list/get, on the same auth seam and
FR-020 discipline, gated by the new `strategy:read`/`strategy:write`
scopes. Delegates creation + enqueue to `app.services.strategy`; never
imports `apps/workers`.

SPEC-13 US1 adds the `/v1/refresh-rules` router
(`contracts/refresh-rules-api.md`) — workspace-scoped CRUD + enable/disable
for the DB-driven scheduler's recurring refresh policy (exactly one of a
5-field UTC cron or an interval cadence, one of six `ScrapeScope`s + target
id), on the same auth seam and FR-020 discipline, gated by the new
`refresh_rules:read`/`refresh_rules:write` scopes. The scheduler pass that
acts on these rules lives in `apps/scheduler` (US2, a later phase); this
router never imports it.

SPEC-16 US1 adds the `/v1/webhook-events` (+`/{id}`) router
(`contracts/rest-api.md`) — cursor-paginated, `event_type`-filterable reads
over `webhook_events`, on the same auth seam and FR-020 discipline, gated by
the existing `webhooks:read` scope (no new scope). `/v1/webhook-endpoints*`
CRUD (US2) lands in the same router module in a later phase. Never imports
`apps/workers` — the `create_webhook_event` task that populates this table
is enqueued by name elsewhere.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.routers import (
    access_policies,
    alerts,
    api_keys,
    auth,
    competitors,
    domain_access_rules,
    jobs,
    matches,
    product_groups,
    products,
    proxy_providers,
    refresh_rules,
    scrape_profiles,
    strategy,
    variants,
    webhooks,
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
app.include_router(alerts.router)
app.include_router(proxy_providers.router)
app.include_router(access_policies.router)
app.include_router(domain_access_rules.router)
app.include_router(strategy.router)
app.include_router(refresh_rules.router)
app.include_router(webhooks.router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. Returns 200 whenever the process is serving."""
    return {"status": "ok"}
