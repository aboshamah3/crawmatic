---
description: "Task list for SPEC-15 Retention, Rollups & Partition Maintenance implementation"
---

# Tasks: Retention, Rollups & Partition Maintenance

**Input**: Design documents from `/srv/crawmatic/crawmatic/specs/015-retention-rollups-partition-maintenance/`

**Prerequisites**: plan.md ✅, spec.md ✅ (US1–US4, 25 FRs + FR-009a, 4 clarifications), research.md ✅
(R1–R10), data-model.md ✅ (`variant_price_daily_rollups` + registry + partition/soft-ref entities),
contracts/ ✅ (partition-creation / daily-rollup / retention-drop / soft-reference-tolerance),
quickstart.md ✅ — all read and binding.

**Tests**: INCLUDED. The plan (Technical Context → Testing) and quickstart.md mandate the two-tier
strategy used by specs 05–14: pure-logic **unit tests** (+ one offline alembic-render test) that run
green in this DB-less build env and MUST pass, and **live-DB integration tests** (`*_live.py`) guarded
by a `skipif` probe that skip cleanly when no Postgres/Redis is present (this build env has no container
engine — see user memory). Both are first-class tasks below.

**Organization**: Tasks grouped by user story for independent implementation and testing. Setup +
Foundational block all stories. US1 (partition create-ahead, P1) is the standalone MVP.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: `[US1]`/`[US2]`/`[US3]`/`[US4]` (Setup/Foundational/Polish carry no story label)
- Every task lists a concrete repo-relative file path from `/srv/crawmatic/crawmatic/`.

## Path Conventions

Backend monorepo (uv workspace): shared lib in `libs/shared/app_shared/` (new scraping-free
`maintenance/` package), the three Celery tasks in `apps/workers/app/workers/`, the cadence enqueues in
`apps/scheduler/app/scheduler/`, the one new migration in `alembic/versions/`, tests in `tests/unit/` +
`tests/integration/`. Paths below are repo-root-relative.

## Reuse posture (applies to every task)

`app_shared` stays **scraping-free** (no Scrapy/Twisted/Playwright — FR-003, Principle I/V). Runtime
partition create/drop is **runtime DDL, not Alembic migrations** (R2, §29) — Alembic is used **exactly
once**, for the new `variant_price_daily_rollups` table (R1/FR-009a, `down_revision='93511d5f7885'`,
single head). All three maintenance tasks run under the existing **BYPASSRLS system session**
(`get_system_session`, R9) — the sanctioned SPEC-13 cross-tenant seam — with **app-level workspace
scoping preserved** on every rollup read/write (explicit `workspace_id=`; unscoped cross-tenant source
scans annotated `# noqa: workspace-scope`). Reuse SPEC-13's system-session infra, SPEC-09's persisted
`comparable` flag + `variant_price_states` surface, `Money`/`Numeric(18,4)`, `emit_rls_policy`,
`enum_column(AlertType)`, `TimestampMixin`, `new_uuid7` — **as-is**. No new third-party dependency.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: DB/env-tunable configuration knobs (Principle IV), task-name constants, and the new
scraping-free package marker shared by every later phase.

- [X] T001 Add the nine env/DB-tunable `Settings` knobs in `libs/shared/app_shared/config.py`
  (data-model §6, Principle IV — no hardcoded literals): five retention windows
  `RETENTION_PRICE_OBSERVATIONS_DAYS: int = 90`, `RETENTION_REQUEST_ATTEMPTS_DAYS: int = 90`,
  `RETENTION_PRICE_ALERT_EVENTS_DAYS: int = 365`, `RETENTION_WEBHOOK_EVENTS_DAYS: int = 90`,
  `RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS: int = 730`; three cadence intervals
  `PARTITION_CREATE_INTERVAL_SECONDS: int = 86400`, `DAILY_ROLLUP_INTERVAL_SECONDS: int = 86400`,
  `RETENTION_INTERVAL_SECONDS: int = 86400`; and `PARTITION_CREATE_LOOKAHEAD_MONTHS: int = 1`. Match
  the existing `SCHEDULER_*`/`STRATEGY_*_INTERVAL_SECONDS` pattern; reuse the existing
  `SYSTEM_DATABASE_URL` (→ `AUTH_DATABASE_URL` fallback) — add no new session knob. (research R3/R8; FR-017)
- [X] T002 [P] Add the three maintenance task-name constants to `libs/shared/app_shared/task_names.py`:
  `MAINTENANCE_PARTITION_CREATE = "maintenance.partition_create"`,
  `MAINTENANCE_DAILY_ROLLUP = "maintenance.daily_rollup"`,
  `MAINTENANCE_RETENTION_DROP = "maintenance.retention_drop"` (research R8; contracts headers).
- [X] T003 [P] Create the new scraping-free package marker
  `libs/shared/app_shared/maintenance/__init__.py` (empty package). (plan Project Structure)

**Checkpoint**: Config knobs read at settings-build time; task names and the `maintenance` package exist.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The partition **registry** and the shared partition **primitives** (existence gate +
catalog discovery) that BOTH US1 (create-ahead) and US3 (retention) depend on. Must exist before any
user story.

**⚠️ CRITICAL**: No user-story work can begin until this phase is complete.

- [X] T004 Implement the registry module `libs/shared/app_shared/maintenance/registry.py` (data-model §2,
  research R3): frozen `@dataclass PartitionedTable(name, partition_key, feeds_rollups,
  retention_setting)` + the module-level `PARTITIONED_TABLES` tuple with exactly the four entries —
  `price_observations`/`scraped_at`/`feeds_rollups=True`/`RETENTION_PRICE_OBSERVATIONS_DAYS`,
  `request_attempts`/`created_at`/`False`/`RETENTION_REQUEST_ATTEMPTS_DAYS`,
  `price_alert_events`/`created_at`/`False`/`RETENTION_PRICE_ALERT_EVENTS_DAYS`,
  `webhook_events`/`created_at`/`False`/`RETENTION_WEBHOOK_EVENTS_DAYS` (absent until SPEC-16) — plus a
  `retention_days(entry, settings) -> int` helper resolving the `Settings` attr by name.
  `variant_price_daily_rollups` is deliberately **not** in this registry (not partitioned). Scraping-free.
  (FR-001; data-model §2)
- [X] T005 Implement the shared partition primitives in `libs/shared/app_shared/maintenance/partitions.py`
  (data-model §3, research R2/R4) — used by BOTH US1 and US3, scraping-free, system-session shaped:
  `table_exists(session, name) -> bool` via `SELECT to_regclass('public.'+name)` (NULL → False, FR-002/R4);
  `existing_partitions(session, parent) -> list[(name, start, end)]` discovering child partitions from
  `pg_catalog` (`pg_inherits` + `pg_get_expr(relpartbound)`) with parsed half-open UTC bounds (used by
  US3 drop eligibility); and the `{parent}_{YYYY}_{MM}` name/suffix helpers. No DDL here (create in US1,
  drop in US3). (FR-002/006/018; research R2/R4)
- [X] T006 [P] Unit test `tests/unit/test_partition_registry.py`: assert `PARTITIONED_TABLES` shape (four
  entries, correct partition keys, `feeds_rollups` True only for `price_observations`, retention-setting
  names resolve via `retention_days`) and that the `table_exists` gate query is built against
  `to_regclass` (compiled-SQL/predicate assertion — no live DB). (FR-001/002)

**Checkpoint**: Registry + partition primitives available and unit-verified — US1 and US3 can begin.

---

## Phase 3: User Story 1 - Next month's partitions exist before writes need them (Priority: P1) 🎯 MVP

**Goal**: A scheduled maintenance task ensures the current + next month partition exists for every
**existing** registered table (self-healing, idempotent, correct across Dec→Jan/Feb), enqueued by the
scheduler loop on a daily cadence so next-month always exists before the month begins.

**Independent Test**: Run the partition-creation job at any date; verify current + next-month partitions
now exist for every registered *existing* table, that re-running is a no-op (no error/duplicate), that a
missing current-month partition is self-healed, that `webhook_events` (absent) is skipped without error,
and that a write dated into next month succeeds. (quickstart US1)

### Implementation for User Story 1

- [X] T007 [US1] Add `month_partition_bounds(now_utc, offset) -> (suffix, start, end)` to
  `libs/shared/app_shared/maintenance/partitions.py` — half-open `[YYYY-MM-01, <next-month>-01)` in UTC,
  correct across Dec→Jan and Feb lengths, mirroring the migration `_month_partition_bounds` convention;
  `offset=0` yields the current month (self-heal), `offset=1` next month. tz-aware UTC (FR-025).
  (FR-004/005/007; contract partition-creation.md §Bounds)
- [X] T008 [US1] Implement `create_missing_partitions(session, *, now_utc, lookahead_months) -> RunReport`
  in `libs/shared/app_shared/maintenance/partitions.py` (contract partition-creation.md): for each
  registry entry, `table_exists` gate → skip absent (record `tables_skipped_absent`); else for offsets
  `0..lookahead_months` compute bounds (T007), pre-check catalog existence and issue
  `CREATE TABLE IF NOT EXISTS {parent}_{suffix} PARTITION OF {parent} FOR VALUES FROM ('{start}') TO
  ('{end}')` (idempotent, RLS inherited from parent — no per-partition DDL), recording
  `partitions_created`. Return the structured RunReport (data-model §5). (FR-004/005/006/007/008)
- [X] T009 [US1] Create `apps/workers/app/workers/tasks_maintenance.py` with the
  `@app.task(name=MAINTENANCE_PARTITION_CREATE)` wrapper that opens a `get_system_session()` (BYPASSRLS,
  R9), calls `create_missing_partitions(session, now_utc=utcnow(), lookahead_months=Settings.
  PARTITION_CREATE_LOOKAHEAD_MONTHS)`, commits, and emits one structured run-report log line (FR-023).
  Mirror the `finalize_jobs`/`strategy_stats_flush` maintenance-task idiom. (research R8/R9; FR-003/023)
- [X] T010 [US1] Wire the task into Celery in `apps/workers/app/workers/celery_app.py`: add
  `tasks_maintenance` to `include`, and add `MAINTENANCE_PARTITION_CREATE` to the `task_routes` mapping
  → the existing `maintenance` queue (leave `task_queues`/other routes unchanged). (research R8)
- [X] T011 [US1] Extend the scheduler loop in `apps/scheduler/app/scheduler/scheduler_app.py`: add a new
  independent interval accumulator driven by `PARTITION_CREATE_INTERVAL_SECONDS` that each elapsed
  interval enqueues `MAINTENANCE_PARTITION_CREATE` via `app_shared.messaging.enqueue` (fire-and-forget,
  mirroring the existing `STRATEGY_STATS_FLUSH`/refresh-pass accumulators), logging-and-swallowing any
  enqueue error. PRESERVE the existing accumulators + SIGTERM/SIGINT clean shutdown. (research R8; FR-003)
- [X] T012 [P] [US1] Unit test `tests/unit/test_partition_bounds.py`: `month_partition_bounds` half-open
  bounds, Dec→Jan year rollover, Feb length, `offset=0` current vs `offset=1` next, tz-aware UTC. No DB.
  (FR-007; US1 edge case "month/year boundary")
- [X] T013 [P] [US1] Live integration test `tests/integration/test_partition_create_live.py` (`skipif`
  Postgres probe): current + next-month partitions created for every existing registered table; re-run
  is a no-op (FR-006); a missing current-month partition is self-healed (FR-005); `webhook_events`
  (absent) skipped without error (FR-002); a write dated into next month succeeds. (US1 AS-1..4; SC-001/002)

**Checkpoint**: US1 is a complete, independently testable MVP — the calendar-driven outage guarantee
(next-month partition always exists) holds even before rollups or retention ship.

---

## Phase 4: User Story 2 - Daily rollups summarize each day's pricing (Priority: P2)

**Goal**: Create the new `variant_price_daily_rollups` table (FR-009a) and a scheduled task that upserts
one rollup row per (workspace, variant, UTC day) — client price + competitor min/avg/max + comparable
count + alert type — aggregated from that day's `price_observations` (comparable, same-currency only),
with client fields read from the SPEC-09 `variant_price_states` surface.

**Independent Test**: Seed a day of raw observations + current-price state for several variants; run the
rollup job for that day; verify exactly one row per (workspace, variant, day) with correct client price,
min/avg/max competitor prices, comparable count, and alert type; currency-mismatched competitor prices
excluded from aggregates and count; a zero-comparable variant still gets a row (count 0, competitor
prices NULL); and re-running the same day upserts without duplication. (quickstart US2)

### Implementation for User Story 2

- [X] T014 [P] [US2] Create the `VariantPriceDailyRollup` model in
  `libs/shared/app_shared/models/rollups.py` — `class VariantPriceDailyRollup(Base, WorkspaceScopedBase,
  TimestampMixin)`, template `VariantPriceState` (`models/alerts.py`): UUIDv7 `id` pk, `workspace_id`
  (FK anchor), `product_id`/`product_variant_id` (soft `Uuid`, no FK), `date` (`Date`, UTC calendar
  day), `currency` (`CHAR(3)`), `client_price` (`Money`, NOT NULL), `cheapest_/average_/
  highest_competitor_price` (`Money`, nullable), `comparable_competitor_count` (`Integer`, NOT NULL),
  `latest_alert_type` (`enum_column(AlertType)`). `__table_args__`: workspace FK; `UniqueConstraint(
  "workspace_id","product_variant_id","date", name="uq_variant_price_daily_rollups_workspace_id_
  product_variant_id_date")` (upsert arbiter); `Index("ix_variant_price_daily_rollups_workspace_id",...)`
  and `Index("ix_variant_price_daily_rollups_date",...)`; follow `NAMING_CONVENTION`. (data-model §1;
  FR-009/012/013/014/025)
- [X] T015 [P] [US2] Register the model: add `from app_shared.models.rollups import
  VariantPriceDailyRollup` and `"VariantPriceDailyRollup"` to `__all__` in
  `libs/shared/app_shared/models/__init__.py`, and add `VariantPriceDailyRollup` to the
  `WORKSPACE_OWNED_MODELS` frozenset in `libs/shared/app_shared/repository.py` so the unscoped-query CI
  guard (`scripts/check_workspace_scoping.py`) covers it. (FR-014; data-model §1 Registration)
- [X] T016 [P] [US2] Author the Alembic migration
  `alembic/versions/<rev>_variant_price_daily_rollups.py` with **`down_revision = '93511d5f7885'`**
  (current single head): hand-authored `op.create_table(...)` (Uuid, `Date`, `CHAR(3)`, `Numeric(18,4)`,
  `String(32)` enum, `DateTime(timezone=True)`) with explicit PK / workspace FK / the unique constraint,
  then `op.create_index(...)` for the `workspace_id` and `date` indexes, then the
  `for stmt in emit_rls_policy("variant_price_daily_rollups"): op.execute(stmt)` loop (ENABLE + FORCE
  RLS + fail-closed workspace-isolation policy) so RLS is present from the first migration. `downgrade()`
  drops indexes then table. Mirror `2db33dea5e14`/the SPEC-13 refresh_rules migration. (FR-009a; data-model §1 Migration)
- [X] T017 [US2] Implement `run_daily_rollup(session, *, target_date=None) -> RunReport` in
  `libs/shared/app_shared/maintenance/rollups.py` (contract daily-rollup.md, research R6): default
  `target_date` = most-recent-completed UTC day (yesterday UTC); cross-tenant scan
  (`# noqa: workspace-scope`) of distinct `(workspace_id, product_variant_id, product_id)` with ≥1
  `price_observations` where `scraped_at::date = D`; per pair aggregate competitor
  min/avg/max + count over `price_observations WHERE scraped_at::date=D AND workspace_id=:ws AND
  product_variant_id=:pv AND success AND comparable AND price IS NOT NULL AND currency=:client_currency`
  (exact `NUMERIC` Decimal — FR-012; currency filter excludes mismatches from agg **and** count —
  FR-011); read `client_price`/`currency`/`latest_alert_type` from `variant_price_states` scoped by
  ws+pv (R6); UPSERT `variant_price_daily_rollups` `ON CONFLICT (workspace_id, product_variant_id, date)
  DO UPDATE` with explicit `workspace_id=` (FR-010/014); zero-comparable variant → row with client price,
  NULL competitor prices, count 0 (FR-013). Return RunReport (`rollups_upserted`). (FR-009/010/011/012/013/014)
- [X] T018 [US2] Add the `@app.task(name=MAINTENANCE_DAILY_ROLLUP)` wrapper to
  `apps/workers/app/workers/tasks_maintenance.py` — opens `get_system_session()`, calls
  `run_daily_rollup(session)` (default day; accept an optional `target_date` arg for backfill), commits,
  emits the structured run-report log line (FR-023). (research R8/R9)
- [X] T019 [US2] Add `MAINTENANCE_DAILY_ROLLUP` to the `task_routes` mapping → `maintenance` queue in
  `apps/workers/app/workers/celery_app.py` (extends T010). (research R8)
- [X] T020 [US2] Add a `DAILY_ROLLUP_INTERVAL_SECONDS` interval accumulator to
  `apps/scheduler/app/scheduler/scheduler_app.py` that enqueues `MAINTENANCE_DAILY_ROLLUP`
  fire-and-forget (mirrors T011). Preserve existing accumulators. (research R8)
- [X] T021 [P] [US2] Unit test `tests/unit/test_rollup_aggregation.py`: currency-mismatch exclusion from
  min/avg/max **and** count (FR-011), correct min/avg/max, exact `Decimal` money with no float/NaN/Inf
  (FR-012), comparable count, and the zero-comparable → count-0 / NULL-competitor-price case (FR-013) —
  against the compiled aggregate predicate / a pure aggregation helper, no live DB. (FR-011/012/013)
- [X] T022 [P] [US2] Unit test `tests/unit/test_migration_offline_rollups.py`: assert the new migration
  renders offline (`alembic upgrade head --sql` produces `CREATE TABLE variant_price_daily_rollups` + the
  RLS statements), that `alembic heads` yields exactly **one** head, and that the new revision's
  `down_revision == '93511d5f7885'`. Mirror `tests/unit/test_strategy_single_head.py`. Runs green, no DB.
  (FR-009a; research cross-cutting single-head)
- [X] T023 [P] [US2] Live integration test `tests/integration/test_daily_rollup_live.py` (`skipif`
  probe): one row per (workspace, variant, day) with correct client price + competitor min/avg/max +
  count + alert type; currency-mismatched competitor prices excluded (SC-006); zero-comparable variant
  → row with count 0 / NULL competitor prices (US2 AS-3); re-run of the same day upserts (no
  duplicate/corruption); and a cross-workspace RLS-denial check on the new table. (US2 AS-1..4; SC-002/006)

**Checkpoint**: Rollups exist and populate correctly — the durable 2-year summary surface is live and is
the precondition for safe retention (US3).

---

## Phase 5: User Story 3 - Expired partitions dropped, but only after rollups verified (Priority: P2)

**Goal**: A scheduled task drops whole expired monthly partitions via `DROP TABLE` (never bulk DELETE);
for `price_observations` (the only `feeds_rollups` table) it first verifies date-level rollup coverage;
non-rollup tables drop by age alone; and the small non-partitioned rollup table is aged by a single
sanctioned `DELETE`.

**Independent Test**: Create partitions older than the window with known rollup coverage; run retention;
verify partitions with complete coverage are dropped (via `DROP TABLE`, no bulk DELETE on raw tables),
partitions with missing coverage are retained + flagged `skipped_pending_rollups`, in-window partitions
untouched, and `request_attempts`/`price_alert_events` drop by age alone with their own windows.
(quickstart US3)

### Implementation for User Story 3

- [X] T024 [US3] Add `drop_partition(session, name)` to
  `libs/shared/app_shared/maintenance/partitions.py` — `DROP TABLE IF EXISTS {name}` (partition-drop,
  never bulk DELETE — FR-015; idempotent/concurrent-safe via `IF EXISTS` — FR-020). No workspace rows;
  DDL on the system session. (FR-015/020; contract retention-drop.md)
- [X] T025 [US3] Implement retention logic in `libs/shared/app_shared/maintenance/retention.py` (contract
  retention-drop.md, research R7): `partition_eligible(part, cutoff) -> bool` (whole half-open range
  `end <= cutoff`, deterministic — FR-018); `rollups_cover(session, part) -> bool` — the date-level
  verify-before-drop `SELECT DISTINCT scraped_at::date FROM {part.name} EXCEPT SELECT DISTINCT date FROM
  variant_price_daily_rollups WHERE date>=d0 AND date<dN` returns empty (cross-tenant, system session —
  FR-016); and `run_retention(session, *, now_utc) -> RunReport` — **Part A**: per registry entry,
  existence gate → skip absent; `cutoff = now_utc - retention_days(entry)`; for each `existing_partitions`
  eligible partition, if `entry.feeds_rollups` require `rollups_cover` (else record
  `partitions_skipped_pending_rollups`), then `drop_partition` (record `partitions_dropped`);
  non-`feeds_rollups` tables drop by age alone (FR-019). **Part B**: the one sanctioned bulk
  `DELETE FROM variant_price_daily_rollups WHERE date < (now_utc::date -
  RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS)` (age policy for the non-partitioned rollup table, R7).
  Return the RunReport. (FR-015/016/017/018/019/020; SC-003/004/005)
- [X] T026 [US3] Add the `@app.task(name=MAINTENANCE_RETENTION_DROP)` wrapper to
  `apps/workers/app/workers/tasks_maintenance.py` — opens `get_system_session()`, calls
  `run_retention(session, now_utc=utcnow())`, commits, emits the structured run-report log line (the
  `dangling_soft_refs_tolerated` field is wired in by US4/T033). (research R8/R9; FR-023)
- [X] T027 [US3] Add `MAINTENANCE_RETENTION_DROP` to the `task_routes` mapping → `maintenance` queue in
  `apps/workers/app/workers/celery_app.py` (extends T010/T019). (research R8)
- [X] T028 [US3] Add a `RETENTION_INTERVAL_SECONDS` interval accumulator to
  `apps/scheduler/app/scheduler/scheduler_app.py` that enqueues `MAINTENANCE_RETENTION_DROP`
  fire-and-forget (mirrors T011/T020). Preserve existing accumulators. (research R8)
- [X] T029 [P] [US3] Unit test `tests/unit/test_retention_eligibility.py`: `partition_eligible` true only
  when the whole half-open range is `<= cutoff` (boundary partition deterministic — FR-018), per-table
  window resolution (obs/attempts 90, alerts 365, rollups 730 — FR-017), and that `feeds_rollups=False`
  entries skip the coverage check (FR-019). Assert the `rollups_cover` EXCEPT query shape without a live
  DB. (FR-017/018/019)
- [X] T030 [P] [US3] Live integration test `tests/integration/test_retention_drop_live.py` (`skipif`
  probe): an expired `price_observations` partition with complete date coverage is dropped via
  `DROP TABLE` (assert no bulk DELETE issued on raw tables — SC-003); one with missing coverage is
  **retained** + reported `skipped_pending_rollups` (SC-004); an in-window partition is untouched;
  `request_attempts`/`price_alert_events` drop by age alone with their own windows (FR-019); re-run does
  not drop twice (FR-020); and the rollup-table `DELETE` ages out rows older than 730 days. (US3 AS-1..4; SC-003/004/005)

**Checkpoint**: Retention is bounded, drop-only, and verify-before-drop-safe; a partition with missing
rollups is always retained and surfaced.

---

## Phase 6: User Story 4 - Readers tolerate references into dropped partitions (Priority: P3)

**Goal**: Confirm every reader of `match_current_prices` relies on its denormalized fields (never a hard
join on `observation_id` into `price_observations`, which may dangle after a partition drop), and add an
operator-visibility tolerance check that counts dangling soft references as expected, not corruption.

**Independent Test**: Take a `match_current_prices` row whose `observation_id` points into an
already-dropped partition; exercise every read path that consumes it; verify each returns correct
denormalized data with no error/500/row-drop, and that the tolerance check reports such refs as
tolerated. (quickstart US4)

### Implementation for User Story 4

- [ ] T031 [US4] **AUDIT** every existing consumer of `match_current_prices` under `apps/api/app/` (and
  any price-comparison surface) to confirm none inner-joins/dereferences `observation_id` into
  `price_observations` in a way that would drop or error the row when the observation's partition is
  gone — they must rely on the denormalized fields (`price`, `old_price`, `currency`, `comparable`,
  `stock_status`, `success`, `error_code`, `extraction_method`, `extraction_confidence`, `scraped_at`).
  Record the audit result; fix any hard-join reader to tolerate a `None` observation (expected
  not-found, no exception). (FR-021; contract soft-reference-tolerance.md §FR-021; SC-007)
- [ ] T032 [US4] Implement the tolerance check in `libs/shared/app_shared/maintenance/soft_refs.py`
  (contract soft-reference-tolerance.md §FR-022): `count_tolerated_dangling_refs(session) -> int`
  counting `match_current_prices WHERE observation_id IS NOT NULL AND observation_id NOT IN (SELECT id
  FROM price_observations)` — a cross-tenant (`# noqa: workspace-scope`) best-effort probe on the system
  session, classifying the result as expected/tolerated (informational, never an error/corruption
  signal). (FR-022)
- [ ] T033 [US4] Wire the tolerance check into the retention task in
  `apps/workers/app/workers/tasks_maintenance.py`: after `run_retention`, best-effort call
  `count_tolerated_dangling_refs(session)` and add `dangling_soft_refs_tolerated` to the run-report log
  line, wrapped so it can NEVER block or fail the core create/rollup/drop guarantees — applying FR-024's
  non-blocking principle to this optional check (FR-024 itself builds no vacuum/analyze; out of v1 scope
  per spec). (FR-022)
- [ ] T034 [P] [US4] Live integration test `tests/integration/test_soft_ref_tolerance_live.py` (`skipif`
  probe): a `match_current_prices` row whose `observation_id` points into an already-dropped partition
  still loads and returns correct denormalized data with no error/500/row-drop (US4 AS-1); an explicit
  fetch of the missing raw row is an expected `None`; and `count_tolerated_dangling_refs` reports it as
  tolerated (US4 AS-2). (SC-007)

**Checkpoint**: All four stories complete — readers are dangling-ref-safe and the maintenance report
surfaces tolerated soft refs.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Import-boundary + workspace-scoping + single-head guardrails, deploy-topology fix, and
end-to-end validation across all stories.

- [ ] T035 [P] Extend `tests/unit/test_import_boundaries.py` to assert the new
  `app_shared.maintenance.*` package and `apps/workers/app/workers/tasks_maintenance.py` import no
  Scrapy/Twisted/Playwright (FR-003, Principle I/V), and run `uv run python
  scripts/check_workspace_scoping.py` to confirm the guard passes (the sanctioned cross-tenant scans in
  `rollups.py`/`retention.py`/`soft_refs.py` are annotated `# noqa: workspace-scope`; every rollup
  read/write carries explicit `workspace_id=`). (FR-003/014; quickstart Setup/build checks)
- [ ] T036 Fix the deploy topology in `docker-compose.yml`: add the broker/`redis` service to the
  `scheduler` service's `depends_on` (it now enqueues three more maintenance tasks; the existing gap is
  latent — research R8 operational note). Do not change job logic.
- [ ] T037 Verify the single-Alembic-head guard stays green: `uv run alembic heads` reports exactly one
  head (the new `variant_price_daily_rollups` revision chained off `93511d5f7885`); linear history
  preserved. (research cross-cutting; FR-009a)
- [ ] T038 [P] Run the suites: `uv run pytest tests/unit -q` all green (no DB — includes
  `test_partition_registry`, `test_partition_bounds`, `test_rollup_aggregation`, `test_retention_eligibility`,
  `test_migration_offline_rollups`, `test_import_boundaries`) and `uv run pytest tests/integration -q`
  with the four `*_live.py` tests SKIPPING cleanly in this DB-less build env. (quickstart)
- [ ] T039 Walk the quickstart.md validation scenarios (Setup + US1–US4 + the SC-001..007 coverage table)
  end-to-end as the acceptance checklist for
  `specs/015-retention-rollups-partition-maintenance/quickstart.md`.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies — start immediately.
- **Foundational (Phase 2)**: depends on Setup (needs config knobs + the `maintenance` package). BLOCKS US1 & US3.
- **US1 (Phase 3)**: depends on Foundational (registry + partition primitives). The MVP.
- **US2 (Phase 4)**: depends on Setup (config, task names) + the `maintenance` package; independent of
  US1's files (disjoint) — can be built in parallel with US1. Creates the rollup table US3 needs.
- **US3 (Phase 5)**: depends on Foundational (registry + `existing_partitions`) **and** US2 (the
  `variant_price_daily_rollups` table its coverage check reads and its Part-B DELETE ages).
- **US4 (Phase 6)**: depends on US3 (extends the retention task's report) + the existing
  `match_current_prices`/`price_observations` schema (no new migration).
- **Polish (Phase 7)**: depends on all desired stories complete.

### Key Task-Level Edges

- T001 → T009/T011/T017/T020/T025/T028 (knobs); T002 → T010/T019/T027 (task names); T003 → all maintenance modules.
- T004 → T008, T025, T029; T005 → T008 (create), T024/T025 (drop/eligibility).
- T007 → T008; T008 → T009 → T010, T011; T008 → T012, T013.
- T014 → T015, T016, T017, T023; T016 → T022; T017 → T018 → T019, T020; T017 → T021, T023.
- T024 + T025 → T026 → T027, T028; T025 → T029, T030.
- T031/T032 → T033; T032 → T034.

### Parallel Opportunities

- Setup: T002, T003 in parallel (T001 independent too — all three can run together).
- Foundational: T004 and T005 are different files (parallelizable); T006 after both.
- US1: T012 after T007/T008; T013 after the task/scheduler wiring; T009→T010/T011 sequential (shared files).
- US2: T014, T015, T016 in parallel (model / registration / migration); then T017 → T018 → T019/T020;
  tests T021, T022, T023 in parallel once their targets exist.
- US3: T029, T030 in parallel after T025/T026.
- US4: T034 in parallel after T032; T031 (audit) can run any time after the schema is understood.
- Polish: T035, T038 in parallel; T036, T037, T039 sequential checks.
- With staffing: after Foundational, US1 (`apps/scheduler` + `maintenance/partitions.py`) and US2
  (`models/` + migration + `maintenance/rollups.py`) proceed concurrently on disjoint files.

---

## Parallel Example: User Story 2

```bash
# Launch the model / registration / migration layer together (disjoint files):
Task: "T014 Create VariantPriceDailyRollup model in libs/shared/app_shared/models/rollups.py"
Task: "T015 Register the model in models/__init__.py + repository.py WORKSPACE_OWNED_MODELS"
Task: "T016 Author the variant_price_daily_rollups migration (down_revision=93511d5f7885)"

# Then, once model + migration + aggregation exist, launch the US2 tests together:
Task: "T021 Unit test rollup aggregation in tests/unit/test_rollup_aggregation.py"
Task: "T022 Offline migration render + single-head test in tests/unit/test_migration_offline_rollups.py"
Task: "T023 Live rollup upsert/currency/RLS test (skipif)"
```

---

## Implementation Strategy

### MVP First

1. Phase 1 Setup → Phase 2 Foundational (registry + partition primitives).
2. Phase 3 US1 (create-ahead task + scheduler cadence) → **STOP and VALIDATE** the independent test —
   this alone prevents the calendar-driven write outage (SC-001) and is deployable standalone.

### Incremental Delivery

1. Setup + Foundational → registry + primitives ready.
2. US1 → next-month partitions always exist (deploy/demo — the P1 MVP).
3. US2 → daily rollups populate the durable summary table (deploy/demo).
4. US3 → retention drops expired partitions, verify-before-drop safe (deploy/demo — needs US2).
5. US4 → readers dangling-ref-safe + tolerance check (deploy/demo).
6. Polish → import-boundary/scoping/single-head guards + compose fix + quickstart sign-off.

### Parallel Team Strategy

After Foundational: Dev A on US1 (`apps/scheduler` + `maintenance/partitions.py` create path), Dev B on
US2 (`models/rollups.py` + migration + `maintenance/rollups.py` — disjoint files). US3 then layers onto
both (needs the registry primitives from A and the rollup table from B); US4 hardens the read paths + the
US3 retention task last.

---

## Notes

- **[P]** = different files, no dependency on an incomplete task.
- **No live Docker/Postgres/Redis in this build env**: unit tasks (T006, T012, T021, T029) and the
  offline migration-render test (T022) run green here; the four live tasks (T013, T023, T030, T034) are
  authored as `*_live.py` that SKIP cleanly via the `skipif` probe and execute only against a real
  Postgres. Never fake a live result.
- **Two documented deviations** (plan Complexity Tracking): (1) all three maintenance tasks use the
  BYPASSRLS `get_system_session()` (T009/T018/T026) — app-level `workspace_id` scoping preserved on every
  rollup read/write; unscoped cross-tenant source scans annotated `# noqa: workspace-scope`. (2) the one
  sanctioned bulk `DELETE` (T025 Part B) ages the small, non-partitioned `variant_price_daily_rollups`
  table — SC-003's "0% bulk DELETE" targets the raw append-heavy partitions, which this is not.
- **Non-negotiables** reflected above: partition-drop only / never bulk DELETE on raw tables
  (T024/T025), verify-before-drop hard ordering (T025 `rollups_cover`), idempotent + concurrency-safe
  (T008 `IF NOT EXISTS`, T017 upsert, T024 `IF EXISTS`), TIMESTAMPTZ/UTC everywhere (T007/T014/T017/T025),
  Decimal/finite money (T014/T017), RLS from the first migration (T016), single alembic head
  (T016/T022/T037), scraping-free path (T035).
- **Runtime partition DDL (create/drop) is NOT a migration** (R2) — Alembic is used only once, for the
  new rollup table (T016).
- Commit after each task or logical group; stop at any checkpoint to validate a story independently.
- Do NOT commit as part of this task-generation step.
