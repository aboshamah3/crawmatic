---
description: "Dependency-ordered task list for SPEC-08 Jobs & Orchestration"
---

# Tasks: Jobs & Orchestration

**Input**: Design documents from `/specs/008-jobs-orchestration/`

**Prerequisites**: plan.md (required), spec.md (required), research.md (D1–D8), data-model.md, contracts/ (10 files), quickstart.md

**Tests**: Included — the spec, plan (Project Structure `tests/unit` + `tests/integration`), and quickstart.md explicitly enumerate the unit and live test suites, matching the SPEC-02→07 test pattern. Every DB/Redis/Scrapyd-**independent** behavior is unit-tested **here** (batching + 50–200 bounds, deterministic node selection, the finalized-status rule incl. boundary values, counter aggregation with a fake session, the job-creation service + dispatch orchestration against fakes, the messaging seam against a fake producer, model/unique/RLS DDL render via offline `alembic upgrade head --sql`, import boundaries, workspace-scoping guard, endpoint request/response with a dependency-overridden session). Live-stack tests (real run-match/run-variant end-to-end, actual `unique(scrape_job_id, match_id)` + composite-FK enforcement, RLS row denial, real `schedule.json` per-batch dispatch, retried-dispatch no-double-run) are **authored and skip cleanly** where no Postgres/Redis/Scrapyd is reachable — no container engine in this build env (SPEC-02→07 deferred-verification pattern).

**Organization**: Tasks are grouped by user story (US1..US3) to enable independent implementation and testing. Shared blocking work is in Setup + Foundational.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: `[US1]`/`[US2]`/`[US3]` maps a task to a spec.md user story (Setup / Foundational / Integration / Polish carry no story label)
- Every task lists an exact repo-relative file path

## Path Conventions

Backend monorepo (uv workspace). Models/enums/config/scopes/task-names/pure orchestration logic/messaging seam/Scrapyd client in `libs/shared/app_shared/`; the router + schemas in `apps/api/app/`; the dispatch + maintenance tasks + queue wiring in `apps/workers/app/workers/`; the migration at repo-root `alembic/versions/`; tests in `tests/unit/` and `tests/integration/`.

---

## Scope Boundary (read first)

**IN SCOPE — exactly two new tables + the orchestration layer over them:**

- Tables: `scrape_jobs` + `scrape_job_targets` (exact §22 shapes, both `WorkspaceScopedBase`, in `WORKSPACE_OWNED_MODELS`, `emit_rls_policy` ENABLE+FORCE fail-closed in the creating migration; `unique(scrape_job_id, match_id)`, `unique(workspace_id, id)` on jobs, workspace-local composite FK target→job; `created_at` only, no `updated_at`).
- Enums (extend `app_shared.enums`): `ScrapeScope`, `ScrapeJobType`, `ScrapeJobStatus`, `ScrapeJobSource`, `ScrapeTargetStatus` (target `error_code` reuses the existing `ScrapeErrorCode`).
- Pure orchestration libs (`app_shared.jobs.*`, SQLAlchemy-only, no scrapy/twisted/fastapi): `batching.plan_batches`, `nodes.select_node`, `lifecycle.resolve_finalized_status` + `stall_window`, `targets.mark_target` + `aggregate_counts`, `service.create_match_job`/`create_variant_job`.
- Enqueue-by-name producer seam (`app_shared.messaging.enqueue`); `task_names`/`config` additions; the `jobs:write` scope; `ScrapydDispatchClient.schedule(..., node_url=...)` extension.
- Endpoints under `/v1`: `POST /jobs/run/match/{id}`, `POST /jobs/run/variant/{id}`, `GET /jobs/{id}`, `GET /jobs/{id}/results` (scope-gated, workspace-scoped).
- Workers: `dispatch_job` (`scrape_dispatch` queue) + `recover_stalled_batches`/`finalize_jobs`/`refresh_job_counters` (`maintenance` queue); celery queue/route registration; reliance on the existing `worker_process_init` fork-safety hook (asserted, not re-implemented).

**OUT OF SCOPE (do NOT build — later specs):** product/group/competitor/workspace run endpoints + the scheduler / SCHEDULED trigger + celery beat wiring (SPEC-13; the job model carries the scope fields but only match/variant endpoints ship). Price analysis / current-price update / alerting / `price_analysis` emission (SPEC-09 — this spec ends at persisting job/target rows). In-flight fencing-token match locks + distributed rate limiting (SPEC-11 — re-dispatch safety here rests on the idempotency guard + `unique(job,match)` + target-state checks; stall recovery reads `locked_at` where present but does not implement locks). No third `scrape_job_batches` table (a batch is a derived `(domain, mode)` grouping — research D1). Reuse unchanged: the SPEC-07 `ScrapydDispatchClient` (auth + Redis `SET NX` idempotency), the `generic_price_spider`, the `worker_process_init` hook, `scoped_select`/`scoped_get`, `enum_column`, `emit_rls_policy`, `WorkspaceScopedBase`, the AST scoping guard, `deps.require_scopes`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Enums, config, the new scope + task-name constants, the empty `jobs` package, and the enqueue-by-name producer seam. All DB-independent; every later file imports these.

- [X] T001 [P] Extend `libs/shared/app_shared/enums.py` with five `StrEnum` → `VARCHAR` enums (data-model.md, per `enum_column`): `ScrapeScope` (`WORKSPACE`/`COMPETITOR`/`PRODUCT`/`VARIANT`/`PRODUCT_GROUP`/`MATCH`), `ScrapeJobType` (`MANUAL`/`SCHEDULED`/`API_TRIGGERED`/`RETRY_FAILED`/`DISCOVERY`), `ScrapeJobStatus` (`PENDING`/`RUNNING`/`COMPLETED`/`PARTIAL_FAILED`/`FAILED`/`CANCELLED`), `ScrapeJobSource` (`API`/`SCHEDULER`/`INTERNAL`/`PLUGIN`), `ScrapeTargetStatus` (`PENDING`/`STARTED`/`COMPLETED`/`FAILED`/`SKIPPED`). Reuse the existing `ScrapeErrorCode` (§34) for target `error_code` — do NOT add a new error enum. (FR-003)
- [X] T002 [P] Extend `libs/shared/app_shared/config.py` (`Settings`) with `SCRAPE_DISPATCH_HTTP_BATCH_MIN: int = 50`, `SCRAPE_DISPATCH_HTTP_BATCH_MAX: int = 200`, `SCRAPE_STALL_TIMEOUT_SECONDS: int = 900` (env/DB-tunable, not hardcoded literals — Principle IV). (FR-011, FR-015)
- [X] T003 [P] Extend `libs/shared/app_shared/security/scopes.py` with `JOBS_WRITE = "jobs:write"` in the `Scope` vocabulary (`JOBS_READ` already exists; the run endpoints require write, following the `matches:*` precedent). (FR-006, FR-007, FR-010)
- [X] T004 [P] Extend `libs/shared/app_shared/task_names.py` with `SCRAPE_DISPATCH_JOB = "scrape_dispatch.dispatch_job"`, `SCRAPE_RECOVER_STALLED = "maintenance.recover_stalled_batches"`, `SCRAPE_FINALIZE_JOBS = "maintenance.finalize_jobs"` (plain strings; the module stays celery-free). (FR-011, FR-015, D8)
- [X] T005 [P] Create `libs/shared/app_shared/jobs/__init__.py` — empty package init for the framework-agnostic batching/nodes/lifecycle/targets/service modules.
- [X] T006 [P] Create `libs/shared/app_shared/messaging.py` — `enqueue(name, *, queue, kwargs=None) -> None`: lazily construct a module-level `celery.Celery(broker=Settings.REDIS_URL)` producer (no result backend) and `send_task(name, kwargs=kwargs, queue=queue)`. May import `celery` (the ban is scrapy/twisted/playwright/fastapi). Per contracts/messaging.md (D8). (FR-006, FR-007)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The two ORM models + registration + migration, the Scrapyd client `node_url` extension, and the celery queue/route wiring (relying on the existing fork-safety hook). Plus the DB-independent shape/RLS/offline-migration/scoping/messaging/scope/fork-safety tests. **No user story can be implemented until this phase is complete.**

**⚠️ CRITICAL**: Blocks all of Phase 3–5.

- [X] T007 Create `libs/shared/app_shared/models/jobs.py` — two `WorkspaceScopedBase` ORM models per data-model.md / contracts/models-jobs.md: `ScrapeJob` (`scrape_jobs`: id, workspace_id, `type`/`scope`/`status`/`source` via `enum_column`, `priority` via `enum_column`→`MatchPriority` default `NORMAL`, nullable soft UUID refs `product_id`/`product_variant_id`/`product_group_id`/`competitor_id`/`match_id` (no FK), `total_targets`/`success_count`/`failure_count`/`skipped_count` Integer default 0, nullable `requested_by` UUID, nullable `started_at`/`completed_at` `TZDateTime`, explicit `created_at` — NO `updated_at`; `__table_args__` = `UniqueConstraint("workspace_id","id")` + `ForeignKeyConstraint(["workspace_id"],["workspaces.id"])`) and `ScrapeJobTarget` (`scrape_job_targets`: id, workspace_id, scrape_job_id, `match_id` (indexed soft UUID, no FK), `status` via `enum_column`, nullable `locked_at`/`started_at`/`completed_at` `TZDateTime`, nullable `error_code` via `enum_column`→`ScrapeErrorCode`, explicit `created_at`; `__table_args__` = `UniqueConstraint("scrape_job_id","match_id", name="uq_scrape_job_targets_scrape_job_id_match_id")` + composite `ForeignKeyConstraint(["workspace_id","scrape_job_id"],["scrape_jobs.workspace_id","scrape_jobs.id"], name="fk_scrape_job_targets_workspace_scrape_job_scrape_jobs")` + RLS-anchor `ForeignKeyConstraint(["workspace_id"],["workspaces.id"])`). All emitted constraint/index names ≤63 bytes (depends on T001). (FR-001, FR-002)
- [X] T008 Extend `libs/shared/app_shared/models/__init__.py` to re-export `ScrapeJob`, `ScrapeJobTarget` (Base.metadata visibility for Alembic offline render) (depends on T007). (FR-005)
- [X] T009 Extend `libs/shared/app_shared/repository.py` to add `ScrapeJob`, `ScrapeJobTarget` to `WORKSPACE_OWNED_MODELS` (so `scoped_select`/`scoped_get` + the AST scoping guard cover them) (depends on T007). (FR-004)
- [X] T010 Create the Alembic migration `alembic/versions/<rev>_scrape_jobs_targets_tables.py` per contracts/migration-jobs.md — `op.create_table("scrape_jobs", ...)` (all §22 columns, enum-likes as `sa.String(length=32)`, `PrimaryKeyConstraint("id")`, `UniqueConstraint("workspace_id","id")`, RLS-anchor FK, index on `workspace_id`, `created_at` only), `op.create_table("scrape_job_targets", ...)` (`unique(scrape_job_id, match_id)`, composite FK → `scrape_jobs(workspace_id, id)`, RLS-anchor FK, indexes on `workspace_id` + `match_id`, `match_id` has no FK, `created_at` only), then `for stmt in emit_rls_policy("scrape_jobs"): op.execute(stmt)` and likewise for `scrape_job_targets`; `downgrade()` drops `scrape_job_targets` then `scrape_jobs`; `down_revision = "2db33dea5e14"` (current head, SPEC-07 observations); single head preserved (depends on T007). (FR-001, FR-002, FR-004, FR-005)
- [X] T011 [P] Extend `libs/shared/app_shared/scrapyd/client.py` — add optional `node_url: str | None = None` to `ScrapydDispatchClient.schedule(...)`; POST to `node_url` when given, else `SCRAPYD_HTTP_URLS[0]` (back-compat with the SPEC-07 thin `dispatch_generic_price_spider` task). Auth + the claim/commit/release Redis `SET NX` idempotency ordering are unchanged. Per contracts/dispatch-task.md "Client extension". (FR-012, FR-014)
- [X] T012 Extend `apps/workers/app/workers/celery_app.py` — register the `scrape_dispatch` and `maintenance` queues + task routes (route `SCRAPE_DISPATCH_JOB` → `scrape_dispatch`, `SCRAPE_RECOVER_STALLED`/`SCRAPE_FINALIZE_JOBS` → `maintenance`) and add `tasks_jobs` to the worker's task imports/`include`. **Do NOT** add or duplicate a fork-safety hook — the existing `@worker_process_init.connect → dispose_engine` (SPEC-01) already satisfies FR-016; this task only wires queues/routes for the first DB-touching tasks that rely on it. (FR-011, FR-015, FR-016, D7)

### Foundational tests (DB/Redis-independent)

- [X] T013 [P] Unit test `tests/unit/test_jobs_models.py` — table/column names + nullability for both tables; both models `enum_column`-render `VARCHAR` (not DB enum); `created_at` present, `updated_at` absent; `unique(scrape_job_id, match_id)`; `unique(workspace_id, id)` on jobs; the composite target→job FK; RLS-anchor FK on both; `match_id` has no FK; both models in `WORKSPACE_OWNED_MODELS` and re-exported from `app_shared.models`; every constraint/index name ≤63 bytes (depends on T007, T008, T009). (FR-001, FR-002, FR-003)
- [X] T014 [P] Unit test `tests/unit/test_jobs_rls.py` — `emit_rls_policy` render (ENABLE + FORCE + fail-closed policy) for both `scrape_jobs` and `scrape_job_targets` (depends on T007). (FR-004, SC-006)
- [X] T015 [P] Unit test `tests/unit/test_migration_offline_jobs.py` — `alembic upgrade head --sql` (offline, no DB) renders both `CREATE TABLE`s, the two uniques, the composite FK, and the RLS statements for both tables; `alembic heads` yields a single head; `down_revision == 2db33dea5e14` (depends on T010). (FR-005)
- [X] T016 [P] Unit test `tests/unit/test_jobs_scoping_guard.py` — the workspace-scoping AST CI guard flags a planted unscoped `select` on `ScrapeJob` / `ScrapeJobTarget` (both in the guarded `WORKSPACE_OWNED_MODELS` set) (depends on T009). (FR-004, SC-006)
- [X] T017 [P] Unit test `tests/unit/test_jobs_messaging.py` — `enqueue(name, queue, kwargs)` routes to the right queue with the right task name + kwargs via a fake/patched Celery producer; the producer is lazily constructed (depends on T006). (FR-006, FR-007)
- [X] T018 [P] Extend `tests/unit/test_scopes.py` — assert `jobs:write` is in the `Scope` vocabulary (`jobs:read` already covered) (depends on T003). (FR-006, FR-007)
- [X] T019 [P] Unit test `tests/unit/test_jobs_fork_safety.py` — assert `apps/workers/app/workers/celery_app.py` connects `worker_process_init` → `dispose_engine` (the inherited engine is disposed before first DB-touching task use); the hook exists and is relied upon, not re-implemented (depends on T012). (FR-016, D7)

**Checkpoint**: Models + migration + RLS + client `node_url` + celery queues wired; DB-independent shape/RLS/offline-migration/scoping/messaging/scope/fork-safety tests green. User stories can begin.

---

## Phase 3: User Story 1 - Run a single match and observe its job (Priority: P1) 🎯 MVP

**Goal**: `POST /v1/jobs/run/match/{id}` creates a durable `ScrapeJob` (scope MATCH, `type=MANUAL`, `source=API`, `requested_by`=principal) + exactly one `ScrapeJobTarget`, enqueues `scrape_dispatch.dispatch_job`, and returns the job id immediately (202). The dispatch task groups the target into a batch and issues one authenticated Scrapyd `schedule.json` carrying `workspace_id`/`scrape_job_id`/`match_ids` to a deterministically selected node; a duplicate delivery does not double-run. `GET /jobs/{id}` and `GET /jobs/{id}/results` return status/counts/timestamps and per-target outcomes, workspace-scoped.

**Independent Test**: Call run-match for a valid in-workspace match → assert a job + exactly one target (scoped, `MANUAL`/`API`/`requested_by`), a dispatch task enqueued once, status PENDING, job id returned; unknown/cross-workspace match → 404, no job, no enqueue; the dispatch task sets RUNNING+started_at once and issues one `schedule` per batch with the selected node + `batch_index`, a duplicate delivery issues no second POST; get/results return the documented shapes.

### Implementation for User Story 1

- [ ] T020 [P] [US1] Create `libs/shared/app_shared/jobs/batching.py` (pure — no DB/Redis/network) — `Batch = (batch_index: int, mode: ScrapeProfileMode, domain: str, match_ids: list[UUID])`; `plan_batches(targets, *, http_min=50, http_max=200) -> list[Batch]` grouping targets (with their attached `competitor_domain` + `mode`) by `(domain, mode)`, chunking each group to ≤ `http_max` (50–200 guidance), `batch_index` the stable enumerated position over a canonical sort of the groups; every target in exactly one batch, empty input → empty list. Per contracts/batching.md (D1). (FR-011, SC-008)
- [ ] T021 [P] [US1] Create `libs/shared/app_shared/jobs/nodes.py` (pure) — `select_node(domain, nodes) -> node_url` returning `nodes[stable_hash(domain) % len(nodes)]` where `stable_hash` is a process-stable digest (`int.from_bytes(hashlib.blake2b(domain.encode(), digest_size=8).digest())`), **not** builtin `hash()`; single-node pool → that node. Per contracts/node-selection.md (D3). (FR-014, SC-005)
- [ ] T022 [US1] Create `libs/shared/app_shared/jobs/service.py` — `create_match_job(session, *, workspace_id, match, requested_by) -> (job_id, status)`: create `ScrapeJob(scope=MATCH, type=MANUAL, source=API, requested_by, match_id/product_variant_id/product_id/competitor_id from the match, status=PENDING, total_targets=1)` + one `ScrapeJobTarget(status=PENDING, match_id=match.id)`; `enqueue(SCRAPE_DISPATCH_JOB, queue="scrape_dispatch", kwargs={"scrape_job_id": str(job.id), "workspace_id": str(workspace_id)})`; return `(job.id, PENDING)`. Counters start at 0, never incremented here. SQLAlchemy + `messaging.enqueue` only, no scrapy/twisted/fastapi. (`create_variant_job` added in US2.) Per contracts/job-service.md (depends on T007, T006). (FR-006, FR-010)
- [ ] T023 [US1] Create `apps/api/app/schemas/jobs.py` (Pydantic v2) — `JobRunResponse { id, status }`; `JobResponse { id, type, scope, status, priority, total_targets, success_count, failure_count, skipped_count, requested_by, source, started_at, completed_at, created_at }`; `JobTargetResponse { id, match_id, status, error_code, started_at, completed_at, locked_at }`; `JobResultsResponse { items: list[JobTargetResponse] }`. Per contracts/api-jobs.md (depends on T001). (FR-008, FR-009)
- [ ] T024 [US1] Create `apps/api/app/routers/jobs.py` (per contracts/api-jobs.md) — on the `get_current_principal` auth seam (RLS context already set on the yielded session): `POST /v1/jobs/run/match/{match_id}` (require `jobs:write`; `scoped_get(session, CompetitorProductMatch, match_id, ws)` → 404 `NOT_FOUND` + no job on miss/cross-ws; delegate to `service.create_match_job`; **202** `JobRunResponse`), `GET /v1/jobs/{job_id}` (require `jobs:read`; `scoped_get(ScrapeJob)` → 404 on miss; **200** `JobResponse`), `GET /v1/jobs/{job_id}/results` (require `jobs:read`; verify job visible via `scoped_get` → 404; `scoped_select(ScrapeJobTarget, ws).where(scrape_job_id == job_id)`; **200** `JobResultsResponse`). (run-variant added in US2.) All reads/writes through `scoped_*` + RLS; never imports `apps/workers` (depends on T022, T023). (FR-006, FR-008, FR-009)
- [ ] T025 [US1] Register the jobs router in `apps/api/app/main.py` under `/v1` (depends on T024). (FR-006, FR-008, FR-009)
- [ ] T026 [US1] Create `apps/workers/app/workers/tasks_jobs.py` — `dispatch_job(scrape_job_id, workspace_id)` (name `SCRAPE_DISPATCH_JOB`, queue `scrape_dispatch`): open a session, `set_workspace_context(session, workspace_id)`, `scoped_get` the job + `scoped_select` its PENDING targets; if not terminal set `status=RUNNING`, `started_at=now` once; resolve each target's `competitor_domain` + `mode` **set-based** (one scoped read over the matches/competitors, not per-target); `batches = plan_batches(targets, http_min=settings.SCRAPE_DISPATCH_HTTP_BATCH_MIN, http_max=settings.SCRAPE_DISPATCH_HTTP_BATCH_MAX)`; for each batch pick the mode-appropriate node pool `nodes = settings.SCRAPYD_BROWSER_URLS if batch.mode == ScrapeProfileMode.BROWSER else settings.SCRAPYD_HTTP_URLS` (I1 — a BROWSER batch must never be routed to HTTP nodes; the browser spider/service is SPEC-14 but routing is mode-correct here), then `client.schedule(project="price_monitor", spider="generic_price_spider", workspace_id, scrape_job_id, match_ids=batch.match_ids, mode=batch.mode, batch_index=batch.batch_index, node_url=select_node(batch.domain, nodes))`. The client's Redis `SET NX` on `dispatched:{scrape_job_id}:{batch_index}` neutralizes a duplicate delivery (no second POST). Never starts Scrapy in-process. (`finalize_jobs`/`refresh_job_counters`/`recover_stalled_batches` added in US3.) Per contracts/dispatch-task.md (depends on T020, T021, T011, T012). (FR-011, FR-012, FR-013, SC-001, SC-003, SC-008)

### Tests for User Story 1

- [ ] T027 [P] [US1] Unit test `tests/unit/test_jobs_batching.py` — a single-target/single-domain input → one batch (batch_index 0); a >`http_max` group splits into ≤`http_max` chunks; batch sizes within `[1, http_max]`; stable `batch_index` across repeated calls on the same input; no match in two batches; empty input → no batches. (Multi-domain/mode grouping extended in US2.) (depends on T020). (FR-011, SC-008)
- [ ] T028 [P] [US1] Unit test `tests/unit/test_jobs_node_selection.py` — `select_node` deterministic for a given domain across repeated calls and a fresh reimport (not builtin `hash()`); different domains distribute across a multi-node pool; single-node pool → that node (depends on T021). (FR-014, SC-005)
- [ ] T029 [P] [US1] Unit test `tests/unit/test_jobs_service.py` — `create_match_job` (fake session + fake `enqueue`): 1 job + 1 target, provenance `type=MANUAL`/`source=API`/`requested_by`, `scope=MATCH`, `match_id`/`product_variant_id`/`product_id`/`competitor_id` from the match, `total_targets=1`, `status=PENDING`, counters 0, `enqueue` called once with `SCRAPE_DISPATCH_JOB`/`scrape_dispatch`/correct kwargs. (`create_variant_job` cases added in US2.) (depends on T022). (FR-006, FR-010)
- [ ] T030 [P] [US1] Unit test `tests/unit/test_jobs_dispatch_task.py` — `dispatch_job` (fake client + fake redis + fake session): `set_workspace_context` invoked before any query; sets RUNNING+started_at once; one `schedule` per planned batch carrying the selected node + `batch_index` + `workspace_id`/`scrape_job_id`/`match_ids`; a duplicate delivery of the same `(scrape_job_id, batch_index)` issues no second POST (SET NX no-op) (depends on T026). (FR-011, FR-012, FR-013, SC-003)
- [ ] T031 [P] [US1] Unit test `tests/unit/test_jobs_router.py` (dependency-overridden session + fake `enqueue`) — run-match → 202 + `JobRunResponse`, 1 job + 1 target scoped, `MANUAL`/`API`/`requested_by`, enqueue called once; unknown/cross-ws match → 404, no job, no enqueue; `GET /jobs/{id}` → 200 `JobResponse` shape, missing job → 404; `GET /jobs/{id}/results` → 200 `JobResultsResponse` shape, missing job → 404; every route declares the correct `require_scopes` (write for run-match, read for get/results). (run-variant cases added in US2.) (depends on T024, T025). (FR-006, FR-008, FR-009, SC-006)

**Checkpoint**: The trigger→job→dispatch→observe loop works end-to-end at fixture scale (pure batching/node logic + service + dispatch orchestration + router shapes, all against fakes). US1 is independently testable. MVP demoable.

---

## Phase 4: User Story 2 - Run all active matches for a variant (Priority: P2)

**Goal**: `POST /v1/jobs/run/variant/{id}` finds every ACTIVE match of the variant, creates one `ScrapeJob` (scope VARIANT) with exactly one unique target per active match (`total_targets = N`), and dispatches domain/mode-grouped batches — not one Scrapyd job per URL. Inactive matches are excluded. A variant with zero active matches creates the job, sets `total_targets = 0`, finalizes COMPLETED immediately, and dispatches nothing.

**Independent Test**: Seed a variant with several active matches + at least one inactive; call run-variant → one job with exactly one target per *active* match (unique per `(job, match)`), `total_targets = N`; inactive excluded; a variant whose active matches span multiple domains/modes batches by domain+mode within the 50–200 bounds; zero-active → 202 COMPLETED, `total_targets=0`, no enqueue.

### Implementation for User Story 2

- [ ] T032 [US2] Extend `libs/shared/app_shared/jobs/service.py` with `create_variant_job(session, *, workspace_id, variant, requested_by) -> (job_id, status)`: resolve all ACTIVE matches of the variant via one `scoped_select(CompetitorProductMatch, ws).where(product_variant_id == variant.id, status == MatchStatus.ACTIVE)` (inactive excluded); create `ScrapeJob(scope=VARIANT, type=MANUAL, source=API, requested_by, product_variant_id, product_id=variant.product_id, status=PENDING, total_targets=N)`; **N==0** → set `status=COMPLETED`, `total_targets=0`, `completed_at=now`, **do not enqueue**, return `(job.id, COMPLETED)`; **N>0** → set-based insert of one `ScrapeJobTarget` per active match (`unique(scrape_job_id, match_id)` guards duplicates), enqueue dispatch, return `(job.id, PENDING)`. Per contracts/job-service.md (depends on T022). (FR-007, FR-020, SC-002)
- [ ] T033 [US2] Extend `apps/api/app/routers/jobs.py` with `POST /v1/jobs/run/variant/{variant_id}` (require `jobs:write`; `scoped_get(session, ProductVariant, variant_id, ws)` → 404 `NOT_FOUND` + no job on miss/cross-ws; delegate to `service.create_variant_job`; **202** `JobRunResponse` — `status=PENDING`, or `status=COMPLETED` for the zero-active-match case) (depends on T024, T032). (FR-007, FR-020)

### Tests for User Story 2

- [ ] T034 [US2] Extend `tests/unit/test_jobs_service.py` — `create_variant_job`: one target per ACTIVE match, inactive excluded, `total_targets == N`, `scope=VARIANT`, enqueue called once; **zero active matches** → job `status=COMPLETED`, `total_targets=0`, `completed_at` set, **enqueue NOT called** (depends on T029, T032). (FR-007, FR-020, SC-002)
- [ ] T035 [US2] Extend `tests/unit/test_jobs_router.py` — run-variant → 202 one target per active match, inactive excluded, unique `(job, match)`; unknown/cross-ws variant → 404, no job; zero-active → 202 `status=COMPLETED`, no enqueue (depends on T031, T033). (FR-007, FR-020, SC-002)
- [ ] T036 [P] [US2] Extend `tests/unit/test_jobs_batching.py` — multi-domain / multi-mode targets → grouped by `(domain, mode)`, one Scrapyd batch per group (not per match, SC-008), each group's chunks honoring the 50–200 guidance; batch count tracks domain/mode grouping (US2-AS3) (depends on T027). (FR-011, SC-008)

**Checkpoint**: Variant fan-out creates one unique target per active match, batches by domain/mode, and resolves the empty case to COMPLETED without dispatch. US2 is independently testable.

---

## Phase 5: User Story 3 - Accurate job lifecycle and counters under scale (Priority: P3)

**Goal**: As targets complete, job counters (success/failure/skipped) stay correct by **aggregation** from `scrape_job_targets` (never per-target increments on the hot job row), status finalizes **deterministically** (COMPLETED / PARTIAL_FAILED / FAILED) with `completed_at` set, and a batch queued on a node that died is detected past the stall timeout and re-dispatched to a deterministically selected node — under the same idempotency guard and without double-running progressed or in-flight-locked targets.

**Independent Test**: Simulate targets transitioning to completed/failed/skipped → counters derived by `GROUP BY status` (one UPDATE, never per-target), status resolves via the failure-centric rule (no-failures COMPLETED incl. skipped-only, failure+success PARTIAL_FAILED, failure+no-success FAILED, zero-target COMPLETED); `mark_target` touches only the target row; simulate a dispatched batch whose targets never leave PENDING past the timeout → re-dispatched to the same (hash-by-domain) node, progressed/`locked_at`-live targets excluded, the window-bucketed key idempotent within a window and fresh across windows.

### Implementation for User Story 3

- [ ] T037 [P] [US3] Create `libs/shared/app_shared/jobs/lifecycle.py` (pure) — `resolve_finalized_status(success, failure, skipped, total) -> ScrapeJobStatus` as the single ordered **failure-centric** rule (`total==0`→COMPLETED; `failure==0`→COMPLETED (covers all-success, success+skipped, and skipped-only — skips are non-fatal); `failure>0` and `success>0`→PARTIAL_FAILED; `failure>0` and `success==0`→FAILED); `stall_window(now, timeout_seconds) -> int` = `floor(now.timestamp() / timeout_seconds)`. Per contracts/lifecycle-counters.md (D6). (FR-019, FR-020)
- [ ] T038 [US3] Create `libs/shared/app_shared/jobs/targets.py` — `mark_target(session, *, workspace_id, scrape_job_id, match_id, status, error_code=None) -> None` (single writer of a target's `status` + `started_at` on STARTED / `completed_at` on terminal / `error_code` on FAILED; touches ONLY the target row, never job counters; workspace-scoped) and `aggregate_counts(session, scrape_job_id, workspace_id) -> Counts(success, failure, skipped, total)` (one scoped `SELECT status, COUNT(*) ... GROUP BY status`). Per contracts/lifecycle-counters.md (D5) (depends on T007). (FR-017, FR-018)
- [ ] T039 [US3] Extend `apps/workers/app/workers/tasks_jobs.py` with `finalize_jobs()` and `refresh_job_counters()` (name `SCRAPE_FINALIZE_JOBS`, queue `maintenance`): scan non-terminal jobs; `refresh_job_counters` writes `aggregate_counts(...)` totals to the job row in **one** UPDATE (never per-target); `finalize_jobs` finalizes a job whose targets are **all** terminal — write counts, `status = resolve_finalized_status(...)`, `completed_at = now`; idempotent (re-running on a finalized job is a no-op). `set_workspace_context` per job. Per contracts/lifecycle-counters.md (depends on T026, T037, T038). (FR-018, FR-019, SC-004, SC-007)
- [ ] T040 [US3] Extend `apps/workers/app/workers/tasks_jobs.py` with `recover_stalled_batches()` (name `SCRAPE_RECOVER_STALLED`, queue `maintenance`): scan RUNNING jobs with `started_at` set; select targets still `status == PENDING` whose age past `started_at` exceeds `settings.SCRAPE_STALL_TIMEOUT_SECONDS`; exclude progressed (STARTED/terminal) and `locked_at`-live targets; **re-resolve each stalled target's `competitor_domain` + `mode` set-based** (same one-read pattern as T026, not per-target — U3); `re_batches = plan_batches(stalled_targets, ...)`; re-dispatch each via `client.schedule(..., batch_index=f"{batch_index}:r{stall_window(now, timeout)}", node_url=select_node(domain, nodes))` where `nodes` is the **mode-appropriate pool** (`SCRAPYD_BROWSER_URLS` for BROWSER, else `SCRAPYD_HTTP_URLS` — same as T026, I1) — the window-suffixed key re-dispatches the stalled batch while the `SET NX` guard neutralizes a duplicate recovery delivery within one window. Per contracts/stall-recovery.md (D4) (depends on T026, T037, T020, T021). (FR-015, SC-005)

### Tests for User Story 3

- [ ] T041 [P] [US3] Unit test `tests/unit/test_jobs_lifecycle.py` — `resolve_finalized_status` boundary values (failure-centric): all-success COMPLETED; success+skipped (no failures) COMPLETED; skipped-only (no failures) COMPLETED; mixed success+failure PARTIAL_FAILED; failure+skipped no-success FAILED; failure-only FAILED; zero-target COMPLETED. `stall_window` is stable within a window and increments across windows (depends on T037). (FR-019, FR-020, SC-007)
- [ ] T042 [P] [US3] Unit test `tests/unit/test_jobs_counters.py` — `aggregate_counts` over a fake session (GROUP BY status) → correct `Counts`; the finalize/refresh path writes counts in **one** UPDATE, never a per-target increment; `mark_target` transitions a target's status/timestamps/error_code and never mutates job counters (depends on T038, T039). (FR-017, FR-018, SC-004)
- [ ] T043 [P] [US3] Unit test `tests/unit/test_jobs_stall_recovery.py` (fakes) — targets still PENDING past the timeout → re-dispatched; STARTED/terminal or `locked_at`-live targets excluded; within one window a duplicate recovery delivery → one re-dispatch (SET NX no-op on the second), across windows a fresh suffixed key permits re-dispatch; same domain → same node on the re-dispatch (depends on T040). (FR-015, SC-005)

**Checkpoint**: Counters aggregate (never hot-row increments), finalization is deterministic, and stalled batches recover idempotently without double-running progressed work. US3 is independently testable.

---

## Phase 6: Integration (live-stack, authored + skip-marked) — ⏸ deferred live verification

**Purpose**: End-to-end scenarios against a real Postgres/Redis/Scrapyd stack. Authored now and **skip cleanly** where those services are unreachable (no container engine in this build env — SPEC-02→07 deferred-verification pattern). Each is deferred live verification. Zero real-competitor network calls; fixtures/loopback only.

- [ ] T044 [P] ⏸ DEFERRED (needs live Postgres/Redis) Author `tests/integration/test_jobs_run_match_live.py` (US1/SC-001) — seed ws/product/variant/competitor/match; `POST /v1/jobs/run/match/{id}` → job + 1 target (scoped), dispatch enqueued, `schedule.json` carries `workspace_id`/`scrape_job_id`/`match_ids`; job status/results readable and reflect target outcomes.
- [ ] T045 [P] ⏸ DEFERRED (needs live Postgres/Redis) Author `tests/integration/test_jobs_run_variant_live.py` (US2/SC-002) — variant with active + inactive matches → one target per ACTIVE match, `unique(scrape_job_id, match_id)` enforced; zero-active → COMPLETED, `total_targets=0`, no dispatch.
- [ ] T046 [P] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_jobs_counters_finalize_live.py` (US3/SC-004/SC-007) — simulate target transitions → counters aggregate (write count independent of target count); status finalizes COMPLETED/PARTIAL_FAILED/FAILED; `completed_at` set.
- [ ] T047 [P] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_jobs_isolation_live.py` (Isolation/SC-006) — cross-workspace job/target read + write blocked (app scoping + RLS); the workspace-local composite FK blocks a cross-ws target→job reference; no workspace context → 0 rows.
- [ ] T048 [P] ⏸ DEFERRED (needs live Redis/Scrapyd) Author `tests/integration/test_jobs_dispatch_scrapyd_live.py` (US1/SC-003) — authenticated `schedule.json` per batch carrying `workspace_id`/`scrape_job_id`/`match_ids`; a retried dispatch of the same `(scrape_job_id, batch_index)` does not double-run.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T049 [P] Extend `tests/unit/test_import_boundaries.py` — assert `app_shared.jobs.*` (batching, nodes, lifecycle, targets, service), `app_shared.messaging`, and `app_shared.models.jobs` import **no** scrapy/twisted/playwright/fastapi; the API router `apps/api/app/routers/jobs.py` imports `app_shared.jobs.service`/`app_shared.messaging`, never `apps.workers` (depends on T006, T020, T021, T022, T024, T032, T037, T038). (Principle I)
- [ ] T050 Run the DB/Redis-independent validation from `specs/008-jobs-orchestration/quickstart.md`: `uv run pytest tests/unit -q` green + `SPECIFY_FEATURE_DIRECTORY=specs/008-jobs-orchestration uv run alembic upgrade head --sql` renders both tables + `unique(scrape_job_id, match_id)` + `unique(workspace_id, id)` + the composite FK + RLS on both (single head, `down_revision == 2db33dea5e14`) + `uv run python scripts/check_workspace_scoping.py` exit 0 + `bash scripts/check_single_head.sh` single head + import-boundary green. (If deps missing, `uv sync --all-packages` first — never plain `uv sync`.)
- [ ] T051 [P] Confirm `apps/workers/app/workers/celery_app.py` registers the `scrape_dispatch` + `maintenance` queues/routes and includes `tasks_jobs` so `dispatch_job` / `finalize_jobs` / `refresh_job_counters` / `recover_stalled_batches` are discoverable by name (deployment sanity; no live worker run required here) (depends on T012, T026, T039, T040). (FR-011, FR-015)

### Result-path target terminalization (US1/US3 — makes FR-017/018/019 operational, remediates analyze U1)

- [ ] T052 Wire the SPEC-07 item pipeline to terminalize targets: in `libs/scrape-core/scrape_core/pipelines.py` `_flush_batch` (already inside the reactor-safe `run_in_thread` transaction, already carrying `scrape_job_id`/`match_id`/`success`/`error_code` per item), also call `app_shared.jobs.targets.mark_target(...)` for each item with a non-null `scrape_job_id` — COMPLETED on `item.success`, else FAILED with `item.error_code` — in the SAME transaction/session as the observation/attempt writes (no extra reactor hop). After the batch commits, enqueue `SCRAPE_FINALIZE_JOBS` (via `app_shared.messaging.enqueue`, queue `maintenance`) once per distinct affected `scrape_job_id`, so `finalize_jobs` resolves counters/status event-driven without depending on the SPEC-13 beat. Keep `scrape-core` import-clean (no fastapi/apps.workers; `app_shared.jobs.targets` + `app_shared.messaging` only). Per contracts/lifecycle-counters.md (depends on T038, T006, T039). (FR-017, FR-018, FR-019, SC-007)
- [ ] T053 [P] Unit test `tests/unit/test_pipeline_target_terminalization.py` — over a fake session + fake `mark_target`/`enqueue`: a successful item marks its target COMPLETED, a failed item marks FAILED with `error_code`, an item with `scrape_job_id is None` marks nothing; target marking shares the batch transaction (no second `run_in_thread`); exactly one `SCRAPE_FINALIZE_JOBS` enqueue per distinct `scrape_job_id` in the batch (depends on T052). (FR-017, SC-007)

---

## FR / SC Coverage

| Requirement | Task(s) |
|-------------|---------|
| FR-001 `scrape_jobs` entity (§22 shape) | T007, T008, T010, T013, T015, T044 |
| FR-002 `scrape_job_targets` + `unique(scrape_job_id, match_id)` | T007, T008, T010, T013, T015, T045 |
| FR-003 type/status/source enums | T001, T013, T023 |
| FR-004 workspace isolation (app scoping + RLS) | T009, T010, T014, T016, T024, T047 |
| FR-005 single-head forward migration | T008, T010, T015 |
| FR-006 `POST /run/match/{id}` + 404 on cross-ws | T003, T022, T024, T025, T029, T031, T044 |
| FR-007 `POST /run/variant/{id}`, inactive excluded | T032, T033, T034, T035, T045 |
| FR-008 `GET /jobs/{id}` status/counts/timestamps | T023, T024, T031 |
| FR-009 `GET /jobs/{id}/results` per-target outcomes | T023, T024, T031 |
| FR-010 provenance `requested_by`/`MANUAL`/`API` | T022, T029 |
| FR-011 `scrape_dispatch` batching by domain/mode 50–200 | T002, T020, T026, T027, T036, T051 |
| FR-012 authenticated `schedule.json` reusing the client | T011, T026, T030, T048 |
| FR-013 idempotent dispatch (no double-run) | T026, T030, T048 |
| FR-014 deterministic node selection | T011, T021, T028 |
| FR-015 stall detect + window-bucketed re-dispatch | T002, T040, T043, T051 |
| FR-016 Celery fork-safety (existing hook) | T012, T019 |
| FR-017 target state transitions (`mark_target` + result-path caller) | T038, T042, T052, T053 |
| FR-018 counters aggregated, never per-target increments | T038, T039, T042, T046, T052 |
| FR-019 deterministic finalization | T037, T039, T041, T046, T052 |
| FR-020 zero-target → COMPLETED, no dispatch | T032, T034, T035, T037, T041 |
| SC-001 async trigger returns job id | T022, T024, T026, T044 |
| SC-002 one unique target per active match | T032, T034, T035, T045 |
| SC-003 duplicate dispatch → exactly one run | T026, T030, T048 |
| SC-004 job-row writes independent of target count | T038, T039, T042, T046 |
| SC-005 stalled batch re-dispatched within timeout | T021, T028, T040, T043 |
| SC-006 no cross-workspace observe/affect; no-context 0 rows | T014, T016, T031, T047 |
| SC-007 counts/outcomes consistent at every terminal state | T037, T039, T041, T046, T052, T053 |
| SC-008 one Scrapyd job per batch, not per URL | T020, T026, T027, T036 |

Every FR-001..FR-020 and SC-001..SC-008 maps to ≥1 task.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies (T001–T006 all `[P]`, different files).
- **Foundational (Phase 2)**: depends on Setup (T001 enums → T007 models; T003 scopes → T018; T004 task-names → T012/messaging; T006 messaging → T017). **Blocks all user stories.** T008/T009/T010 depend on T007; T011/T012 are `[P]` (different files); tests T013–T019 depend on their targets.
- **US1 (Phase 3)**: depends on Foundational. Pure `batching`/`nodes` (T020/T021) `[P]`; `service.create_match_job` (T022) needs models+messaging; schemas (T023) then router (T024) then `main.py` (T025); `dispatch_job` (T026) needs batching+nodes+client+celery. Tests T027–T031 `[P]` once their targets land.
- **US2 (Phase 4)**: depends on US1 — extends `service.py` (T032 after T022), the router (T033 after T024/T032), and the service/router/batching test files (T034/T035/T036 after T029/T031/T027).
- **US3 (Phase 5)**: depends on Foundational; `lifecycle` (T037) `[P]`; `targets` (T038) needs models; `finalize_jobs`/`refresh_job_counters` (T039) and `recover_stalled_batches` (T040) extend `tasks_jobs.py` (after T026). Tests T041–T043 `[P]`.
- **Integration (Phase 6)**: depends on the corresponding user-story implementations; authored anytime, skip-marked. ⏸ deferred live verification.
- **Polish (Phase 7)**: after the desired stories (T049 import-boundary needs all `app_shared.jobs.*` + `messaging` + `models.jobs` to exist; T050 runs the full unit suite; T051 needs the tasks + celery wiring).

### Story-level & shared-file notes

- **US1**: independent after Foundational. Creates `service.py`, `schemas/jobs.py`, `routers/jobs.py`, `tasks_jobs.py`, `batching.py`, `nodes.py`.
- **US2**: **extends US1's `service.py` (T032 after T022), `routers/jobs.py` (T033 after T024)**, and the US1 service/router/batching **test files** (sequential edits to shared files).
- **US3**: **extends US1's `tasks_jobs.py` (T039/T040 after T026)**; the new `lifecycle.py`/`targets.py` + their tests are independent new files.
- Deferred (⏸) integration tasks are authored anytime but only pass on a Postgres/Redis/Scrapyd host.

### Within a story

- Pure `app_shared` module + its unit test before the `apps/api` router / `apps/workers` task that uses it.
- `schemas/*.py` before/with the router that imports them; router before its `main.py` registration.
- Task modules before their celery route/discovery confirmation (T051).

---

## Parallel Opportunities

- **Setup**: T001–T006 all `[P]` (different files).
- **Foundational**: T011, T012 `[P]` with each other; tests T013–T019 `[P]` once their targets land.
- **US1**: T020, T021 `[P]`; tests T027–T031 `[P]`.
- **US2**: T036 `[P]`; T032/T033 are sequential edits to shared US1 files.
- **US3**: T037 `[P]`; tests T041–T043 `[P]`.
- **Integration**: T044–T048 all `[P]` (distinct files).
- **Cross-story**: with Foundational done, US1 and US3's pure cores (`lifecycle`/`targets`) can be built in parallel; US2 slots in once US1's `service.py`/`routers/jobs.py` land.

### Parallel Example: US1 pure `app_shared` cores

```bash
# The two DB-independent orchestration cores + their corpora run fully in parallel (different files):
Task: "Create libs/shared/app_shared/jobs/batching.py + tests/unit/test_jobs_batching.py"
Task: "Create libs/shared/app_shared/jobs/nodes.py + tests/unit/test_jobs_node_selection.py"
```

### Parallel Example: Foundational unit tests

```bash
# After T007/T010/T012 land, run the DB-independent shape/RLS/offline/scoping/messaging/scope/fork tests together:
Task: "Unit test tests/unit/test_jobs_models.py"
Task: "Unit test tests/unit/test_jobs_rls.py"
Task: "Unit test tests/unit/test_migration_offline_jobs.py"
Task: "Unit test tests/unit/test_jobs_scoping_guard.py"
Task: "Unit test tests/unit/test_jobs_messaging.py"
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1 Setup → Phase 2 Foundational (models + migration + RLS + client `node_url` + celery queues; all shape/RLS/offline/scoping/messaging/scope/fork tests green).
2. Phase 3 US1 → run-match + dispatch + get/results, pure batching/node logic, service + dispatch orchestration against fakes.
3. **STOP & VALIDATE**: run the unit suite; author the deferred live run-match/dispatch tests for the PG/Redis/Scrapyd host.

### Incremental Delivery

1. Setup + Foundational → foundation ready.
2. US1 (P1, MVP) → single-match trigger→dispatch→observe loop.
3. US2 (P2) → variant fan-out (one unique target per active match, domain/mode batching, zero-active → COMPLETED).
4. US3 (P3) → aggregated counters + deterministic finalization + stalled-batch recovery.
5. Integration (⏸ skip-marked) + Polish (import boundaries, quickstart validation, celery discovery sanity).

### Deferred (live-Postgres/Redis/Scrapyd) tasks

T044, T045, T046, T047, T048 — authored here, left unchecked `- [ ]`, marked ⏸ DEFERRED (needs live infra). They cover the live halves of SC-001..SC-008: real run-match/run-variant end-to-end, actual `unique(scrape_job_id, match_id)` + composite-FK enforcement, RLS cross-workspace + no-context row denial, counter aggregation + deterministic finalization on real rows, and authenticated per-batch `schedule.json` with retried-dispatch no-double-run. No test contacts a real competitor domain.

---

## Notes

- `[P]` = different files, no dependency on an incomplete task.
- `[Story]` label maps a task to a user story for traceability; Setup / Foundational / Integration / Polish carry none.
- `app_shared` stays FastAPI-free and scrapy/twisted/playwright-free (T049 guards this); `app_shared.messaging` may import `celery` (the ban is scrapy/twisted/playwright/fastapi). The API router imports `app_shared.jobs.service`/`messaging`, never `apps/workers` (Constitution I).
- A "batch" is a **derived** `(domain, mode)` grouping with a deterministic `batch_index` — **no** third table (research D1); the idempotency guard is the reused SPEC-07 Redis `SET NX` key (D2); node selection is a stateless hash-by-domain (D3); stall recovery reads target state + a window bucket (D4).
- Reuse, do NOT rebuild: the SPEC-07 `ScrapydDispatchClient` (only extended with `node_url`), the `generic_price_spider`, the `worker_process_init` fork-safety hook (asserted, not re-added), `scoped_select`/`scoped_get`, `enum_column`, `emit_rls_policy`, `WorkspaceScopedBase`, the AST scoping guard, `deps.require_scopes`, `app_shared.redis_client`.
- One new scope minted: `jobs:write` (`jobs:read` already exists).
- Live-stack tests (Phase 6) are authored and skip cleanly — no Postgres/Redis/Scrapyd in this build env.
- Do NOT commit — the orchestrator commits after this step.
