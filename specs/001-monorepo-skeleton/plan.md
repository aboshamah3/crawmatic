# Implementation Plan: Monorepo & Services Skeleton

**Branch**: `001-monorepo-skeleton` | **Date**: 2026-07-02 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-monorepo-skeleton/spec.md`

**Master doc**: `/srv/crawmatic/PROJECT_SPEC.md` §3 (Final Stack), §4 (Deployment Services), §5 (Monorepo Structure), §6 (Service Communication).

## Summary

Deliver the empty-but-bootable multi-service skeleton on which every later spec (02+) is built. A single `uv` workspace (one root `pyproject.toml`, one `uv.lock`) contains five deployable application members — `apps/api`, `apps/scheduler`, `apps/workers`, `apps/scrapers` (Scrapyd HTTP), `apps/scrapers-browser` (Scrapyd browser) — and two library members — `libs/shared` (`app_shared`) and `libs/scrape-core` (`scrape_core`). A root `docker-compose.yml` brings up all eight components (the five app services plus `postgres`, `pgbouncer`, `redis`) with pinned images, non-root users, and dual-stack binds. The only business behaviour in scope is `GET /health` on the API; the scheduler, worker, and both Scrapyd nodes boot to a running state via their §4 start commands and do nothing else. All application services reach Postgres only through PgBouncer (transaction pooling, port 6432); both Scrapyd nodes require basic auth; only the API is published to the host.

**Explicitly OUT OF SCOPE (later specs):** DB models/schema, Alembic migrations, the one-shot direct-to-Postgres migration job, authentication/API keys, RLS, scraping/extraction behaviour, spiders' logic, rate limiting, and the split Redis broker/locks instances. This phase establishes only structure, wiring, health checks, and the minimal cross-cutting patterns (lazy per-process DB engine, Celery fork-safety hook) that later specs depend on. The `alembic/` directory is scaffolded empty; **no migrations are authored here.**

## Technical Context

**Language/Version**: Python 3.13 (`requires-python = ">=3.13,<3.14"`). Chosen as the current stable interpreter supported across the entire pinned stack (FastAPI/Starlette, Celery 5.5, Scrapy 2.13, Twisted 24.x, scrapy-playwright 0.0.4x, SQLAlchemy 2.0, psycopg 3.2) as of 2026-07. Rationale and alternatives in [research.md](./research.md).

**Primary Dependencies** (declared per-member; pinned in `uv.lock`):
- `apps/api`: FastAPI, Uvicorn (+ `app_shared`)
- `apps/scheduler`: `app_shared` (plain Python process)
- `apps/workers`: Celery, Redis client (+ `app_shared`)
- `apps/scrapers` / `apps/scrapers-browser`: Scrapy, Scrapyd (+ `scrape_core`, `app_shared`); browser member adds `scrapy-playwright` + Playwright
- `libs/shared` (`app_shared`): SQLAlchemy, psycopg, pydantic-settings — **MUST NOT depend on Scrapy/Twisted/Playwright**
- `libs/scrape-core` (`scrape_core`): may depend on `app_shared`; never the reverse

**Storage**: PostgreSQL reached exclusively through PgBouncer (transaction pooling, 6432). No schema/models in this phase.

**Testing**: pytest. Skeleton-level checks only: health-endpoint test, import-boundary tests (assert `app_shared` cannot import Scrapy/Twisted/Playwright), and a compose smoke test (all eight components healthy). No business-logic tests.

**Target Platform**: Linux containers on a multi-service platform (Railway or similar); local orchestration via Docker Compose using the same images.

**Project Type**: Multi-service monorepo (uv workspace) — backend only, no frontend.

**Performance Goals**: N/A for the skeleton beyond "all eight components reach healthy state with one command" (SC-001) and "health check succeeds 100% once up" (SC-002).

**Constraints**: Pinned images (no `latest`); non-root containers; dual-stack (IPv4+IPv6) binds on API, both Scrapyd nodes, and PgBouncer; only API published to host; Scrapyd basic auth on both nodes; one lazy SQLAlchemy engine per process (never at import, never per request); Celery disposes inherited engine on `worker_process_init`.

**Scale/Scope**: Eight components, five Dockerfiles, one compose file, one `.env.example`. Skeleton establishes patterns designed for later 2,000 products / 10k–20k matches per workspace, but carries no data path yet.

**Chosen pinned image tags** (research.md §Image Pinning):
- Python base: `python:3.13.5-slim-bookworm`
- Postgres: `postgres:17.5-bookworm`
- PgBouncer: `edoburu/pgbouncer:v1.23.1-p3`
- Redis: `redis:7.4.2-bookworm`
- uv (build-stage binary copy): `ghcr.io/astral-sh/uv:0.7.13`
- Playwright browser (browser Scrapyd service, installed at build): Chromium via `playwright install --with-deps chromium`, Playwright version pinned in `uv.lock`.

## Constitution Check

*GATE: evaluated against `.specify/memory/constitution.md` v1.0.1 (8 principles). Re-checked after Phase 1 — still PASS.*

| Principle | Status for this skeleton | Justification |
|-----------|--------------------------|---------------|
| **I. API-First, Service-Oriented Architecture** | **PASS** | Monorepo deploys exactly the eight named components; responsibilities separated (FastAPI/Celery orchestrate, Scrapyd/Scrapy scrape, scheduler process, Postgres source of truth). Only `api-service` published to host; Scrapyd/internal services unpublished (FR-013). Shared scrape code lives in `libs/scrape-core` imported by both Scrapy projects (FR-004). |
| **II. Workspace Isolation (NON-NEGOTIABLE)** | **N/A — deferred (SPEC-03)** | No queries, tables, or routes with data in this phase; no `workspace_id` surface exists yet. Nothing here can violate isolation; the DB session helper is structured so later RLS / `SET LOCAL` works under transaction pooling. |
| **III. Variant-Level Pricing & Explicit Matching** | **N/A — deferred (SPEC-04/05)** | No catalog/models in scope. |
| **IV. Database-Driven Configuration** | **N/A — deferred (SPEC-06+)** | No configuration tables or resolution chains yet; env-driven service config only (FR-017). |
| **V. Disciplined Scraping Runtime (NON-NEGOTIABLE)** | **PASS (structural)** | Scrapy runs under Scrapyd services, never inside Celery (§4 start commands honoured: worker runs `celery ... worker`; Scrapyd nodes run `scrapyd`). Task names will live in `app_shared/task_names.py` (string constants); spiders will `send_task()` and never import the worker — the dependency boundary that enforces this is set up now. No spider logic authored here. |
| **VI. Internal-Only & Legally Compliant Access (NON-NEGOTIABLE)** | **PASS** | Both Scrapyd nodes require basic auth (FR-012); unauthenticated requests rejected. Only API publicly exposed (FR-013). No external scraping APIs anywhere in the dependency closure. No raw HTML/screenshot storage introduced. |
| **VII. Monetary & Extraction Correctness** | **N/A — deferred (SPEC-02+)** | No money handling or extraction in scope. |
| **VIII. Scale-Safe Data & Concurrency** | **PASS (patterns established)** | All Postgres traffic routed through PgBouncer transaction pooling (FR-011); no direct connections from app services (the migration-job exception is later). One lazy SQLAlchemy engine per process (FR-020); Celery disposes the inherited engine on `worker_process_init` (fork-safety). Redis present. Rate limiting / dedup / partitioning are later specs, not contradicted. |

**Technology & Security Constraints**: locked stack honoured (Python/FastAPI/Celery/Redis/Scrapy/Scrapyd/scrapy-playwright); all images pinned (no `latest`, FR-014); containers non-root (FR-015); dual-stack binds (FR-016). No secrets committed — `.env.example` enumerates variables only.

**Result: Constitution Check PASSES.** No deviations to record; Complexity Tracking table left empty.

## Project Structure

### Documentation (this feature)

```text
specs/001-monorepo-skeleton/
├── plan.md              # This file
├── research.md          # Phase 0 — version/image/pattern decisions
├── data-model.md        # Phase 1 — structural entities (NOT DB models)
├── quickstart.md        # Phase 1 — bring-up & validation guide
├── contracts/           # Phase 1 — health, service topology, environment
│   ├── health.md
│   ├── service-topology.md
│   └── environment.md
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
crawmatic/
├── README.md
├── PROJECT_SPEC.md
├── pyproject.toml                 # uv workspace root; [tool.uv.workspace] members
├── uv.lock                        # single lockfile for the whole workspace
├── docker-compose.yml             # brings up all 8 components
├── .env.example                   # enumerates every env var from §6
├── .dockerignore
│
├── apps/
│   ├── api/
│   │   ├── pyproject.toml          # member: fastapi, uvicorn, app_shared
│   │   ├── Dockerfile              # from python:3.13.5-slim-bookworm, non-root
│   │   └── app/
│   │       ├── __init__.py
│   │       └── main.py             # FastAPI app; GET /health only
│   ├── scheduler/
│   │   ├── pyproject.toml          # member: app_shared
│   │   ├── Dockerfile
│   │   └── app/scheduler/
│   │       ├── __init__.py
│   │       └── scheduler_app.py    # boots to running loop; no business logic
│   ├── workers/
│   │   ├── pyproject.toml          # member: celery, redis, app_shared
│   │   ├── Dockerfile
│   │   └── app/workers/
│   │       ├── __init__.py
│   │       └── celery_app.py       # Celery app + worker_process_init fork hook
│   ├── scrapers/
│   │   ├── pyproject.toml          # member: scrapy, scrapyd, scrape_core, app_shared
│   │   ├── Dockerfile              # bakes project + scrapyd.conf (basic auth)
│   │   ├── scrapy.cfg
│   │   ├── scrapyd.conf
│   │   └── price_monitor/
│   │       ├── __init__.py
│   │       ├── settings.py
│   │       └── spiders/__init__.py
│   └── scrapers-browser/
│       ├── pyproject.toml          # + scrapy-playwright, playwright
│       ├── Dockerfile              # installs Playwright chromium at build; low concurrency
│       ├── scrapy.cfg
│       ├── scrapyd.conf
│       └── price_monitor_browser/
│           ├── __init__.py
│           ├── settings.py         # playwright download handler; low concurrency
│           └── spiders/__init__.py
│
├── libs/
│   ├── shared/
│   │   ├── pyproject.toml           # member: sqlalchemy, psycopg, pydantic-settings
│   │   └── app_shared/
│   │       ├── __init__.py
│   │       ├── config.py            # env-driven settings (pydantic-settings)
│   │       ├── database.py          # lazy per-process engine/session helper (stub)
│   │       └── task_names.py        # Celery task-name string constants (empty stub)
│   └── scrape-core/
│       ├── pyproject.toml           # member: may depend on app_shared
│       └── scrape_core/
│           └── __init__.py
│
├── alembic/
│   └── versions/                    # scaffolded EMPTY — no migrations authored here
│       └── .gitkeep
│
└── tests/
    ├── unit/
    │   └── test_import_boundaries.py  # app_shared must not import scrapy/twisted/playwright
    └── integration/
        └── test_health.py            # GET /health returns 200
```

**Structure Decision**: Adopt the master-doc §5 monorepo tree verbatim as a `uv` workspace. Five deployable app members + two library members, each with its own `pyproject.toml` declaring only its dependency closure; one root `pyproject.toml` declares `[tool.uv.workspace]` members and one `uv.lock` locks the whole graph. Per-service Dockerfiles install only their member (`uv sync --package <member> --no-dev`), which is what physically keeps Scrapy/Twisted/Playwright out of the API/scheduler/worker images (FR-003). Scrapy project package names (`price_monitor`, `price_monitor_browser`) follow master doc §5; library import packages are `app_shared` and `scrape_core` per the plan brief.

## Phase 0 & Phase 1 outputs

- Phase 0 research → [research.md](./research.md) (Python version, image pins, uv-workspace layout, PgBouncer transaction-pooling implications, dual-stack, non-root, engine hygiene, Celery fork-safety, Scrapyd auth).
- Phase 1 design → [data-model.md](./data-model.md) (structural entities, not DB tables), [contracts/](./contracts/) (health endpoint, service topology/exposure, environment variables), [quickstart.md](./quickstart.md) (bring-up & validation).

## Complexity Tracking

> No Constitution Check violations. Table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |
