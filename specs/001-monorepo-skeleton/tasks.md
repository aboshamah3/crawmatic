---
description: "Task list for Monorepo & Services Skeleton (SPEC-01)"
---

# Tasks: Monorepo & Services Skeleton

**Input**: Design documents from `/specs/001-monorepo-skeleton/`

**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/ (health, service-topology, environment), quickstart.md

**Tests**: Tests ARE in scope for this feature — the plan's Testing section explicitly requests a health-endpoint test, import-boundary tests, and an optional compose smoke test. No business-logic tests are generated (there is no business logic).

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Every task lists an exact file path or artifact.

## Path Conventions

- **Multi-service monorepo (uv workspace)** rooted at repository root `/srv/crawmatic/crawmatic/`.
- Application members under `apps/`, library members under `libs/`, tests under `tests/`.
- Paths below are repo-root-relative and taken verbatim from plan.md → Project Structure.

## Scope Boundary (enforced — do NOT generate tasks for these)

Explicitly OUT OF SCOPE for SPEC-01 (deferred to later specs; no tasks here):

- DB tables / SQLAlchemy models / Alembic migrations (the one-shot direct-to-Postgres migration job included) — `alembic/` is scaffolded EMPTY only.
- Authentication / API keys / RLS / `workspace_id` surfaces.
- Scraping, extraction, item models, spider logic, rate limiting, confidence, DB pipelines.
- Any API endpoint beyond `GET /health`.
- Split Redis broker/locks instances (single Redis in the skeleton).

Task tasks that would introduce the above are intentionally omitted.

---

## Phase 1: Setup (uv Workspace & Packaging Skeleton)

**Purpose**: Establish the uv workspace, per-member packaging declarations, and the single lockfile so every later phase can `uv sync --package <member>`.

- [X] T001 Create root uv workspace file `pyproject.toml` with `[tool.uv.workspace] members = ["apps/*", "libs/*"]`, `requires-python = ">=3.13,<3.14"`, and shared dev tooling (pytest); no runtime deps at root (plan.md Structure Decision, FR-002)
- [X] T002 [P] Create `apps/api/pyproject.toml` declaring member `api` deps: fastapi, uvicorn, `app_shared` (workspace) — must NOT declare scrapy/twisted/playwright (FR-002, FR-003)
- [X] T003 [P] Create `apps/scheduler/pyproject.toml` declaring member `scheduler` deps: `app_shared` only (FR-002, FR-003)
- [X] T004 [P] Create `apps/workers/pyproject.toml` declaring member `workers` deps: celery, redis, `app_shared` — no scrapy/twisted/playwright (FR-002, FR-003)
- [X] T005 [P] Create `apps/scrapers/pyproject.toml` declaring member `scrapers` deps: scrapy, scrapyd, `scrape_core`, `app_shared` (FR-001, FR-004)
- [X] T006 [P] Create `apps/scrapers-browser/pyproject.toml` declaring member `scrapers-browser` deps: scrapy, scrapyd, scrapy-playwright, playwright, `scrape_core`, `app_shared` (FR-001, FR-004, FR-009)
- [X] T007 [P] Create `libs/shared/pyproject.toml` declaring member `app_shared` deps: sqlalchemy, psycopg, pydantic-settings — MUST NOT depend on scrapy/twisted/playwright and MUST NOT depend on `scrape_core` (FR-003)
- [X] T008 [P] Create `libs/scrape-core/pyproject.toml` declaring member `scrape_core`; MAY depend on `app_shared`, never the reverse (FR-003, FR-004)
- [X] T009 [P] Create `.dockerignore` at repo root excluding `.git`, `.venv`, `__pycache__`, `.env`, `specs/`, test caches from build contexts
- [X] T010 [P] Scaffold `alembic/versions/.gitkeep` — empty Alembic directory, NO `env.py`/`alembic.ini`/migrations authored (data-model.md; migrations deferred to SPEC-02)
- [X] T011 Generate the single workspace lockfile `uv.lock` by running `uv lock` at repo root (depends on T001–T008) (FR-002)

**Checkpoint**: `uv sync` resolves; the workspace graph and per-member closures are locked.

---

## Phase 2: Foundational (Shared Library Code — Blocking Prerequisites)

**Purpose**: The `app_shared` and `scrape_core` packages every application member imports, plus the cross-cutting patterns (env config fail-fast, lazy per-process DB engine, task-name constants) that later specs depend on.

**⚠️ CRITICAL**: No user story can be completed until this phase is done — all five app members import `app_shared`.

- [X] T012 [P] Create `libs/shared/app_shared/__init__.py` (package marker; keep import-light — MUST NOT import scrapy/twisted/playwright, and MUST NOT trigger engine creation at import) (FR-003, FR-020)
- [X] T013 [P] Create `libs/shared/app_shared/config.py` — pydantic-settings `Settings` enumerating every variable in `contracts/environment.md` (DATABASE_URL, DB_POOL_SIZE, DB_MAX_OVERFLOW, REDIS_URL, SCRAPYD_HTTP_URLS, SCRAPYD_BROWSER_URLS, SCRAPYD_USERNAME, SCRAPYD_PASSWORD, API_PORT, API_PUBLIC_BASE_URL, INTERNAL_API_BASE_URL); `API_PORT` is the single canonical API-port variable (the container's uvicorn `$PORT` is derived from it by compose — see T029/T034, not a separately-configured var); fail fast with a clear error on missing required vars; parse `SCRAPYD_*_URLS` as comma-separated pools even when length 1 (FR-017, FR-018) [analyze I1]
- [X] T014 [P] Create `libs/shared/app_shared/database.py` — lazy per-process SQLAlchemy engine + session helper created on first use (never at import time, never per request), honoring DB_POOL_SIZE/DB_MAX_OVERFLOW and transaction-pooling operation; expose a `dispose_engine()` for the Celery fork hook (FR-020, plan §VIII)
- [X] T015 [P] Create `libs/shared/app_shared/task_names.py` — empty string-constant stub for Celery task names (no tasks defined yet) (FR-004, plan §V)
- [X] T016 [P] Create `libs/scrape-core/scrape_core/__init__.py` — package marker only; may import `app_shared`, no scraping logic yet (FR-004)

**Checkpoint**: Shared foundation ready — every app member can import `app_shared` (and scrapers `scrape_core`); user stories can begin.

---

## Phase 3: User Story 1 - Bring the whole stack up locally (Priority: P1) 🎯 MVP

**Goal**: A clean checkout boots all eight components with one command; the API answers `GET /health`; every app service is wired to reach Postgres only through PgBouncer.

**Independent Test**: `docker compose up --build -d` → `docker compose ps` shows all 8 running (api healthy); `curl -fsS http://localhost:${API_PORT}/health` → `{"status":"ok"}`; `DATABASE_URL` host is `pgbouncer:6432`, never `postgres:5432` (quickstart §2–§4; SC-001, SC-002, SC-003).

### Implementation for User Story 1

- [ ] T017 [P] [US1] Create `apps/api/app/__init__.py` (package marker)
- [ ] T018 [US1] Create `apps/api/app/main.py` — FastAPI app exposing ONLY `GET /health` returning 200 `{"status":"ok"}`; dependency-free (no DB/Redis/Scrapyd touch, no per-request engine); no other routes (FR-005, contracts/health.md)
- [ ] T019 [P] [US1] Create `apps/scheduler/app/__init__.py` and `apps/scheduler/app/scheduler/__init__.py` (package markers)
- [ ] T020 [US1] Create `apps/scheduler/app/scheduler/scheduler_app.py` — boots to a running loop via `python -m app.scheduler.scheduler_app`; no business logic (FR-006, contracts/service-topology.md)
- [ ] T021 [P] [US1] Create `apps/workers/app/__init__.py` and `apps/workers/app/workers/__init__.py` (package markers)
- [ ] T022 [US1] Create `apps/workers/app/workers/celery_app.py` — Celery app (broker/back-end from `REDIS_URL`) with a `worker_process_init` hook that disposes the inherited engine (`app_shared.database.dispose_engine`) for fork-safety; no tasks/business logic (FR-007, FR-020, plan §VIII)
- [ ] T023 [P] [US1] Create `apps/scrapers/scrapy.cfg` — points at `price_monitor.settings`, declares scrapyd deploy target (plan Structure)
- [ ] T024 [P] [US1] Create `apps/scrapers/scrapyd.conf` — `http_port = 6800`, `bind_address = ::` (dual-stack), HTTP basic auth `username`/`password` sourced from `SCRAPYD_USERNAME`/`SCRAPYD_PASSWORD` (FR-008, FR-012, FR-016, contracts/service-topology.md)
- [ ] T025 [P] [US1] Create `apps/scrapers/price_monitor/__init__.py`, `apps/scrapers/price_monitor/settings.py` (default Scrapy HTTP settings; imports `scrape_core`), and `apps/scrapers/price_monitor/spiders/__init__.py` (no spiders) (FR-004, FR-008)
- [ ] T026 [P] [US1] Create `apps/scrapers-browser/scrapy.cfg` — points at `price_monitor_browser.settings`
- [ ] T027 [P] [US1] Create `apps/scrapers-browser/scrapyd.conf` — `http_port = 6800`, `bind_address = ::`, basic auth from env, `max_proc = 1` (concrete low browser concurrency) (FR-009, FR-012, FR-016) [analyze A1]
- [ ] T028 [P] [US1] Create `apps/scrapers-browser/price_monitor_browser/__init__.py`, `.../settings.py` (scrapy-playwright download handler, `CONCURRENT_REQUESTS = 2` and `PLAYWRIGHT_MAX_CONTEXTS = 1` for low browser concurrency; imports `scrape_core`), and `.../spiders/__init__.py` (FR-004, FR-009) [analyze A1]
- [ ] T029 [P] [US1] Create `apps/api/Dockerfile` — base `python:3.13.5-slim-bookworm`, copy `uv` from `ghcr.io/astral-sh/uv:0.7.13`, `uv sync --package api --no-dev`, create + `USER` non-root, `ENV PORT=8000` default, CMD `uvicorn app.main:app --host :: --port ${PORT}` (compose injects `PORT=${API_PORT}` so the listening port always equals the published port — see T034) (FR-014, FR-015, FR-016, FR-019) [analyze I1]
- [ ] T030 [P] [US1] Create `apps/scheduler/Dockerfile` — pinned base, `uv sync --package scheduler --no-dev`, non-root `USER`, CMD `python -m app.scheduler.scheduler_app` (FR-014, FR-015, FR-019)
- [ ] T031 [P] [US1] Create `apps/workers/Dockerfile` — pinned base, `uv sync --package workers --no-dev`, non-root `USER`, CMD `celery -A app.workers.celery_app worker --loglevel=info` (FR-014, FR-015, FR-019)
- [ ] T032 [P] [US1] Create `apps/scrapers/Dockerfile` — pinned base, `uv sync --package scrapers --no-dev`, bake the Scrapy project + `scrapyd.conf` at build (no runtime uploads), non-root `USER`, CMD `scrapyd` (FR-008, FR-014, FR-015, FR-019)
- [ ] T033 [P] [US1] Create `apps/scrapers-browser/Dockerfile` — pinned base, `uv sync --package scrapers-browser --no-dev`, `playwright install --with-deps chromium` baked at build, bake project + `scrapyd.conf`, non-root `USER`, CMD `scrapyd` (FR-009, FR-014, FR-015, FR-019)
- [ ] T034 [US1] Create root `docker-compose.yml` wiring all 8 components: build the 5 app services (context per member) + pinned infra images `postgres:17.5-bookworm`, `edoburu/pgbouncer:v1.23.1-p3`, `redis:7.4.2-bookworm`; `env_file: .env`; `depends_on` ordering (postgres → pgbouncer; api/scheduler/worker → pgbouncer); pgbouncer `transaction` pool mode; app services target `pgbouncer:6432` via `DATABASE_URL` (never postgres:5432); healthcheck for `api` hitting `GET /health`; for the `api` service set `environment: PORT=${API_PORT}` and publish only `api` via `ports: "${API_PORT}:${API_PORT}"` so host, container-listen, and healthcheck ports are the single `API_PORT` value (SC-001, SC-002, SC-003, FR-010, FR-011, FR-014) [analyze I1]

### Test for User Story 1

- [ ] T035 [US1] Create `tests/integration/test_health.py` — start the FastAPI app (TestClient) and assert `GET /health` == 200 and body `{"status":"ok"}` (FR-005, SC-002, contracts/health.md)

**Checkpoint**: One command brings all eight components healthy; `/health` returns 200; DB routing points only at PgBouncer. MVP is deliverable here.

---

## Phase 4: User Story 2 - Reach each service on its expected boundary (Priority: P1)

**Goal**: Only the API is publicly reachable; both Scrapyd nodes are internal-only and require basic auth; internal-facing services bind dual-stack.

**Independent Test**: From `worker`, unauthenticated `curl http://scrapers:6800/daemonstatus.json` → 401 and authenticated (`-u $SCRAPYD_USERNAME:$SCRAPYD_PASSWORD`) → 200 (same for `scrapers-browser`); `curl http://localhost:6800/` from host is refused; only `api` shows a host port (quickstart §5; SC-004, SC-005).

> Note: Scrapyd basic-auth config (T024/T027) and dual-stack binds are authored in US1 because the nodes cannot boot without their conf; US2 hardens the compose exposure model and verifies the security boundary. This is the one accepted cross-story file touch (US2 edits the US1 `docker-compose.yml`); run US2 after US1.

### Implementation for User Story 2

- [ ] T036 [US2] Edit `docker-compose.yml` to make exposure explicit: `api` is the ONLY service with `ports:`; add `expose:` (internal-network only, no host publish) for `scrapers` (6800), `scrapers-browser` (6800), `pgbouncer` (6432), `redis` (6379), `postgres` (5432), and no ports for `scheduler`/`worker` (FR-013, SC-005)
- [ ] T037 [US2] Confirm dual-stack binds are wired across artifacts: `api` command `--host ::` (T029/T034), both `scrapyd.conf` `bind_address = ::` (T024/T027), and `pgbouncer` bound dual-stack via `LISTEN_ADDR=*` (from `PGBOUNCER_LISTEN_ADDR`, set on the pgbouncer service in `docker-compose.yml`) (FR-016) [analyze U1]
- [ ] T038 [US2] Verify Scrapyd auth boundary per quickstart §5: both nodes reject credential-less requests (401) and accept authenticated internal requests (200) (FR-012, SC-004)
- [ ] T039 [US2] Verify public-exposure boundary per quickstart §5: only `api` is reachable on the host; `scrapers`/`scrapers-browser`/infra are not publicly reachable (FR-013, SC-005)

**Checkpoint**: Security boundary matches the topology contract — public API, internal authenticated Scrapyd, dual-stack internal binds.

---

## Phase 5: User Story 3 - Configure services from the environment (Priority: P2)

**Goal**: Every service reads connection targets, ports, and credentials from the environment; a committed `.env.example` lets a new developer bring the stack up without reading source.

**Independent Test**: `cp .env.example .env && docker compose up --build -d` brings the stack up unchanged; services pick up configured values (ports, pooler host, Redis, Scrapyd URLs); a missing required var makes the affected service fail fast (quickstart §1; SC-006, FR-017).

### Implementation for User Story 3

- [ ] T040 [P] [US3] Create `.env.example` at repo root enumerating EVERY variable in `contracts/environment.md` with non-secret placeholders: `DATABASE_URL` (host `pgbouncer:6432`), `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `REDIS_URL`, `SCRAPYD_HTTP_URLS`, `SCRAPYD_BROWSER_URLS`, `SCRAPYD_USERNAME`, `SCRAPYD_PASSWORD`, `API_PORT` (canonical; compose derives the container `PORT` from it), `API_PUBLIC_BASE_URL`, `INTERNAL_API_BASE_URL`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `PGBOUNCER_AUTH_TYPE` (`trust` local), `PGBOUNCER_POOL_MODE` (`transaction`), `PGBOUNCER_LISTEN_ADDR` (`*` — dual-stack IPv4+IPv6 bind, maps to the pgbouncer image's `LISTEN_ADDR`); no real secrets (FR-017, FR-016, SC-006) [analyze I1, U1]
- [ ] T041 [P] [US3] Create/extend root `README.md` with a bring-up section: prerequisites (Docker Compose v2, uv 0.7.13), `cp .env.example .env`, `docker compose up --build -d`, and the `/health` + Scrapyd-auth validation steps — enough for a new dev to boot from a clean checkout without reading source (SC-006, quickstart.md)
- [ ] T042 [US3] Verify env-driven config behavior against `libs/shared/app_shared/config.py`: a missing required variable fails fast with a clear error, and `SCRAPYD_HTTP_URLS`/`SCRAPYD_BROWSER_URLS` are treated as pools even with a single URL (FR-017, FR-018)

**Checkpoint**: Same images run across local/deployed environments via `.env` only; new-developer bring-up path is documented and proven.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Cross-cutting validation that spans all members.

- [ ] T043 [P] Create `tests/unit/test_import_boundaries.py` — assert `app_shared` cannot import `scrapy`/`twisted`/`playwright` and does not depend on `scrape_core` (no reverse edge) (FR-003, data-model.md)
- [ ] T044 [P] (Optional) Create `tests/integration/test_compose_smoke.py` — bring the stack up and assert all eight components reach running/healthy, aligned with quickstart §2 (SC-001)
- [ ] T045 Run the full `quickstart.md` validation end-to-end and confirm the SC mapping: SC-001 (one-command 8-up), SC-002 (/health 200), SC-003 (PgBouncer-only), SC-004 (Scrapyd auth), SC-005 (only api public), SC-006 (clean-checkout bring-up)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately. T011 (`uv lock`) depends on T001–T008.
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories (every app member imports `app_shared`).
- **User Story 1 (Phase 3)**: Depends on Foundational. Delivers the MVP.
- **User Story 2 (Phase 4)**: Depends on US1 (edits/verifies the `docker-compose.yml` and Scrapyd nodes created in US1).
- **User Story 3 (Phase 5)**: Depends on Foundational (`config.py`); the bring-up verification depends on US1's compose. Independently testable via `.env.example` + README.
- **Polish (Phase 6)**: Depends on the phases it validates (import-boundary test needs Foundational; smoke/quickstart need US1–US2).

### User Story Dependencies

- **US1 (P1)**: After Foundational — no dependency on other stories. This is the MVP.
- **US2 (P1)**: After US1 — hardens/verifies the boundary on US1's artifacts (single accepted cross-story file: `docker-compose.yml`).
- **US3 (P2)**: After Foundational — `.env.example`/README are independent; the live bring-up check reuses US1's compose.

### Within Each User Story

- Package markers (`__init__.py`) before the modules that live in them.
- Service module before its Dockerfile CMD references it; Dockerfiles before `docker-compose.yml` builds them.
- `config.py`/`database.py`/`task_names.py` (Foundational) before any app module that imports them.

### Parallel Opportunities

- Setup: T002–T010 are all `[P]` (distinct files) after T001; T011 waits for the pyprojects.
- Foundational: T012–T016 are all `[P]` (distinct files).
- US1: all package markers, both Scrapy projects, and all five Dockerfiles marked `[P]` run in parallel; `docker-compose.yml` (T034) waits for the Dockerfiles; `test_health.py` (T035) waits for `main.py` (T018).
- US3: `.env.example` (T040) and README (T041) are `[P]`.
- Polish: T043 and T044 are `[P]`.

---

## Parallel Example: User Story 1 Dockerfiles

```bash
# After Foundational + app modules exist, build all five Dockerfiles in parallel:
Task: "Create apps/api/Dockerfile (T029)"
Task: "Create apps/scheduler/Dockerfile (T030)"
Task: "Create apps/workers/Dockerfile (T031)"
Task: "Create apps/scrapers/Dockerfile (T032)"
Task: "Create apps/scrapers-browser/Dockerfile (T033)"
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1: Setup (workspace + lockfile).
2. Phase 2: Foundational (shared libs — CRITICAL, blocks all stories).
3. Phase 3: US1 — all eight up + `/health` + PgBouncer routing.
4. **STOP and VALIDATE**: quickstart §2–§4 (SC-001, SC-002, SC-003).

### Incremental Delivery

1. Setup + Foundational → workspace ready.
2. US1 → all-8-up + health (MVP; SC-001/002/003).
3. US2 → security boundary hardened + verified (SC-004/005).
4. US3 → env-driven config + new-dev bring-up (SC-006).
5. Polish → import-boundary + smoke + full quickstart run.

---

## Success-Criteria Coverage

| Criterion | Covered by |
|-----------|------------|
| **SC-001** all 8 up, one command | T034 (compose), T044/T045 |
| **SC-002** /health 200 100% | T018, T034 (healthcheck), T035 |
| **SC-003** PgBouncer-only DB | T013/T014 (config/engine), T034 (DATABASE_URL→pgbouncer) |
| **SC-004** Scrapyd auth reject/accept | T024, T027, T038 |
| **SC-005** only api public | T034/T036, T039 |
| **SC-006** clean-checkout bring-up | T040 (.env.example), T041 (README), T045 |

---

## Notes

- `[P]` tasks touch different files with no incomplete-task dependency.
- `[Story]` labels map tasks to spec.md user stories (US1/US2/US3) for traceability; Setup/Foundational/Polish carry no story label by design.
- The only accepted cross-story same-file edit is `docker-compose.yml` (created in US1, exposure-hardened in US2); US2 must run after US1.
- SCOPE GUARD: no DB models/migrations, no auth/API keys, no scraping/extraction logic, and no API endpoints beyond `/health` are generated — these are deferred to SPEC-02+.
- Commit after each task or logical group; stop at any checkpoint to validate a story independently.
