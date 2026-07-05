---
description: "Task list for SPEC-13 Scheduler implementation"
---

# Tasks: Scheduler

**Input**: Design documents from `/srv/crawmatic/crawmatic/specs/013-scheduler/`

**Prerequisites**: plan.md ✅, spec.md ✅ (US1/US2/US3, 21 FRs), research.md ✅ (R1–R9),
data-model.md ✅ (`refresh_rules`), contracts/ ✅ (refresh-rules-api / scheduler-loop /
job-service-seam), quickstart.md ✅

**Tests**: INCLUDED. The spec/plan/quickstart mandate a two-tier strategy (specs 05–12 precedent):
pure-logic **unit tests** that run green in this DB-less build env, and **live-DB integration
tests** (`*_live.py`) guarded by a `skipif` probe that skip cleanly with no Postgres. Both are
first-class tasks below.

**Organization**: Tasks grouped by user story for independent implementation and testing.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: `[US1]`/`[US2]`/`[US3]` (Setup/Foundational/Polish carry no story label)
- Every task lists a concrete absolute-or-repo-relative file path.

## Path Conventions

Backend monorepo (uv workspace): shared lib in `libs/shared/app_shared/`, services in
`apps/api/` and `apps/scheduler/`, migrations in `alembic/versions/`, tests in
`tests/unit/` + `tests/integration/`. Paths below are repo-root-relative
(`/srv/crawmatic/crawmatic/...`).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Add the new dependency and package scaffolding shared by every later phase.

- [ ] T001 Add `croniter` (pure-Python, scraping-free) as a dependency of the shared lib in
  `libs/shared/pyproject.toml`, then refresh the lockfile with `uv lock` and
  `uv sync --all-packages` (never plain `uv sync` — it wipes workspace-member deps). Verify
  `croniter` imports no Scrapy/Twisted/Playwright/FastAPI. (research R1, FR-019)
- [ ] T002 [P] Create the new scraping-free scheduling package marker
  `libs/shared/app_shared/scheduling/__init__.py` (empty package). (plan Project Structure)
- [ ] T003 [P] Confirm the reused enum members exist in `libs/shared/app_shared/enums.py` —
  `ScrapeScope` (WORKSPACE/COMPETITOR/PRODUCT/VARIANT/PRODUCT_GROUP/MATCH), `ScrapeJobType.SCHEDULED`,
  `ScrapeJobSource.SCHEDULER`, `MatchStatus.ACTIVE`. No new enum is minted (research R6); add a
  member only if genuinely missing. This is a verification gate, not a rewrite.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The shared cadence primitive that BOTH the API (US1: first `next_run_at`, cron
validation) and the scheduler (US2: recompute per run) depend on. Must exist before US1/US2.

**⚠️ CRITICAL**: No user-story work can begin until this phase is complete.

- [ ] T004 Implement the cadence module `libs/shared/app_shared/scheduling/cadence.py`:
  `compute_next_run_at(rule, run_time) -> datetime` (cron:
  `croniter(rule.cron_expression, run_time_utc).get_next(datetime)`; interval:
  `run_time + timedelta(minutes=rule.interval_minutes)`), plus `validate_cron(expr) -> None`
  (raises on unparseable cron) and an interval guard (`> 0`). All datetimes tz-aware UTC; always
  base computation on the passed `run_time`/now (never the stale `next_run_at`), giving backlog
  fire-once for free. (research R1; FR-003/006/016/018)
- [ ] T005 [P] Unit-test the cadence module in `tests/unit/test_cadence.py`: cron next-occurrence,
  interval next-run, far-past `next_run_at` → single strictly-future result (backlog), invalid cron
  rejected, non-positive interval rejected. Runs green with no DB. (FR-003/006/016)

**Checkpoint**: Cadence math available and unit-verified — US1 and US2 can begin.

---

## Phase 3: User Story 1 - Configure recurring refresh schedules (Priority: P1) 🎯 MVP

**Goal**: Persist workspace-owned `refresh_rules` and expose full CRUD + enable/disable through
`/v1/refresh-rules`, with cadence/scope validation and workspace isolation (app-scoping + RLS).

**Independent Test**: Create a `WORKSPACE`/`cron` rule and a `PRODUCT_GROUP`/`interval` rule via the
API, read them back (correct first `next_run_at`, `enabled=true`), disable one, and confirm rules
in workspace A are invisible/unaddressable to workspace B. Negatives: neither/both cadence →
`422 INVALID_CADENCE`; missing/cross-workspace target id → `422 SCOPE_TARGET_MISMATCH`; bad cron →
`422 INVALID_CRON`.

### Implementation for User Story 1

- [ ] T006 [P] [US1] Create the `RefreshRule` model in
  `libs/shared/app_shared/models/refresh_rules.py`:
  `class RefreshRule(Base, WorkspaceScopedBase, TimestampMixin)` — UUIDv7 pk, `workspace_id`,
  `name`, `scope` (`enum_column(ScrapeScope)`→`String(32)`), nullable `product_id`/
  `product_variant_id`/`product_group_id`/`competitor_id`/`match_id`, `cron_expression`,
  `interval_minutes`, `priority` (default 0), `enabled` (default true), TIMESTAMPTZ `next_run_at`/
  `last_run_at`/`locked_at`. `__table_args__`: workspace FK; five **nullable composite** scope-target
  FKs `(workspace_id, X) -> table(workspace_id, id)` with `ondelete="CASCADE"` and short (<63-byte)
  names; CHECK `ck_refresh_rules_exactly_one_cadence` (`num_nonnulls(cron_expression, interval_minutes)=1`);
  CHECK `ck_refresh_rules_interval_minutes_positive`; CHECK `ck_refresh_rules_scope_target`
  (per-scope target-id matrix); partial index `ix_refresh_rules_due` on `(next_run_at) WHERE enabled`.
  Follow the `ScrapeJob`/`Competitor` pattern + `NAMING_CONVENTION`. (data-model.md; FR-001/002/003/018/020; research R6/R7)
- [ ] T007 [P] [US1] Register the model: add `from app_shared.models.refresh_rules import RefreshRule`
  and `"RefreshRule"` to `__all__` in `libs/shared/app_shared/models/__init__.py`, and add
  `RefreshRule` to the `WORKSPACE_OWNED_MODELS` frozenset in `libs/shared/app_shared/repository.py`
  so the unscoped-query CI guard (`scripts/check_workspace_scoping.py`) covers it. (FR-005; data-model Registration checklist)
- [ ] T008 [P] [US1] Author the Alembic migration
  `alembic/versions/<rev>_refresh_rules.py` with **`down_revision = 'f30c60cfa2f7'`** (verified single
  current head): hand-authored `create_table` (Uuid, `String(32)` enum, `DateTime(timezone=True)`),
  explicit PK / workspace FK / five CASCADE composite scope-target FKs / three CHECK constraints,
  then `op.create_index("ix_refresh_rules_due", ..., postgresql_where=sa.text("enabled"))`, then the
  `for statement in emit_rls_policy("refresh_rules"): op.execute(statement)` loop (ENABLE + FORCE RLS
  + workspace-isolation policy) so RLS is present from the first migration. `downgrade()` drops the
  index then the table. Mirror the SPEC-08 jobs migration. (FR-005/020; data-model Migration)
- [ ] T009 [P] [US1] Create Pydantic v2 schemas (`extra="forbid"`) in
  `apps/api/app/schemas/refresh_rules.py`: `RefreshRuleCreate`, `RefreshRuleUpdate`
  (all-optional, empty body → `EMPTY_UPDATE`), `RefreshRuleResponse` (`from_attributes=True`, all
  columns), `RefreshRuleListResponse` (`{items, next_cursor}`). Cross-field validators: exactly-one
  cadence → `INVALID_CADENCE`; `interval_minutes` `ge=1`; cron parseable via `validate_cron` →
  `INVALID_CRON`; scope↔target-id matrix → `SCOPE_TARGET_MISMATCH`. (FR-002/003; contract refresh-rules-api; research R9)
- [ ] T010 [US1] Implement the CRUD router `apps/api/app/routers/refresh_rules.py`
  (`APIRouter(prefix="/v1/refresh-rules", tags=["refresh-rules"])`, `refresh_rules:read/write`
  scopes) over the **ordinary RLS-enforced request session** (NOT the bypass session): `POST`
  (validate, verify target id in-workspace via `scoped_get` else `SCOPE_TARGET_MISMATCH`, compute
  first `next_run_at` via cadence, insert with `workspace_id=principal.workspace_id`, `201`);
  `GET` list (`scoped_select`, keyset on `(created_at, id)`, `clamp_limit`, `paginate`); `GET /{id}`
  (`scoped_get`→404); `PATCH /{id}` (`scoped_get`→404, `exclude_unset`, re-validate, recompute
  `next_run_at` if cadence changed — this is also the enable/disable path); `DELETE /{id}`
  (`scoped_get`→404, hard delete). Mirror `apps/api/app/routers/competitors.py`; structured
  `{"error":{"code","message"}}` envelope. (FR-004/005/006; contract refresh-rules-api)
- [ ] T011 [US1] Register the router: `include_router(refresh_rules.router)` in
  `apps/api/app/main.py`. (contract refresh-rules-api)

### Tests for User Story 1

- [ ] T012 [P] [US1] Unit test schema validation in `tests/unit/test_refresh_rules_validation.py`:
  neither/both cadence → `INVALID_CADENCE`; bad cron → `INVALID_CRON`; each scope's target-id matrix
  (WORKSPACE forbids ids; others require exactly theirs) → `SCOPE_TARGET_MISMATCH`; empty PATCH →
  `EMPTY_UPDATE`. No DB. (FR-002/003; US1 AS-5/6)
- [ ] T013 [P] [US1] Live integration test `tests/integration/test_refresh_rules_crud_live.py`:
  CRUD round-trip + first-`next_run_at` correctness + cross-workspace RLS denial (workspace B cannot
  read/write A's rule even with an omitted app filter). Guard with the
  `pytest.mark.skipif(not _live_refresh_rules_reachable(), ...)` probe (copy the
  `test_competitors_crud_live.py` idiom, probing `refresh_rules`). (FR-004/005; US1 AS-1..4; SC-001/004)
- [ ] T014 [P] [US1] Live integration test `tests/integration/test_refresh_rules_migration_live.py`:
  `alembic upgrade head` then `downgrade` round-trip for the `refresh_rules` migration (table +
  CHECKs + partial index + RLS present after upgrade, gone after downgrade). Same `skipif` probe. (FR-005)

**Checkpoint**: US1 is a complete, independently testable MVP — operators can capture refresh
policy via the API even before the scheduler loop acts on it.

---

## Phase 4: User Story 2 - Scheduler enqueues due jobs automatically (Priority: P1)

**Goal**: A scheduler pass claims due rules, resolves each scope to ACTIVE matches, creates a
`SCHEDULED`/`SCHEDULER` scrape job via the reused SPEC-08 job service (enqueue-before-commit), and
advances `next_run_at`/`last_run_at`/`locked_at` — including the zero-match advance and backlog
fire-once.

**Independent Test**: Seed an enabled rule with `next_run_at` in the past whose scope resolves to
≥1 ACTIVE match; call `run_refresh_pass(session_factory, now=now, batch_limit=100)`; assert exactly
one `ScrapeJob` (`type=SCHEDULED`, `source=SCHEDULER`) with one target per active match, its dispatch
enqueued, `last_run_at == run_time`, and `next_run_at` advanced one cadence into the future. A
zero-match rule advances its schedule with no job/dispatch.

**Dependency**: US2 uses the `RefreshRule` model (T006) and cadence (T004) from earlier phases.

### Implementation for User Story 2

- [ ] T015 [P] [US2] Add the new `Settings` knobs in `libs/shared/app_shared/config.py`:
  `SCHEDULER_POLL_INTERVAL_SECONDS: int = 30`, `SCHEDULER_CLAIM_BATCH_LIMIT: int = 100`, and
  `SYSTEM_DATABASE_URL` (env-overridable) that **falls back to `AUTH_DATABASE_URL`** when unset.
  Match the existing `STRATEGY_*_INTERVAL_SECONDS` pattern. (research R2/R8; FR-007)
- [ ] T016 [US2] Add the BYPASSRLS `get_system_session()` (+ `get_system_sessionmaker()`) helper in
  `libs/shared/app_shared/database.py`, bound to `Settings.SYSTEM_DATABASE_URL` (→ `AUTH_DATABASE_URL`
  fallback), mirroring the existing `get_auth_session()` seam for the cross-tenant claim. Document
  that this is used ONLY by the scheduler pass; the API CRUD path keeps the RLS-enforced request
  session. Depends on T015. (research R2; FR-005)
- [ ] T017 [P] [US2] Implement `resolve_scope_matches(session, *, workspace_id, scope, target_id)
  -> list[CompetitorProductMatch]` in `libs/shared/app_shared/jobs/scopes.py` — always
  `scoped_select(CompetitorProductMatch, workspace_id).where(status == MatchStatus.ACTIVE,
  <scope predicate>)` for all six scopes (WORKSPACE base-only; COMPETITOR/PRODUCT/VARIANT/MATCH id
  equality; PRODUCT_GROUP via `EXISTS(product_group_items ...)` with the product-arm OR variant-arm
  membership). A missing/dangling target id yields `[]` (no crash). (FR-010/020; research R4; contract job-service-seam)
- [ ] T018 [US2] Add `create_scope_job(session, *, workspace_id, scope, target_id, requested_by,
  job_type=ScrapeJobType.MANUAL, source=ScrapeJobSource.API) -> tuple[uuid|None, ScrapeJobStatus|None]`
  in `libs/shared/app_shared/jobs/service.py`: resolve via T017; **empty → `(None, None)`** (no job,
  no dispatch — FR-015); else create one `ScrapeJob(type=job_type, source=source, scope=scope,
  status=PENDING, total_targets=len(matches), workspace_id=..., requested_by=...)`, flush, one
  `ScrapeJobTarget(status=PENDING)` per match, then `_enqueue_dispatch(job.id, workspace_id)`
  **before returning (never commit — caller owns the transaction)**. Leave `create_match_job`/
  `create_variant_job` untouched. Depends on T017. (FR-011/012/015; research R3/R4; contract job-service-seam)
- [ ] T019 [US2] Implement the refresh pass
  `apps/scheduler/app/scheduler/refresh.py`: `run_refresh_pass(session_factory, *, now, batch_limit) -> int`
  doing **per-rule** claim→process→commit (NOT one batch transaction): loop up to `batch_limit`,
  each iteration opening a fresh transaction, claiming ONE row with
  `select(RefreshRule).where(enabled, next_run_at <= now).order_by(next_run_at).with_for_update(skip_locked=True).limit(1)`
  (break when none), calling `create_scope_job(..., job_type=SCHEDULED, source=SCHEDULER)`, setting
  `last_run_at=run_time`, `locked_at=run_time`, `next_run_at=compute_next_run_at(rule, run_time)`,
  then `commit()` (enqueue already happened → commit last). No global/advisory pass-lock; priority
  NOT in ORDER BY. Backlog rules fire once (cadence bases on `now`). (FR-007/008/009/012/013/015/016; research R5; contract scheduler-loop)
- [ ] T020 [US2] Extend the loop in `apps/scheduler/app/scheduler/scheduler_app.py`: add a second
  independent interval accumulator driven by `SCHEDULER_POLL_INTERVAL_SECONDS` that each elapsed
  interval calls `run_refresh_pass(get_system_sessionmaker(), now=utcnow, batch_limit=SCHEDULER_CLAIM_BATCH_LIMIT)`,
  logging-and-swallowing any pass exception (never crash-loop). PRESERVE the existing SIGTERM/SIGINT
  clean shutdown and the existing `STRATEGY_LIGHT_RECHECK` / `STRATEGY_STATS_FLUSH` accumulators.
  Depends on T015/T016/T019. (FR-007/019; contract scheduler-loop)

### Tests for User Story 2

- [ ] T021 [P] [US2] Unit test `tests/unit/test_scope_resolution.py`: assert the correct predicate
  branch is selected per scope (WORKSPACE base-only; COMPETITOR/PRODUCT/VARIANT/MATCH id filters;
  PRODUCT_GROUP EXISTS both membership arms) and `status == ACTIVE` always applied — verified against
  the compiled query/predicate without a live DB. (FR-010)
- [ ] T022 [P] [US2] Unit test `tests/unit/test_create_scope_job.py` with a fake/mock session:
  zero matches → returns `(None, None)`, creates no `ScrapeJob` and enqueues nothing; ≥1 match →
  creates the job + one target per match and calls `_enqueue_dispatch` **before** any commit
  (assert enqueue-before-commit ordering). (FR-011/012/015)
- [ ] T023 [P] [US2] Live integration test `tests/integration/test_scheduler_pass_live.py`
  (`skipif` probe): due rule fires exactly one `SCHEDULED`/`SCHEDULER` job with one target per active
  match and advances `last_run_at`/`next_run_at`; zero-match rule advances schedule with no job;
  far-past `next_run_at` fires once and lands strictly future (backlog). (US2 AS-1..6; SC-002/005/006)

**Checkpoint**: US1 + US2 = full MVP — configure a rule and it runs on schedule automatically.

---

## Phase 5: User Story 3 - No duplicate runs under concurrency or failure (Priority: P2)

**Goal**: Harden the pass for multi-instance operation and crash recovery — each due rule fires
exactly once per due moment (SKIP-LOCKED per-row claim), a crash before commit re-runs (not
double-fires), and one poison rule never rolls back or blocks the others (per-rule error isolation).

**Independent Test**: Point two overlapping `run_refresh_pass` transactions at the same due set →
each rule claimed/fired by exactly one (0 duplicate, 0 missed). Simulate crash after claim+enqueue
by rolling back instead of committing → `next_run_at` unchanged → next pass re-fires exactly once.
Force one rule's job creation to raise → only that rule rolls back; others already committed.

**Dependency**: US3 hardens the pass authored in US2 (T019/T020).

### Implementation for User Story 3

- [ ] T024 [US3] Add per-rule error isolation to `run_refresh_pass` in
  `apps/scheduler/app/scheduler/refresh.py`: wrap each rule's process step in `try/except`; on failure
  `session.rollback()` (undoing ONLY that rule — lock releases, `next_run_at` unchanged so it retries
  later), `logger.exception(...)`, and stop the pass (or track already-attempted rule ids) so the
  unchanged-`next_run_at` poison rule cannot be re-selected within the same pass and spin the loop;
  successfully fired rules keep their advanced `next_run_at` and are not re-selected. Rely on the
  SPEC-08 idempotent dispatch guard + SPEC-11 match locks to neutralize any leaked dispatch
  (duplicate-over-miss). Depends on T019. (FR-009/014/021; US3 AS-1..4; contract scheduler-loop)

### Tests for User Story 3

- [ ] T025 [P] [US3] Unit test `tests/unit/test_refresh_pass_isolation.py` with a fake session +
  stubbed `create_scope_job`: a rule whose processing raises triggers rollback of only its own
  transaction and does not prevent the pass from having committed earlier rules; assert the pass does
  not re-select the unchanged poison rule endlessly. (FR-021)
- [ ] T026 [P] [US3] Live integration test `tests/integration/test_scheduler_concurrency_live.py`
  (`skipif` probe): two overlapping passes over one due set → each rule fired exactly once
  (SKIP LOCKED, 0 dup / 0 miss); crash-before-commit (rollback after claim+enqueue) leaves
  `next_run_at` unchanged and a later pass re-fires exactly once; deleting a scope-target row
  (product/variant/group/competitor/match) cascade-deletes its referencing rule and the next pass
  neither blocks nor dereferences a missing target. (FR-008/014/020/021; US3 AS-1..4; SC-003; quickstart Scenarios 4/7)

**Checkpoint**: All three stories independently functional; multi-instance-safe and crash-safe.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Guardrails and end-to-end validation across all stories.

- [ ] T027 [P] Add `tests/unit/test_scheduler_import_boundary.py` asserting `apps/scheduler` (and the
  new `app_shared.scheduling`/`app_shared.jobs.scopes`) import no Scrapy/Twisted/Playwright, and
  confirm the existing `tests/unit/test_import_boundaries.py` still passes (croniter adds no forbidden
  import to `app_shared`). (FR-019; quickstart Scenario 8)
- [ ] T028 Verify the single-Alembic-head guard stays green: `uv run alembic heads` reports exactly
  one head (the new `refresh_rules` revision chained off `f30c60cfa2f7`); linear history preserved.
- [ ] T029 [P] Run the suites: `uv run pytest tests/unit -q` all green (no DB) and
  `uv run pytest tests/integration -q` with the `*_live.py` tests SKIPPING cleanly in this DB-less
  build env. (quickstart Run the tests)
- [ ] T030 Walk the quickstart.md validation scenarios (1–8) end-to-end as the acceptance checklist
  for `specs/013-scheduler/quickstart.md`.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies — start immediately.
- **Foundational (Phase 2)**: depends on Setup (needs `croniter` + `scheduling/` package). BLOCKS US1 & US2.
- **US1 (Phase 3)**: depends on Foundational (cadence for first `next_run_at` + cron validation).
- **US2 (Phase 4)**: depends on Foundational (cadence) + the `RefreshRule` model (T006) from US1.
- **US3 (Phase 5)**: depends on US2 (hardens the pass in T019/T020).
- **Polish (Phase 6)**: depends on all desired stories complete.

### Key Task-Level Edges

- T004 → T005; T004 → T010 (first `next_run_at`), T009 (`validate_cron`).
- T006 → T007, T008, T010, T013, T014; T006 also unblocks US2's pass (T019) and US3 tests.
- T009 → T010, T012.  T010 → T011.
- T015 → T016, T020.  T017 → T018.  T016 + T018 + T004 + T006 → T019 → T020.
- T017 → T021; T018 → T022; T019/T020 → T023.
- T019 → T024 → T025, T026.

### Parallel Opportunities

- Setup: T002, T003 in parallel (T001 first — it provides the dep).
- US1: T006, T007, T008, T009 in parallel (all off the model/schema layer); then T010 → T011.
  Tests T012, T013, T014 in parallel once their targets exist.
- US2: T015 and T017 in parallel; then T016 (after T015) and T018 (after T017); T019 → T020.
  Tests T021, T022, T023 in parallel.
- US3: T025, T026 in parallel after T024.
- Polish: T027, T029 in parallel; T028, T030 sequential checks.
- Once Foundational is done, US1 and US2 implementers can proceed largely in parallel (US2 only needs
  the `RefreshRule` model file T006, not the whole US1 API).

---

## Parallel Example: User Story 1

```bash
# After Foundational (T004) completes, launch the model/schema layer together:
Task: "T006 Create RefreshRule model in libs/shared/app_shared/models/refresh_rules.py"
Task: "T007 Register RefreshRule in models/__init__.py + repository.py WORKSPACE_OWNED_MODELS"
Task: "T008 Author refresh_rules Alembic migration (down_revision=f30c60cfa2f7)"
Task: "T009 Create Pydantic schemas in apps/api/app/schemas/refresh_rules.py"

# Then, once model + migration + router exist, launch the US1 tests together:
Task: "T012 Unit test validation in tests/unit/test_refresh_rules_validation.py"
Task: "T013 Live CRUD + cross-workspace RLS denial test (skipif)"
Task: "T014 Live migration upgrade/downgrade test (skipif)"
```

---

## Implementation Strategy

### MVP First

1. Phase 1 Setup → Phase 2 Foundational (cadence).
2. Phase 3 US1 (CRUD + model + migration + validation) → **STOP and VALIDATE** the independent test.
3. Phase 4 US2 (pass + job seam + scheduler loop) → configure-a-rule-it-runs is the true MVP.

### Incremental Delivery

1. Setup + Foundational → cadence ready.
2. US1 → operators can capture refresh policy via API (deploy/demo).
3. US2 → rules fire on schedule automatically (deploy/demo — MVP complete).
4. US3 → multi-instance + crash safety hardening (deploy/demo).
5. Polish → import-boundary + single-head guards + quickstart sign-off.

### Parallel Team Strategy

After Foundational: Dev A on US1 (API/model/migration), Dev B on US2 (job seam + pass, needs only
the T006 model file), then US3 hardening once US2's pass lands.

---

## Notes

- **[P]** = different files, no dependency on an incomplete task.
- **No live Docker/Postgres/Redis in this build env**: unit tasks (T005/T012/T021/T022/T025) run
  green here; live tasks (T013/T014/T023/T026) are authored as `*_live.py` that SKIP cleanly via the
  `skipif` probe and execute only against a real Postgres.
- **One documented deviation** (plan Complexity Tracking): the cross-tenant claim uses the BYPASSRLS
  `get_system_session()` (T016); app-level scoping is preserved on every job/target read/write and
  the API CRUD path never uses it.
- **Non-negotiables** reflected above: enqueue-before-commit (T018/T019), no global pass-lock
  (T019), per-rule error isolation (T024), RLS from the first migration (T008), scraping-free path
  (T001/T027).
- Commit after each task or logical group; stop at any checkpoint to validate a story independently.
