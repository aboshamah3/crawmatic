# Implementation Plan: Jobs & Orchestration

**Branch**: `008-jobs-orchestration` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/008-jobs-orchestration/spec.md`

## Summary

Deliver the orchestration layer that sits between the API and the SPEC-07 scraping runtime: durable **job/target** records, the operator **run endpoints**, and the **`scrape_dispatch`** worker path that expands a job into domain/mode-grouped Scrapyd batches — reusing the SPEC-07 authenticated, idempotent `ScrapydDispatchClient` and the generic HTTP spider unchanged. A run creates a `scrape_jobs` row (scope MATCH/VARIANT, `type=MANUAL`, `source=API`, `requested_by`=principal) plus one `scrape_job_targets` row per active match (`unique(scrape_job_id, match_id)`), enqueues a dispatch task, and returns the job id immediately; the scrape itself runs asynchronously in Scrapyd. Status/results endpoints read the job + its targets, workspace-scoped. Counters are **aggregated** from `scrape_job_targets` (never per-target increments on the hot job row), status is finalized **deterministically** (COMPLETED / PARTIAL_FAILED / FAILED), a zero-target job finalizes as COMPLETED immediately with no dispatch, and a stalled batch is detected + re-dispatched past a configured timeout under the same idempotency guard.

Concretely this feature adds:

- **`libs/shared/app_shared`**:
  - `models/jobs.py` — two ORM models `ScrapeJob` + `ScrapeJobTarget` (exact §22 shapes), both `WorkspaceScopedBase` + registered in `WORKSPACE_OWNED_MODELS`; `unique(scrape_job_id, match_id)` on targets; `unique(workspace_id, id)` on jobs so a target composite-FKs its parent job workspace-locally; scope/match refs are soft UUID columns (§22 soft-ref philosophy). `created_at` only (no `updated_at`, per §22).
  - `enums.py` (extend): `ScrapeScope`, `ScrapeJobType`, `ScrapeJobStatus`, `ScrapeJobSource`, `ScrapeTargetStatus`. Target `error_code` reuses the existing `ScrapeErrorCode` (§34) vocabulary.
  - `jobs/` — **pure, scraping-free** orchestration logic, unit-testable off any DB/Redis: `batching.py` (`plan_batches` groups targets by competitor-domain + mode into HTTP batches of 50–200), `nodes.py` (`select_node` — deterministic hash-by-domain node selection, FR-014), `lifecycle.py` (`resolve_finalized_status` — the deterministic COMPLETED/PARTIAL_FAILED/FAILED rule + `stall_window` bucketing), `targets.py` (`mark_target` state-transition writer + `aggregate_counts` GROUP-BY-status), `service.py` (`create_match_job` / `create_variant_job` — active-match resolution, job+targets creation, enqueue, zero-target immediate finalize).
  - `messaging.py` — a lazy Celery **producer** seam (`enqueue(name, *, queue, kwargs)`) bound to `REDIS_URL`, so the API/scheduler enqueue `scrape_dispatch` work by task *name* (via `task_names`) without importing `apps/workers` (keeps the worker import closure out of the API — Constitution I).
  - `scrapyd/client.py` (extend): optional `node_url` arg on `schedule(...)` (defaults to `SCRAPYD_HTTP_URLS[0]`, back-compat) so the dispatch task can target the deterministically-selected node.
  - `task_names.py` (extend): `SCRAPE_DISPATCH_JOB`, `SCRAPE_RECOVER_STALLED`, `SCRAPE_FINALIZE_JOBS` constants.
  - `config.py` (extend): `SCRAPE_DISPATCH_HTTP_BATCH_MAX=200`, `SCRAPE_DISPATCH_HTTP_BATCH_MIN=50`, `SCRAPE_STALL_TIMEOUT_SECONDS=900`.
  - `repository.py` / `models/__init__.py` (extend): register + re-export the two new models.
- **`apps/api`**:
  - `routers/jobs.py` — `POST /v1/jobs/run/match/{match_id}`, `POST /v1/jobs/run/variant/{variant_id}`, `GET /v1/jobs/{job_id}`, `GET /v1/jobs/{job_id}/results`; on the SPEC-03 auth seam, scope-gated `jobs:read`/`jobs:write`, all reads/writes through `scoped_select`/`scoped_get` + RLS, delegating creation to `app_shared.jobs.service`.
  - `schemas/jobs.py` — `JobRunResponse` (id + status), `JobResponse` (status/type/scope/counts/timestamps), `JobResultsResponse` (per-target match/status/error_code).
  - `main.py` (extend): mount `jobs.router`.
- **`apps/workers`**:
  - `tasks_jobs.py` — `dispatch_job` (`scrape_dispatch` queue): load the job + its targets scoped, set RUNNING + `started_at`, `plan_batches`, and for each batch call `ScrapydDispatchClient.schedule(..., node_url=select_node(domain), batch_index=...)` (idempotent). `recover_stalled_batches` + `finalize_jobs`/`refresh_job_counters` (`maintenance` queue): scan non-terminal jobs, re-dispatch unprogressed targets past the stall timeout (stall-window-bucketed batch key), aggregate counters, finalize deterministically.
  - `celery_app.py` (extend): register the `scrape_dispatch` + `maintenance` queues/routes. **Fork-safety hook already present** (`worker_process_init` → `dispose_engine`, SPEC-01) — FR-016 is satisfied by the existing hook; this spec adds the first DB-touching tasks that rely on it and asserts it in tests.
- **repo root**: one Alembic migration creating `scrape_jobs` + `scrape_job_targets` + `emit_rls_policy` on both, chained onto the current head `2db33dea5e14`, single head preserved.

Everything DB/Redis/Scrapyd-independent is fully unit-tested **here** (batching + 50–200 bounds, deterministic node selection, the finalized-status rule incl. boundary values, counter aggregation with a fake session, job-creation service incl. the zero-target immediate-COMPLETED path, dispatch-task idempotency with a fake client/redis, stall re-dispatch target-exclusion logic, model/unique/RLS DDL render via offline `alembic upgrade head --sql`, import boundaries, workspace-scoping guard, endpoint request/response with a dependency-overridden session). Live-stack items (real run-match/run-variant end-to-end, actual `unique(scrape_job_id, match_id)` enforcement, RLS row denial, real `schedule.json` dispatch) are authored and **skip cleanly** where no Postgres/Redis/Scrapyd is reachable — matching the SPEC-02→07 deferred-verification pattern (no container engine in this build env).

## Technical Context

**Language/Version**: Python 3.13 (`requires-python = ">=3.13,<3.14"`; uv workspace). Use `uv sync --all-packages`.

**Primary Dependencies**:
- Existing: FastAPI (API service), SQLAlchemy 2.0 (sync) + PostgreSQL dialect (`insert(...).on_conflict_do_nothing` for the set-based target insert; `select(status, func.count()).group_by(status)` for counter aggregation), Alembic, `redis`, `requests` (via the reused SPEC-07 `ScrapydDispatchClient`), Celery (worker service + the new `app_shared.messaging` producer seam).
- Reused unchanged from SPEC-07: `app_shared.scrapyd.client.ScrapydDispatchClient` (authenticated `schedule.json` + Redis `SET NX` idempotency), the `generic_price_spider` (`apps/scrapers`), the `worker_process_init` fork-safety hook (`apps/workers/app/workers/celery_app.py`), the observation/current-price persistence path.
- Import boundary (enforced by `tests/unit/test_import_boundaries.py`): `app_shared` MUST NOT import scrapy/twisted/playwright/fastapi. The new `app_shared.jobs.*` + `app_shared.messaging` are pure orchestration/messaging (SQLAlchemy + celery only) — no scrapy/twisted. The API imports `app_shared.jobs.service`/`messaging`, never `apps/workers`.

**Storage**: PostgreSQL 17 via PgBouncer (transaction pooling). Two new **non-partitioned, workspace-owned** tables (`scrape_jobs`, `scrape_job_targets`), RLS enabled+forced in the creating migration; workspace context set per-transaction (`set_config('app.workspace_id', :wsid, true)`). Redis for the reused dispatch idempotency guard (`dispatched:{scrape_job_id}:{batch_index}` via `SET NX`) and Celery broker.

**Testing**: pytest. DB/Redis/Scrapyd-independent logic unit-tested here (pure batching/node/lifecycle/aggregation, service + dispatch orchestration with fakes, offline `alembic upgrade head --sql`). Live-stack tests authored + skip-marked (no Postgres/Redis/Scrapyd in this env).

**Target Platform**: Linux containers. Only `apps/api` is publicly exposed; the `scrape_dispatch`/`maintenance` Celery workers + Scrapyd nodes are internal-only, basic-auth protected.

**Project Type**: Backend monorepo (uv workspace). Spans `libs/shared/app_shared` (models, enums, pure jobs logic, messaging seam, config, task-name constants, the extended Scrapyd client), `apps/api` (router + schemas), `apps/workers` (dispatch + maintenance tasks + queue wiring), plus repo-root Alembic.

**Performance Goals**: Counters are **aggregated** — one `GROUP BY status` read + one `UPDATE` per refresh/finalize, so the write count on the job row does **not** grow with target count (SC-004). Dispatch **batches** by domain/mode (HTTP 50–200) so a large job produces a batch count tracking domain/mode grouping, never one Scrapyd job per URL (SC-008). Active-match resolution + target insert are set-based (one scoped `SELECT`, one bulk `INSERT`), never per-match loops. Node selection is O(1) deterministic hashing.

**Constraints**: Idempotent dispatch is mandatory — the reused Redis `SET NX` guard on `(scrape_job_id, batch_index)` neutralizes at-least-once duplicate delivery (no double Scrapyd run, SC-003). Node selection is deterministic (hash-by-domain) so two retries of a batch always resolve to the same node (FR-014). Stall re-dispatch excludes already-progressed and (where available) in-flight-locked targets and uses a stall-window-bucketed batch key so each recovery cycle is itself idempotent (FR-015). Job counters never per-target increments (FR-018). Finalization is deterministic (FR-019); zero-target → COMPLETED, no dispatch (FR-020). Both tables carry `workspace_id` + RLS; no query fetches a job/target by PK alone. Transaction-pooling-safe only (`SET LOCAL`/`set_config(...,true)`; no session advisory locks). Celery prefork fork-safety via the existing `worker_process_init` engine disposal (FR-016).

**Scale/Scope**: Foundation for 10k–20k matches/workspace (§39). Only match- and variant-scoped run endpoints ship here; product/group/competitor/workspace runs + the scheduler (SCHEDULED trigger) are later specs (the job model carries their scope fields but the endpoints are not exposed). Price analysis / current-price update / alerting are SPEC-09 (this spec ends at persisting job/target outcomes). In-flight fencing-token match locks + distributed rate limiting are SPEC-11 — this spec relies on the idempotency guard + `unique(scrape_job_id, match_id)` + target-state checks and integrates with locks where present.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Relevance | How this plan satisfies it |
|-----------|-----------|----------------------------|
| **I. API-First / Service boundaries** | Orchestration spans API + worker | FastAPI creates the job + enqueues by task *name* through the `app_shared.messaging` producer seam; the `scrape_dispatch`/`maintenance` **Celery** workers do dispatch/finalization; **Scrapyd** scrapes; the DB is the source of truth. The API never imports `apps/workers` (enqueues via `task_names` strings), and `app_shared.jobs.*`/`messaging` stay scrapy/twisted/playwright/fastapi-free (import-boundary test extended). Only `apps/api` is public; dispatch workers + Scrapyd stay internal. **PASS** |
| **II. Workspace Isolation (NON-NEGOTIABLE)** | Both new tables are workspace-owned; every endpoint/task scoped | `ScrapeJob`/`ScrapeJobTarget` carry `workspace_id`, are added to `WORKSPACE_OWNED_MODELS` (CI guard `scripts/check_workspace_scoping.py` covers them), and get `emit_rls_policy` (ENABLE+FORCE+fail-closed) in the creating migration. Every route runs on the `get_current_principal` seam (workspace context set on the session); every read/write goes through `scoped_select`/`scoped_get`; a target composite-FKs its job workspace-locally (`(workspace_id, scrape_job_id) → scrape_jobs(workspace_id, id)`). Dispatch/maintenance tasks call `set_workspace_context` before any job/target query. A match not in the workspace → run-match is a clean `404`, no job created. Cross-workspace read/write + no-context (0 rows) tests authored (live-DB). **PASS** |
| **III. Variant-Level Pricing & Explicit Matching** | Fan-out is variant→its active matches | Run-variant expands to exactly one target per **active** `competitor_product_match` of the variant (variant-level unit); no automatic matching. `unique(scrape_job_id, match_id)` guarantees no duplicate target per match. **PASS** |
| **IV. Database-Driven Configuration** | Batch sizes / stall timeout are config | HTTP batch bounds (`SCRAPE_DISPATCH_HTTP_BATCH_MIN/MAX`) and the stall timeout (`SCRAPE_STALL_TIMEOUT_SECONDS`) are `Settings` values, not literals. Which matches are active/what to scrape comes from the DB (`competitor_product_matches.status`), never hardcoded. **PASS** |
| **V. Disciplined Scraping Runtime (NON-NEGOTIABLE)** | The dispatch half of the runtime | Scraping runs under **Scrapyd**, never started in a Celery task — `dispatch_job` only calls `schedule.json` via the reused client. **Idempotent dispatch**: the reused Redis `SET NX` on `dispatched:{scrape_job_id}:{batch_index}` (+ persisted jobid) means an at-least-once retry never double-runs a batch; stall re-dispatch is stall-window-bucketed and target-state-guarded. **No hot-row contention**: counters aggregated from targets, never per-target increments. Deterministic node selection. This spec adds **no** blocking/reactor code (the pure jobs logic + Celery tasks never touch the Twisted reactor). **PASS** |
| **VI. Internal-Only & Legally Compliant Access (NON-NEGOTIABLE)** | Dispatch auth + no new scraping surface | Dispatch reuses the SPEC-07 client's mandatory Scrapyd **basic auth** (unauth `schedule.json` rejected → no run). No new access method, no external unlocker, no raw-HTML/screenshot storage — this spec persists only job/target rows + forwards `workspace_id`/`scrape_job_id`/`match_ids` to the existing spider. **PASS** |
| **VII. Monetary & Extraction Correctness** | No money/extraction in scope | This spec persists orchestration state only; no price parsing/confidence/currency logic (owned by SPEC-07's spider + SPEC-09's analysis). No `Decimal`/currency surface is introduced. **N/A — not weakened.** |
| **VIII. Scale-Safe Data & Concurrency** | Counters, batching, unique(job,match) | `success_count`/`failure_count`/`skipped_count` are **aggregated** (`GROUP BY status`) periodically + at finalization, never one write-per-target on the job row (SC-004). `unique(scrape_job_id, match_id)` prevents duplicate targets. Dispatch **batches** by domain/mode (50–200), not one job per URL (SC-008). All traffic through PgBouncer; `SET LOCAL`/xact-scoped only; set-based active-match resolution + target insert (no N+1). In-flight fencing-token locks are SPEC-11 — re-dispatch safety here rests on the idempotency guard + `unique(job,match)` + target-state checks (spec Assumptions), a documented boundary, not a violation. **PASS** |

**Technology & Security Constraints (§21/§33/§34)**: Stack lock-in honored (FastAPI, Celery+Redis, SQLAlchemy+Alembic, PostgreSQL, the SPEC-07 Scrapyd client — nothing substituted). UUIDv7 PKs (§21). Scrapyd basic auth reused (§33). Target `error_code` uses the §34 `ScrapeErrorCode` vocabulary. `/v1` versioned surface; `GET /v1/jobs/{id}/results` returns the full target set for a single job (bounded by the job's target count) — no cursor needed for the in-scope endpoints (the paginated `GET /v1/jobs` list is a later spec).

**Gate result**: PASS — no principle violated. One scoped boundary (in-flight match locks are SPEC-11) is documented in Complexity Tracking, consistent with the spec's own Assumptions. Re-checked post-Phase-1 (end of plan): still PASS — no new table beyond the two §22-enumerated tables, no reactor code, no hot-row counter path introduced.

## Project Structure

### Documentation (this feature)

```text
specs/008-jobs-orchestration/
├── plan.md              # This file (/speckit-plan output)
├── research.md          # Phase 0 — 8 decisions: batch-as-derived (no 3rd table), idempotency storage
│                        #   (Redis SET NX reuse), deterministic hash-by-domain node, stall detection +
│                        #   window-bucketed re-dispatch, counter aggregation, deterministic finalization,
│                        #   fork-safety (existing hook), API→worker enqueue seam
├── data-model.md        # Phase 1 — 2 tables (exact §22 shapes), enums, unique(scrape_job_id, match_id),
│                        #   workspace-local composite FK, RLS/isolation, state machines, transport shapes
├── quickstart.md        # Phase 1 — how to validate (unit here; live run-match/variant/RLS on a full stack)
├── contracts/           # Phase 1 — interfaces this feature exposes
│   ├── api-jobs.md                  # the four endpoints: request/response, scopes, status codes, isolation
│   ├── models-jobs.md               # ScrapeJob/ScrapeJobTarget shapes + unique + composite FK + RLS
│   ├── job-service.md               # create_match_job / create_variant_job (active-match, zero-target rule)
│   ├── dispatch-task.md             # scrape_dispatch: batching, deterministic node, idempotency, node_url arg
│   ├── batching.md                  # plan_batches grouping + 50–200 HTTP bounds (pure)
│   ├── node-selection.md            # select_node deterministic hash-by-domain (pure)
│   ├── lifecycle-counters.md        # aggregate_counts + resolve_finalized_status + mark_target + stall rule
│   ├── stall-recovery.md            # recover_stalled_batches: detect + window-bucketed re-dispatch guards
│   ├── messaging.md                 # app_shared.messaging enqueue-by-name producer seam
│   └── migration-jobs.md            # scrape_jobs + scrape_job_targets DDL + RLS on both, single head
└── tasks.md             # Phase 2 (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
libs/shared/app_shared/
├── enums.py              # EXTEND: ScrapeScope (WORKSPACE/COMPETITOR/PRODUCT/VARIANT/PRODUCT_GROUP/MATCH),
│                         #   ScrapeJobType (MANUAL/SCHEDULED/API_TRIGGERED/RETRY_FAILED/DISCOVERY),
│                         #   ScrapeJobStatus (PENDING/RUNNING/COMPLETED/PARTIAL_FAILED/FAILED/CANCELLED),
│                         #   ScrapeJobSource (API/SCHEDULER/INTERNAL/PLUGIN),
│                         #   ScrapeTargetStatus (PENDING/STARTED/COMPLETED/FAILED/SKIPPED). All StrEnum->VARCHAR.
├── config.py             # EXTEND: SCRAPE_DISPATCH_HTTP_BATCH_MIN=50, SCRAPE_DISPATCH_HTTP_BATCH_MAX=200,
│                         #   SCRAPE_STALL_TIMEOUT_SECONDS=900.
├── task_names.py         # EXTEND: SCRAPE_DISPATCH_JOB="scrape_dispatch.dispatch_job",
│                         #   SCRAPE_RECOVER_STALLED="maintenance.recover_stalled_batches",
│                         #   SCRAPE_FINALIZE_JOBS="maintenance.finalize_jobs".
├── messaging.py          # NEW: lazy Celery producer — enqueue(name, *, queue, kwargs) bound to REDIS_URL,
│                         #   so API/scheduler send scrape_dispatch work by name without importing apps/workers.
├── repository.py         # EXTEND: add ScrapeJob, ScrapeJobTarget to WORKSPACE_OWNED_MODELS.
├── models/
│   ├── __init__.py       # EXTEND: re-export ScrapeJob, ScrapeJobTarget (Base.metadata visibility for Alembic).
│   └── jobs.py           # NEW: ScrapeJob (unique(workspace_id, id); scope/refs soft; created_at only) +
│                         #   ScrapeJobTarget (unique(scrape_job_id, match_id); composite FK ->
│                         #   scrape_jobs(workspace_id, id); match_id soft; created_at only). WorkspaceScopedBase.
├── jobs/                 # NEW package — PURE orchestration logic (SQLAlchemy only; no scrapy/twisted/fastapi)
│   ├── __init__.py       # NEW
│   ├── batching.py       # NEW (pure): plan_batches(targets, *, http_max, http_min) -> list[Batch]; group by
│   │                     #   (competitor_domain, mode); HTTP batch 50–200; Batch(batch_index, mode, domain, match_ids).
│   ├── nodes.py          # NEW (pure): select_node(domain, nodes) -> node_url; deterministic stable hash-by-domain.
│   ├── lifecycle.py      # NEW (pure): resolve_finalized_status(success, failure, skipped, total) ->
│   │                     #   ScrapeJobStatus (COMPLETED/PARTIAL_FAILED/FAILED); stall_window(now, timeout) bucket.
│   ├── targets.py        # NEW: mark_target(session, ...) state-transition writer (FR-017);
│   │                     #   aggregate_counts(session, job_id, workspace_id) -> Counts via GROUP BY status.
│   └── service.py        # NEW: create_match_job / create_variant_job (resolve active matches scoped, create job +
│                         #   targets set-based, enqueue dispatch, zero-target -> immediate COMPLETED no dispatch).
└── scrapyd/
    └── client.py         # EXTEND: schedule(..., node_url: str | None = None) — target the selected node;
                          #   defaults to SCRAPYD_HTTP_URLS[0] (back-compat with the SPEC-07 thin task).

apps/api/app/
├── main.py               # EXTEND: include jobs.router.
├── schemas/
│   └── jobs.py           # NEW: JobRunResponse (id, status), JobResponse (status/type/scope/counts/timestamps),
│                         #   JobTargetResponse + JobResultsResponse (per-target match/status/error_code).
└── routers/
    └── jobs.py           # NEW: POST /v1/jobs/run/match/{match_id}, POST /v1/jobs/run/variant/{variant_id},
                          #   GET /v1/jobs/{job_id}, GET /v1/jobs/{job_id}/results. Auth seam + jobs:read/jobs:write
                          #   scopes; scoped_select/scoped_get; delegates creation to app_shared.jobs.service.

apps/workers/app/workers/
├── celery_app.py         # EXTEND: register scrape_dispatch + maintenance queues/routes. (Fork-safety hook
│                         #   worker_process_init -> dispose_engine ALREADY PRESENT from SPEC-01 — satisfies FR-016.)
└── tasks_jobs.py         # NEW: dispatch_job (scrape_dispatch) — RUNNING+started_at, plan_batches, per-batch
                          #   client.schedule(node_url=select_node(domain), batch_index=...) idempotent;
                          #   recover_stalled_batches + finalize_jobs/refresh_job_counters (maintenance).

alembic/versions/
└── <rev>_scrape_jobs_targets_tables.py  # NEW: create scrape_jobs (unique(workspace_id, id)) +
                          #   scrape_job_targets (unique(scrape_job_id, match_id), composite FK -> scrape_jobs);
                          #   emit_rls_policy on BOTH; down_revision = 2db33dea5e14 (current head); single head.

tests/unit/
├── test_import_boundaries.py         # EXTEND: assert app_shared.jobs.* + app_shared.messaging import NO
│                                     #   scrapy/twisted/playwright/fastapi.
├── test_jobs_models.py               # NEW: table/column shapes; unique(scrape_job_id, match_id);
│                                     #   unique(workspace_id, id) on jobs; composite FK; enums; created_at-only.
├── test_jobs_rls.py                  # NEW: emit_rls_policy render for both tables (fail-closed DDL).
├── test_migration_offline_jobs.py    # NEW: `alembic upgrade head --sql` renders both tables + unique + RLS;
│                                     #   single head; down_revision == 2db33dea5e14.
├── test_jobs_batching.py             # NEW: group by domain+mode; HTTP batch size in [50,200]; stable batch_index;
│                                     #   no match appears in two batches; empty -> no batches.
├── test_jobs_node_selection.py       # NEW: select_node deterministic (same domain -> same node across calls);
│                                     #   distribution across nodes; single-node pool.
├── test_jobs_lifecycle.py            # NEW: resolve_finalized_status boundary values (all-success COMPLETED;
│                                     #   mixed PARTIAL_FAILED; none-success FAILED; skipped handling); zero-target.
├── test_jobs_counters.py             # NEW: aggregate_counts GROUP BY over a fake session -> correct counts;
│                                     #   one UPDATE, never per-target increments.
├── test_jobs_service.py              # NEW: create_match_job (1 target, MANUAL/API/requested_by, enqueue called);
│                                     #   create_variant_job (one target per ACTIVE match, inactive excluded);
│                                     #   zero-active-match -> job COMPLETED, total_targets=0, NO enqueue.
├── test_jobs_dispatch_task.py        # NEW: dispatch_job sets RUNNING+started_at, one schedule() per batch with
│                                     #   the selected node + batch_index; duplicate delivery -> no second POST
│                                     #   (fake client/redis); set_workspace_context called.
├── test_jobs_stall_recovery.py       # NEW: recover_stalled_batches re-dispatches only unprogressed targets past
│                                     #   timeout; progressed/locked targets excluded; window-bucketed key idempotent.
├── test_jobs_scoping_guard.py        # NEW: CI guard flags a planted unscoped select on the two new models.
├── test_jobs_messaging.py            # NEW: enqueue(name, queue, kwargs) routes to the right queue via a fake
│                                     #   Celery producer; API never imports apps/workers.
└── test_jobs_router.py               # NEW: run-match/run-variant/get/results with a dependency-overridden session
                                      #   + fake enqueue -> 202/200 shapes; unknown/cross-ws match -> 404, no job.

tests/integration/  (authored, live-stack-marked — skipped without Postgres/Redis/Scrapyd)
├── test_jobs_run_match_live.py       # seed ws/product/variant/competitor/match; POST run-match -> job + 1 target
│                                     #   (scoped), dispatch enqueued, schedule.json carries workspace_id/job_id/match_ids.
├── test_jobs_run_variant_live.py     # variant w/ active + inactive matches -> one target per ACTIVE match,
│                                     #   unique(job,match); zero-active -> COMPLETED total_targets=0 no dispatch.
├── test_jobs_counters_finalize_live.py # simulate target transitions -> counters aggregate; status finalizes
│                                     #   COMPLETED/PARTIAL_FAILED/FAILED; completed_at set.
├── test_jobs_isolation_live.py       # cross-workspace job/target read+write blocked (app + RLS); no-context 0 rows.
└── test_jobs_dispatch_scrapyd_live.py# authenticated schedule.json per batch; retried dispatch does not double-run.
```

**Structure Decision**: Backend monorepo (uv workspace), matching SPEC-02→07. The two ORM models + all **pure** orchestration logic (batching, node selection, lifecycle/finalization, counter aggregation, job-creation service) + the enqueue-by-name messaging seam live in `libs/shared/app_shared` (which already owns the models, enums, config, Redis client, task-name constants, and — from SPEC-07 — the authenticated Scrapyd dispatch client), so every DB/Redis/Scrapyd-independent piece is unit-testable with SQLAlchemy/fakes only and stays scrapy/twisted/fastapi-free. The API is a thin router + schema layer over `app_shared.jobs.service`; the worker is a thin `scrape_dispatch`/`maintenance` task layer over the same pure logic + the reused Scrapyd client. The two tables + RLS land in one repo-root Alembic migration chained onto head `2db33dea5e14`.

## Complexity Tracking

> No Constitution Check violation. Two scoped boundaries (both matching the spec's own Assumptions), documented rather than silently taken.

| Item | Why / Decision | Simpler / stricter alternative rejected because |
|------|----------------|-------------------------------------------------|
| **A dispatch "batch" is a derived grouping, not a third table** | §22 enumerates exactly two Jobs tables (`scrape_jobs`, `scrape_job_targets`); the spec's Key Entities + autospec decision #7 leave batch representation to planning with only the deterministic-node + idempotency guarantees binding. A batch is a stable derived `(domain, mode)` group with a deterministic `batch_index`; the idempotency guard is the reused Redis `SET NX` key (`dispatched:{scrape_job_id}:{batch_index}`), node choice is a stateless hash-by-domain, and stall detection reads `scrape_job_targets` state — so **no** per-batch columns or table are needed. | A `scrape_job_batches` table (persisted node + jobid + dispatched_at) was rejected: it adds a table beyond §22's exact enumeration (the same discipline SPEC-07 applied by *not* creating these very tables early), and hash-by-domain + `SET NX` + target-state give deterministic node + idempotency + stall detection without it. |
| **In-flight fencing-token match locks are SPEC-11 (not built here)** | The spec Assumptions defer distributed rate limiting + fencing-token locks to SPEC-11; this spec provides re-dispatch safety via the idempotency guard + `unique(scrape_job_id, match_id)` + target-state checks (a stalled batch re-dispatches only targets still in a non-progressed state), and integrates with locks where they exist. | Building the full lock machinery here was rejected as out-of-scope (SPEC-11 owns it); silently ignoring re-dispatch safety was rejected — the target-state exclusion + window-bucketed idempotent key are the documented interim guarantee (FR-015). |
