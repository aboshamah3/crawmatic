# Phase 0 Research: Monorepo & Services Skeleton

Date: 2026-07-02. Resolves the two decisions the spec deferred to `/plan` (Python version; pinned image tags) plus the cross-cutting patterns later specs depend on. No `NEEDS CLARIFICATION` markers remained after `/clarify`; this file records concrete choices with rationale and alternatives.

---

## 1. Python version (`requires-python`)

- **Decision**: Python **3.13** — `requires-python = ">=3.13,<3.14"`. Base image `python:3.13.5-slim-bookworm`.
- **Rationale**: 3.13 is the current stable interpreter that the *entire* pinned stack supports as of 2026-07. The historical laggards for a new Python minor are Twisted (Scrapy's reactor) and scrapy-playwright; both have supported 3.13 since their 2024–2025 releases (Twisted 24.x, Scrapy ≥ 2.12, scrapy-playwright ≥ 0.0.42). FastAPI/Starlette, Celery 5.4/5.5, SQLAlchemy 2.0, psycopg 3.2, and Uvicorn all support 3.13. Upper bound `<3.14` prevents an accidental pull of a newer interpreter whose Twisted/scrapy-playwright wheels may not yet exist.
- **Alternatives considered**:
  - *3.12* — safest, but 3.13 is equally well-supported by the pinned stack today and gives a longer support runway; no dependency forces 3.12.
  - *3.14* (released Oct 2025) — rejected: at 2026-07 the Twisted + scrapy-playwright + Scrapy compatibility matrix for 3.14 is not yet uniformly green, and this is a skeleton where interpreter novelty buys nothing.
- **Enforcement**: single `requires-python` in the root `pyproject.toml`; every member inherits it. The base image tag is pinned to a patch release (`3.13.5`), never `3.13` or `latest`.

---

## 2. Image pinning (no `latest` — FR-014)

- **Decision** (widely-used stable tags; bump via a deliberate PR, never floating):

  | Component | Pinned tag | Notes |
  |-----------|-----------|-------|
  | Python base (all 5 app images) | `python:3.13.5-slim-bookworm` | Debian 12 slim; small, glibc (Playwright-friendly). |
  | Postgres | `postgres:17.5-bookworm` | PG 17 GA line; current stable major at 2026-07. |
  | PgBouncer | `edoburu/pgbouncer:v1.23.1-p3` | No first-party PgBouncer image on Docker Hub; `edoburu/pgbouncer` is the widely-used community image and supports env-driven config + `scram-sha-256`. |
  | Redis | `redis:7.4.2-bookworm` | 7.4 stable line; conservative, universally supported by Celery/redis-py. Redis 8 exists but 7.4 is the low-risk broker/locks choice for the skeleton. |
  | uv (build stage) | `ghcr.io/astral-sh/uv:0.7.13` | Copied as a static binary into build stages (`COPY --from=... /uv /bin/uv`); not a runtime image. |

- **Rationale**: every tag is a concrete patch/point release from a currently-maintained line, satisfying "pinned image versions, no `latest`" (§3, §4 hardening, FR-014). `-slim-bookworm` keeps the Python image small while retaining glibc, which Playwright's Chromium needs. The PgBouncer image is community-maintained because the project ships no official image; `edoburu/pgbouncer` is the de-facto standard and honours transaction-pooling + `scram-sha-256` config via env/`pgbouncer.ini`.
- **Alternatives considered**:
  - *`python:3.13.5-alpine`* — rejected: musl breaks Playwright Chromium and complicates psycopg/Twisted wheels.
  - *`bitnami/pgbouncer`* — viable alternative; `edoburu` chosen for its simpler env-var surface and long track record. Either can be substituted without changing the topology.
  - *Digest pinning (`@sha256:…`)* — stronger supply-chain guarantee and recommended for production, but digests can't be resolved in this offline planning environment; tasks may upgrade tag pins to digest pins during implementation. Documented as a follow-up, not a blocker.
  - *Redis 8.x* — deferred; 7.4 is the safer skeleton default and the split broker/locks-`noeviction` topology is a later-spec concern anyway (spec Assumptions).

---

## 3. uv workspace layout (FR-002, FR-003)

- **Decision**: One root `pyproject.toml` with `[tool.uv.workspace] members = ["apps/*", "libs/*"]` and a single `uv.lock`. Each member has its own `pyproject.toml` declaring only its dependencies; library members are referenced as workspace sources (`[tool.uv.sources] app_shared = { workspace = true }`). Each service Dockerfile installs only its member closure via `uv sync --package <member> --frozen --no-dev`.
- **Rationale**: satisfies "one root project, one lockfile, per-service installs" (§3 Packaging, §5). Installing per-member is the mechanism that physically excludes Scrapy/Twisted/Playwright from the API/scheduler/worker images (FR-003) — the boundary is enforced by what each `pyproject.toml` declares, not by convention alone. The future migration-job image (SPEC-02) will install from this same lockfile so migrations run under the exact locked versions.
- **Boundary rules baked in now**:
  - `libs/shared` (`app_shared`) declares **no** Scrapy/Twisted/Playwright dependency, and a unit test (`tests/unit/test_import_boundaries.py`) asserts importing those from within `app_shared`'s closure fails.
  - `libs/scrape-core` (`scrape_core`) may depend on `app_shared`; the reverse is forbidden and covered by the same boundary test.
  - Celery task names are string constants in `app_shared/task_names.py`; spiders will `send_task(name, ...)` and never import `apps/workers`.
- **Alternatives considered**: separate lockfiles per service (rejected — violates "one lockfile" and lets versions drift between the migration job and runtime); a flat single package (rejected — cannot enforce per-service dependency closures or independent deploys).

---

## 4. PgBouncer transaction pooling & DB engine hygiene (FR-011, FR-020)

- **Decision**: All application services connect to Postgres **only** through PgBouncer on port **6432** in **transaction pooling** mode. `app_shared/database.py` provides a lazy, per-process singleton engine created on first use (never at import time, never per request), with a small pool and pooler-safe driver settings.
- **Rationale** (§4 engine/process hygiene): transaction pooling multiplies connections (one pool per Scrapyd spider process + per Celery worker), so each process must own exactly one engine and health checks must reuse it — a per-request engine leaks pooled connections. Driver must be pooler-safe: for psycopg 3 disable server-side prepared statements (`prepare_threshold=None`) because prepared statements don't survive across pooled transactions; design for `SET LOCAL` / `pg_advisory_xact_lock` only (session state doesn't persist). These constraints are encoded as comments/settings in the stub now so later specs inherit them.
- **Skeleton scope**: `database.py` is a minimal stub — it constructs the engine lazily and exposes a session factory, but defines **no** models/metadata and runs **no** queries at boot (a health check must not require a live DB in the skeleton). Full models arrive in SPEC-02.
- **Alternatives considered**: eager engine at import (rejected — breaks fork-safety and creates connections before config is validated); session pooling mode on PgBouncer (rejected — the spec mandates transaction pooling and its constraints); direct-to-Postgres from app services (rejected — FR-011; the only future exception is the one-shot migration job, out of scope here).

---

## 5. Celery fork-safety (§4 engine/process hygiene)

- **Decision**: `apps/workers/app/workers/celery_app.py` registers a `worker_process_init` handler that disposes any SQLAlchemy engine inherited from the parent/prefork parent before first use, forcing each worker process to build its own engine lazily.
- **Rationale**: Celery prefork forks worker processes from a parent; a live libpq connection copied across `fork()` is unsafe. Disposing on `worker_process_init` guarantees each child rebuilds its own pool. Established now (as a hook that calls `app_shared`'s engine-dispose helper) because SPEC-02+ tasks rely on it.
- **Skeleton scope**: the hook is wired and the worker boots via `celery -A app.workers.celery_app worker --loglevel=info`; it registers no real tasks yet.
- **Alternatives considered**: relying on connection recycling / `pool_pre_ping` alone (rejected — does not address inheriting a live fd across fork); `--pool=solo`/threads (rejected — prefork is the intended production model and must be fork-safe from the start).

---

## 6. Container & network hardening (FR-013, FR-015, FR-016)

- **Non-root (FR-015)**: every app Dockerfile creates an unprivileged user (e.g. `appuser`, fixed UID/GID) and drops to it via `USER` before the start command. Infra images (postgres/redis/pgbouncer) run as their images' non-root defaults.
- **Dual-stack binds (FR-016)**: services reachable on internal networking bind IPv6 as well as IPv4, because platform-internal networks (e.g. Railway) are IPv6-only and an IPv4-only `0.0.0.0` bind is unreachable there:
  - API: `uvicorn ... --host :: --port $PORT` (a dual-stack `::` bind accepts IPv4 via v4-mapped addresses on Linux).
  - Scrapyd nodes: `bind_address = ::` in `scrapyd.conf`.
  - PgBouncer: `listen_addr = *` (binds both families).
- **Public exposure (FR-013)**: in `docker-compose.yml` only `api` publishes a host port (`${API_PORT}:${API_PORT}`). Scheduler, worker, both Scrapyd nodes, postgres, pgbouncer, and redis expose ports only on the internal compose network (`expose:`), never `ports:`.
- **Alternatives considered**: IPv4-only `0.0.0.0` binds (rejected — unreachable on IPv6-only internal networks, FR-016); publishing Scrapyd for convenience (rejected — Scrapyd's `addversion.json` is RCE-adjacent and must never be public, §4, FR-013).

---

## 7. Scrapyd basic auth (FR-012, FR-008/009)

- **Decision**: both Scrapyd nodes bake their Scrapy project at build time and enable HTTP basic auth in `scrapyd.conf` (`username = ${SCRAPYD_USERNAME}`, `password = ${SCRAPYD_PASSWORD}`), rendered from env at container start. The browser node additionally installs Playwright Chromium at build (`playwright install --with-deps chromium`) and sets low concurrency in `settings.py`.
- **Rationale**: Scrapyd's API includes `addversion.json` (code upload) — internal networking alone is insufficient, so auth is mandatory (§4). Baking the project avoids reliance on runtime spider uploads in production (FR-008). Building Playwright browsers into the image (not at runtime) makes the browser node reproducible and offline-bootable (FR-009); low concurrency respects the "keep browser concurrency low" rule.
- **Skeleton scope**: each Scrapyd project carries an empty `spiders/` package (no real spiders); the point is that the node boots, requires auth, and would host the baked project. Any future component calling Scrapyd will authenticate every `schedule.json` call.
- **Alternatives considered**: runtime spider upload (rejected — §4 forbids relying on it in production); no auth behind "internal only" (rejected — FR-012, §4 explicitly call this out as RCE risk); installing Playwright at container start (rejected — non-reproducible, needs network at boot).

---

## 8. Redis (FR-010)

- **Decision**: a single `redis:7.4.2-bookworm` instance for the skeleton, present and reachable by services that will use it (worker for broker/locks later).
- **Rationale**: the spec Assumptions and §4 make the split broker vs. locks/limits (`noeviction`) instances a *deployment* concern deferred to later specs; the skeleton only needs Redis running so services can point at `REDIS_URL`.
- **Alternatives considered**: two instances now (rejected — premature for a skeleton; adds compose complexity with no skeleton benefit). The split is documented as a later-spec TODO.

---

## 9. Local PgBouncer auth (spec Assumptions)

- **Decision**: local compose uses PgBouncer `auth_type = trust`; every deployed environment uses `scram-sha-256` with a real userlist.
- **Rationale**: §4 states "`trust` is acceptable only on a developer's local machine." The `.env.example` and compose comments make the deployed requirement explicit so it's not mistaken for a production default.
- **Alternatives considered**: `scram-sha-256` locally (viable but adds a userlist-generation step that slows first-run bring-up; deferred to deployment specs, matching the master doc).

---

## Open follow-ups (not blockers for this phase)

- Upgrade image tag pins to `@sha256` digests during implementation for supply-chain integrity (§4 hardening intent).
- Split Redis into broker + locks/limits (`noeviction`) instances — later spec.
- The one-shot migration job that connects **directly** to Postgres — SPEC-02 (Database Foundation); it is the single allowed exception to PgBouncer routing.
