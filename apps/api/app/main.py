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

PLAN §7.1 adds the `/v1/admin` router (`docs/superpowers/plans/
2026-08-10-phase2-engine.md`) — SaaS control-plane workspace
provisioning, archive, and the usage export. It is guarded by the static
`SAAS_SERVICE_TOKEN` seam in `app.service_auth`, not the workspace seam
in `app.deps`, and is excluded from the public OpenAPI spec.

Production-readiness audit (`CORE_PRODUCT_PRODUCTION_READINESS_AUDIT_
2026-08-15.md` §C2) adds the `GET /version` route
(`apps/api/app/routers/version.py`) — deployed git SHA + build time +
code/live-database Alembic migration heads, unauthenticated like
`/health` but (unlike `/health`) reads the database's `alembic_version`
table on purpose, so a stale or mismatched deploy is visible without
shelling into a container.

The 2026-08-20 prelaunch hardening audit adds `GET /ready`
(`apps/api/app/routers/ready.py`) — the readiness variant
`contracts/health.md` predicted by name when it first wrote FR-020:
`/health` stays liveness-only (never touches the database), and `/ready`
is the new, separate, unauthenticated probe that checks the database
(`SELECT 1`) and Redis (`PING`) — via the same process-wide lazy
singletons FR-020 requires, each independently timeboxed — and answers
200 only when both are reachable, 503 otherwise. See that module's
docstring for the full reasoning, including why Redis has no
"not-configured" state to report for this process.

The same audit's §H5 adds `GET /ops/metrics`
(`apps/api/app/routers/ops_metrics.py`) — the fleet-wide operational
snapshot (queue age, per-domain success and requests/link, proxy spend
velocity and month-end forecast, discovery volume, partition/rollup
health, outbox backlog, circuit-breaker posture, Redis posture) plus the
alert rules evaluated against it. Unlike `/version` it is a
*cross-workspace* surface, so it is guarded by the same
`SAAS_SERVICE_TOKEN` seam as `/v1/admin/*` and excluded from the public
spec. Rules and thresholds live in `app_shared.opsmetrics.rules`;
operator documents are under `docs/ops/`.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html

from app_shared.config import get_settings
from app_shared.config_validation import assert_production_safe

from app.abuse_limit import AbuseLimitMiddleware
from app.error_envelope import register_error_handlers
from app.openapi_public import build_public_openapi
from app.rate_limit import RateLimitMiddleware
from app.thread_pool import configure_thread_pool
from app.routers import (
    access_policies,
    admin,
    alerts,
    api_keys,
    auth,
    competitors,
    cost_rollups,
    domain_access_rules,
    jobs,
    jobs_admin,
    matches,
    ops_metrics,
    product_groups,
    products,
    proxy_providers,
    ready,
    refresh_rules,
    scrape_profiles,
    strategy,
    variants,
    version,
    webhooks,
)

# Audit §L1: refuses to start (raises `ProductionConfigError`) when
# `ENVIRONMENT`/`RAILWAY_ENVIRONMENT_NAME` says "production" and the
# resolved config still looks local-dev-shaped (weak/default secrets,
# PGBOUNCER_AUTH_TYPE=trust, the DB bootstrap/owner role as DATABASE_URL).
# No-op otherwise — never affects local dev, CI, or `docker compose up`.
assert_production_safe()

logger = logging.getLogger(__name__)

app = FastAPI(title="crawmatic-api", openapi_url=None, docs_url=None, redoc_url=None)

register_error_handlers(app)

app.add_middleware(RateLimitMiddleware)

# EPA W5.5-L1 item 2: fail-CLOSED, Postgres-authoritative limits on the
# abuse-able surfaces (discovery trigger, manual recheck, exports, the admin
# control plane). Deliberately a SECOND middleware rather than a change to
# `RateLimitMiddleware`: that one guards cost across the whole API and is
# fail-OPEN on purpose, and merging the two would force one failure posture
# onto both. Inert until its counter table exists — see the module docstring.
app.add_middleware(AbuseLimitMiddleware)

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
# EPA C6: tenant-scoped, cost_rollups:read-gated read over the durable,
# bounded network_cost_rollups table -- see routers/cost_rollups.py's
# module docstring for why this is the opposite auth posture of
# ops_metrics.router's fleet-only surface, never a tenant breakdown
# added there.
app.include_router(cost_rollups.router)
app.include_router(webhooks.router)
app.include_router(admin.router)
# EPA A2: also under /v1/admin, but on the TENANT auth seam (scope-gated,
# RLS-scoped session) rather than `admin.router`'s cross-workspace service
# token — see `routers/jobs_admin.py`'s module docstring. No path is served
# by both routers.
app.include_router(jobs_admin.router)
app.include_router(version.router)
app.include_router(ready.router)
app.include_router(ops_metrics.router)


# H2 (production-readiness audit): bound the anyio worker-thread pool that
# backs every sync DB call to the DB connection pool's size, instead of
# anyio's unrelated default — see `app.thread_pool` for why. Runs at
# startup, not import time: the anyio limiter it configures belongs to the
# event loop the app is served on, which doesn't exist yet at import.
#
# `get_settings()` failing here is swallowed, not raised: this repo already
# resolves `Settings` lazily, per call site (`deps.py`, `service_auth.py`,
# `auth.py`, ...), specifically so unit tests can exercise one router
# without a full DATABASE_URL/REDIS_URL/... environment (see e.g.
# `test_admin_router.py::test_admin_routes_require_the_service_token`'s own
# docstring on why it monkeypatches `get_settings` rather than provide a
# real `Settings()`). Every `TestClient(app)` used as a context manager
# fires this hook, so raising here would newly force a full settings
# environment onto tests that have nothing to do with the DB/thread pool —
# a real deploy still gets a fail-fast Settings error at first request,
# unchanged from before this hook existed; this only skips the one-time
# tuning step when settings aren't resolvable yet, leaving anyio's own
# default limiter in place rather than taking the whole process down.
@app.on_event("startup")
def _configure_thread_pool() -> None:
    try:
        settings = get_settings()
    except Exception:
        logger.warning(
            "H2 thread-pool tuning skipped: Settings() could not be constructed "
            "at startup (see app.thread_pool.configure_thread_pool); the anyio "
            "default limiter is in effect instead of API_THREAD_POOL_SIZE.",
            exc_info=True,
        )
        return
    configure_thread_pool(settings)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. Returns 200 whenever the process is serving."""
    return {"status": "ok"}


@app.get("/openapi-public.json", include_in_schema=False)
def public_openapi() -> dict:
    """The customer-facing spec — internal routers excluded (PLAN §7.4)."""
    return build_public_openapi(app)


@app.get("/docs", include_in_schema=False)
def public_docs():
    """Swagger UI over the PUBLIC spec only — the default `/docs` (which
    would have rendered the full, internal-route-including spec) is
    disabled via `docs_url=None` above."""
    return get_swagger_ui_html(openapi_url="/openapi-public.json", title="Crawmatic API")
