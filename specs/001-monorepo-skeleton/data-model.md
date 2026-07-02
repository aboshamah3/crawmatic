# Phase 1 Data Model: Monorepo & Services Skeleton

> **This is a STRUCTURE-ONLY spec.** There are **no database tables, SQLAlchemy models, or Alembic migrations** in this phase — those arrive in SPEC-02 (Database Foundation). The `alembic/` directory is scaffolded empty. This document instead models the **structural entities** the skeleton is composed of (the "data model" of the deployment topology), taken from the spec's Key Entities and master doc §4–§6.

## Entity: Service Member (deployable)

A deployable unit in the monorepo. Five instances.

| Field | Value domain | Notes |
|-------|--------------|-------|
| `name` | `api` \| `scheduler` \| `worker` \| `scrapers` (Scrapyd HTTP) \| `scrapers-browser` (Scrapyd browser) | Matches §4 service names. |
| `path` | `apps/<dir>` | Workspace member directory. |
| `dependency_closure` | subset of pinned deps | Installed via `uv sync --package <member>`; API/scheduler/worker exclude Scrapy/Twisted/Playwright. |
| `start_command` | shell command | See table below (§4). |
| `exposure` | `public` \| `internal` | Only `api` is `public`. |
| `binds_dual_stack` | bool | true for `api`, `scrapers`, `scrapers-browser`. |
| `runs_as` | non-root user | All members (FR-015). |
| `reaches_postgres_via` | `pgbouncer` \| `none` | Never direct (FR-011); skeleton services may not query at all. |

Start commands (§4, verbatim intent):

| Member | Start command |
|--------|---------------|
| `api` | `uvicorn app.main:app --host :: --port $PORT` |
| `scheduler` | `python -m app.scheduler.scheduler_app` |
| `worker` | `celery -A app.workers.celery_app worker --loglevel=info` |
| `scrapers` | `scrapyd` |
| `scrapers-browser` | `scrapyd` |

**State / lifecycle**: `built → started → healthy`. "Healthy" for `api` = `GET /health` returns 200; for the others = process is up and (Scrapyd) the HTTP API answers on 6800 with auth. No richer state machine in the skeleton.

**Validation rules**:
- Each member installs only its own closure (FR-002, FR-003).
- `api` is the only member with a published host port (FR-013).
- `scrapers` / `scrapers-browser` require basic auth on 6800 (FR-012).
- Every member fails fast if a required env var is missing (FR-017, Edge Cases).

## Entity: Shared Library Member (non-deployable)

A code package imported by service members under fixed dependency direction. Two instances.

| Field | Value domain | Notes |
|-------|--------------|-------|
| `name` / `import_package` | `libs/shared` → `app_shared` \| `libs/scrape-core` → `scrape_core` | |
| `may_import` | `app_shared`: stdlib + SQLAlchemy/psycopg/pydantic-settings; **never** Scrapy/Twisted/Playwright. `scrape_core`: may import `app_shared` + Scrapy-side libs. | FR-003. |
| `imported_by` | `app_shared`: all app members. `scrape_core`: `scrapers`, `scrapers-browser` only. | |

**Validation rules** (enforced by `tests/unit/test_import_boundaries.py`):
- `app_shared` MUST NOT import Scrapy/Twisted/Playwright.
- `scrape_core` MAY depend on `app_shared`; `app_shared` MUST NOT depend on `scrape_core` (no reverse edge).

## Entity: Infrastructure Component

A backing service in the local stack. Three instances.

| Field | Value domain | Notes |
|-------|--------------|-------|
| `name` | `postgres` \| `pgbouncer` \| `redis` | §4. |
| `image` | pinned tag | `postgres:17.5-bookworm`, `edoburu/pgbouncer:v1.23.1-p3`, `redis:7.4.2-bookworm`. |
| `exposure` | `internal` | None published to host. |
| `port` | postgres 5432 · pgbouncer 6432 · redis 6379 | App services target pgbouncer:6432, never postgres:5432. |
| `pgbouncer.pool_mode` | `transaction` | §4. |
| `pgbouncer.auth_type` | `trust` (local) / `scram-sha-256` (deployed) | Assumptions. |
| `binds_dual_stack` | pgbouncer: true | §4 hardening. |

**Relationships**: `pgbouncer → postgres` (the only path to Postgres); app members `→ pgbouncer`; members that need Redis `→ redis`.

## Entity: Environment Configuration

The set of environment variables that parameterize every service (FR-017). Enumerated in `.env.example` and detailed in [contracts/environment.md](./contracts/environment.md). Summary of variables (from §6):

| Variable | Purpose | Consumed by |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+psycopg://…@pgbouncer:6432/…` | api, scheduler, worker, both scrapers (later) |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | per-process pool caps | all DB-using members |
| `REDIS_URL` | Celery broker / locks (later) | worker (and others as needed) |
| `SCRAPYD_HTTP_URLS` | comma-separated pool | worker (later) |
| `SCRAPYD_BROWSER_URLS` | comma-separated pool | worker (later) |
| `SCRAPYD_USERNAME`, `SCRAPYD_PASSWORD` | basic auth on both nodes | scrapers, scrapers-browser; callers |
| `API_PORT` / `PORT` | API listen port (example 8000) | api |
| `API_PUBLIC_BASE_URL`, `INTERNAL_API_BASE_URL` | external/internal base URLs | api / internal callers |
| Postgres/PgBouncer bootstrap (`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, pgbouncer auth) | local stack bring-up | infra |

**Validation rules**:
- `SCRAPYD_HTTP_URLS` / `SCRAPYD_BROWSER_URLS` are parsed as comma-separated **pools** even when a single URL is present (FR-018).
- Missing required variable → the service fails fast with a clear error, not a half-configured start (FR-017, Edge Cases).
- No real secret values are committed; `.env.example` carries placeholders only.

## Non-entities (explicitly deferred)

The following appear in the master doc's §22 database model but are **out of scope** here and introduced by later specs: workspaces, users, api_keys, products, product_variants, competitors, competitor_product_matches, scrape_profiles, access_policies, domain_strategy_profiles, scrape_jobs, price_observations, alerts, webhooks, and all partitioned/RLS tables. No columns, constraints, enums, or migrations for these are created in this phase.
