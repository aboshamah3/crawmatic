---
description: "Dependency-ordered task list for SPEC-09 Current Prices & Alert Logic"
---

# Tasks: Current Prices & Alert Logic

**Input**: Design documents from `/specs/009-current-prices-alerts/`

**Prerequisites**: plan.md (required), spec.md (required), research.md (D1–D11), data-model.md, contracts/ (6 files), quickstart.md

**Tests**: Included — the spec, plan (Project Structure `tests/unit` + `tests/integration`), and quickstart.md explicitly enumerate the unit and live suites, matching the SPEC-02→08 test pattern. Every DB/Redis/Celery-**independent** behavior is unit-tested **here** (the pure §23 decision tree over every 0%/1%/5% boundary + all six alert types, the severity map, the currency filter, the NaN/Infinity/over-scale rejection, the empty-comparable → NO_COMPETITOR_DATA no-div-by-zero path, the ordered event-transition truth table, the recompute-emission dedup against a fake Redis honoring `SET NX`, model/unique/RLS DDL render via offline `alembic upgrade head --sql`, the workspace-scoping guard, import boundaries, and endpoint request/response with a dependency-overridden session). Live-stack tests (real scrape→recompute→comparison, event-history transitions on real rows, cross-workspace + no-context RLS denial across all three tables, currency-mismatch write-back, real-Redis per-variant-per-job dedup, `alembic upgrade head` partition + RLS creation) are **authored and skip cleanly** where no Postgres/Redis/Celery is reachable — no container engine in this build env (SPEC-02→08 deferred-verification pattern).

**Organization**: Tasks are grouped by user story (US1..US3) to enable independent implementation and testing. Shared blocking work is in Setup + Foundational.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: `[US1]`/`[US2]`/`[US3]` maps a task to a spec.md user story (Setup / Foundational / Integration / Polish carry no story label)
- Every task lists an exact repo-relative file path

## Path Conventions

Backend monorepo (uv workspace). Enums/config/task-names/the pure alert engine/ORM models/repository registration live in `libs/shared/app_shared/`; the scrape-completion trigger in `libs/scrape-core/scrape_core/`; the router + schemas in `apps/api/app/`; the `price_analysis` task + queue wiring in `apps/workers/app/workers/`; the migration at repo-root `alembic/versions/`; tests in `tests/unit/` and `tests/integration/`.

---

## Scope Boundary (read first)

**IN SCOPE — the variant-level analysis layer over the existing SPEC-07 `match_current_prices`:**

- Tables: `variant_price_states` + `variant_alert_states` (current-state, regular, `unique(workspace_id, product_variant_id)`) + `price_alert_events` (append-only, **monthly-partitioned by `created_at` from birth**, composite `PRIMARY KEY (id, created_at)`) — all `WorkspaceScopedBase`, in `WORKSPACE_OWNED_MODELS`, `emit_rls_policy` ENABLE+FORCE fail-closed in the creating migration (§22 shapes, D3/D10).
- Enums (extend `app_shared.enums`): `AlertType`, `AlertSeverity`, `AlertStatus`, `AlertEventType` (D8) — all `StrEnum` → `VARCHAR` via `enum_column`.
- The **pure** alert engine (`app_shared.alerts.engine`, stdlib `decimal` only — no sqlalchemy/celery/fastapi/scrapy/redis): the ordered §23 tree, 4dp `ROUND_HALF_UP` quantization before every compare, the fixed severity map, the currency filter, and the ordered event-transition rule (D1/D2/D5/D6).
- The `price_analysis.recompute_variant` Celery task on its **own `price_analysis` queue** (D4), reading the variant + its comparable `match_current_prices`, running the engine, upserting the three tables (writing an event **only** on a type/severity change), idempotent.
- Recompute wiring from three triggers routed to the one idempotent task, deduplicated per variant per job on the emission side via Redis `SET NX` (D7): (a) scrape completion in `scrape_core/pipelines.py` `_flush_batch`; (b) variant PATCH + bulk-upsert on client price/currency change; (c) match archived/paused.
- Four workspace-scoped, `alerts:read`-gated read endpoints (D9): `GET /v1/variants/{variant_id}/price-comparison`, `GET /v1/alerts/current` (+ `/{variant_id}`), `GET /v1/alert-events`.

**OUT OF SCOPE (do NOT build — later specs):** `variant_price_daily_rollups`, retention/rollup/partition-maintenance jobs (SPEC-15 — this spec creates only the current + next-month event partitions in the migration); `webhook_endpoints`/`webhook_events` + WebhookEvent emission (SPEC-16 — this spec stops at persisting `price_alert_events`); access policies/proxies/request-attempt logic (SPEC-10); rate limiting / in-flight locks (SPEC-11); the scheduler / celery-beat wiring (SPEC-13); the deferred endpoints `GET /v1/products/{id}/price-comparison`, `GET /v1/matches/{id}/current-price`, `GET /v1/observations`, `PATCH /v1/alerts/current/{variant_id}` (alert acknowledge). **No new scope** — `Scope.ALERTS_READ` (`alerts:read`) already exists (D9). Reuse unchanged: `match_current_prices`/`price_observations`/`request_attempts` (SPEC-07), the item pipeline's `match_current_prices` upsert, `app_shared.messaging.enqueue` (SPEC-08 seam), `app_shared.redis_client`, `app_shared.pagination`, `scoped_select`/`scoped_get`, `enum_column`, `emit_rls_policy`, `Money`/`parse_money`, `WorkspaceScopedBase`/`TimestampMixin`, the AST scoping guard, `deps.require_scopes`, the `worker_process_init` fork-safety hook, the `_month_partition_bounds` helper from the SPEC-07 migration.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Enums, config, the task-name constant, and the empty pure-engine package. All DB-independent; every later file imports these. **No new scope is minted — `Scope.ALERTS_READ` (`alerts:read`) already exists (verified in `libs/shared/app_shared/security/scopes.py`), so all four endpoints reuse it (D9).**

- [ ] T001 [P] Extend `libs/shared/app_shared/enums.py` with four `StrEnum` → `VARCHAR` enums (per data-model.md / D8, rendered via `enum_column`): `AlertType` (`NO_COMPETITOR_DATA`/`RISK`/`HIGH_PRICE`/`CHANCE_TO_INCREASE_PRICE`/`NORMAL`/`CLOSE_TO_COMPETITORS`), `AlertSeverity` (`NONE`/`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`), `AlertStatus` (`ACTIVE`/`RESOLVED`), `AlertEventType` (`CREATED`/`UPDATED`/`RESOLVED`/`REOPENED`/`UNCHANGED`). Reuse the existing `ScrapeErrorCode.CURRENCY_MISMATCH` for the currency-mismatch write-back — do NOT add a new error enum. (FR-004)
- [ ] T002 [P] Extend `libs/shared/app_shared/config.py` (`Settings`) with `PRICE_ANALYSIS_DEDUP_TTL_SECONDS: int = 21600` (the `analysis:enqueued:{job}:{variant}` `SET NX` key TTL — comfortably longer than a job's lifetime; env/DB-tunable, not a hardcoded literal — Principle IV). The `price_analysis` queue name is a code constant in `celery_app.py`, not config. (FR-012, D4, D7)
- [ ] T003 [P] Extend `libs/shared/app_shared/task_names.py` with `PRICE_ANALYSIS_RECOMPUTE = "price_analysis.recompute_variant"` (plain string; the module stays celery-free). (FR-012, D4)
- [ ] T004 [P] Create `libs/shared/app_shared/alerts/__init__.py` — empty package init for the framework-agnostic engine module (the engine + re-exports land in US1 T014). (D1)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The three ORM models + registration + the single-head partitioned migration, and the celery `price_analysis` queue/route/include wiring. Plus the DB-independent shape/RLS/offline-migration/scoping tests. **No user story can be implemented until this phase is complete.**

**⚠️ CRITICAL**: Blocks all of Phase 3–5.

- [ ] T005 Create `libs/shared/app_shared/models/alerts.py` — three ORM models per data-model.md / contracts/models-alerts.md. `VariantPriceState` (`class …(Base, WorkspaceScopedBase, TimestampMixin)`, `variant_price_states`): `product_id`/`product_variant_id` (`Uuid`, not null), `client_price` (`Money()`, not null), `currency` (`CHAR(3)`, not null), `cheapest/average/highest_competitor_price` (`Money()`, nullable), `comparable_competitor_count` (`Integer`, not null), `latest_alert_type`/`latest_alert_severity` (`enum_column(..., nullable=False)`), `latest_alert_state_id` (`Uuid`, nullable), `calculated_at` (`TZDateTime()`, not null); `__table_args__` = `UniqueConstraint("workspace_id","product_variant_id", name="uq_variant_price_states_workspace_id_product_variant_id")` + workspace FK. `VariantAlertState` (same bases, `variant_alert_states`): `product_id`/`product_variant_id`, `type`/`severity`/`status` (`enum_column`, not null), `client_price` (`Money()`, not null), `benchmark_price`/`cheapest_competitor_price`/`average_competitor_price` (`Money()`, nullable), `message` (`Text`, not null), `details` (`postgresql.JSONB`, nullable), `first_seen_at`/`last_seen_at` (`TZDateTime()`, not null), `resolved_at` (`TZDateTime()`, nullable); `__table_args__` = `UniqueConstraint("workspace_id","product_variant_id", name="uq_variant_alert_states_workspace_id_product_variant_id")` + workspace FK. `PriceAlertEvent` (`class …(Base, WorkspaceScopedBase)` — NO `TimestampMixin`, `price_alert_events`): `created_at: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True)` (composite `PRIMARY KEY (id, created_at)`, partition key — mirrors `PriceObservation.scraped_at`), `product_id`/`product_variant_id` (`Uuid`, not null; `product_variant_id` indexed), `alert_state_id` (`Uuid`, not null), `event_type` (`enum_column(AlertEventType, nullable=False)`), `previous_type`/`new_type` (`enum_column(AlertType)`, nullable/not-null), `previous_severity`/`new_severity` (`enum_column(AlertSeverity)`, nullable/not-null), `message` (`Text`, not null), `details` (`postgresql.JSONB`, nullable); `__table_args__` = workspace FK + `{"postgresql_partition_by": "RANGE (created_at)"}`. All emitted constraint/index names ≤63 bytes (depends on T001). (FR-001, FR-002, FR-003)
- [ ] T006 Extend `libs/shared/app_shared/models/__init__.py` to import + re-export `VariantPriceState`, `VariantAlertState`, `PriceAlertEvent` (add to `__all__`; `Base.metadata` visibility for Alembic offline render) (depends on T005). (FR-001, FR-002, FR-003)
- [ ] T007 Extend `libs/shared/app_shared/repository.py` to add `VariantPriceState`, `VariantAlertState`, `PriceAlertEvent` to `WORKSPACE_OWNED_MODELS` (so `scoped_select`/`scoped_get` + the AST scoping guard cover them) (depends on T005). (FR-005)
- [ ] T008 Create the Alembic migration `alembic/versions/<rev>_alerts_price_states_tables.py` per contracts/migration-alerts.md — `op.create_table("variant_price_states", ...)` (all columns; Money → `sa.Numeric(18,4)`, enums → `sa.String(32)`, `currency` → `sa.CHAR(3)`, timestamps → `sa.DateTime(timezone=True)`, `details` → `postgresql.JSONB`, ids → `sa.Uuid`; explicit `created_at`/`updated_at`; `PrimaryKeyConstraint("id", name="pk_variant_price_states")`, `UniqueConstraint("workspace_id","product_variant_id", name="uq_variant_price_states_workspace_id_product_variant_id")`, workspace FK, `ix_variant_price_states_workspace_id`), `op.create_table("variant_alert_states", ...)` (same pattern; `unique(workspace_id, product_variant_id)`; workspace FK; `ix_..._workspace_id`; optional `ix_..._workspace_id_type`/`_severity` for the list filters), `op.create_table("price_alert_events", ..., postgresql_partition_by="RANGE (created_at)")` (`PrimaryKeyConstraint("id","created_at", name="pk_price_alert_events")`, workspace FK, `ix_price_alert_events_workspace_id`, `ix_price_alert_events_product_variant_id`) then **current + next month** `CREATE TABLE price_alert_events_{suffix} PARTITION OF …` via `op.execute` using a copied `_month_partition_bounds(now)` helper (verbatim from `2db33dea5e14_observations_current_prices_tables.py`), then `for table in ("variant_price_states","variant_alert_states","price_alert_events"): for stmt in emit_rls_policy(table): op.execute(stmt)`; `downgrade()` drops the two current-state tables, then each `price_alert_events_{suffix}` partition (`DROP TABLE IF EXISTS`) then `op.drop_table("price_alert_events")`; `down_revision = "a6b0234cd4ad"` (current head, SPEC-08 `scrape_jobs_targets`); single head preserved (depends on T005). (FR-001, FR-002, FR-003, FR-005, FR-006)
- [ ] T009 Extend `apps/workers/app/workers/celery_app.py` — add `"price_analysis": {}` to `app.conf.task_queues`, `PRICE_ANALYSIS_RECOMPUTE: {"queue": "price_analysis"}` to `app.conf.task_routes`, and `"app.workers.tasks_analysis"` to the `include=[...]` list. The queue is separate from `scrape_dispatch`/`maintenance` and from the Scrapyd/reactor runtime (Principle V, §26). Do NOT add or duplicate a fork-safety hook — the existing `@worker_process_init.connect → dispose_engine` (SPEC-01) already satisfies the DB-touching-task requirement (depends on T003). (FR-012, D4)

### Foundational tests (DB/Redis-independent)

- [ ] T010 [P] Unit test `tests/unit/test_alerts_models.py` — table/column names + nullability for all three tables; every enum column `enum_column`-renders `VARCHAR` (not DB enum); `variant_price_states`/`variant_alert_states` carry `created_at`+`updated_at` (`TimestampMixin`) and `unique(workspace_id, product_variant_id)`; `price_alert_events` has an explicit `created_at` (no `updated_at`), a composite `PRIMARY KEY (id, created_at)`, `postgresql_partition_by="RANGE (created_at)"`, and an index on `product_variant_id`; Money renders `NUMERIC(18,4)`, `currency` `CHAR(3)`, `details` `JSONB`; all three in `WORKSPACE_OWNED_MODELS` and re-exported from `app_shared.models`; every constraint/index name ≤63 bytes (depends on T005, T006, T007). (FR-001, FR-002, FR-003)
- [ ] T011 [P] Unit test `tests/unit/test_alerts_rls.py` — `emit_rls_policy` render (ENABLE + FORCE + fail-closed policy) for `variant_price_states`, `variant_alert_states`, and the `price_alert_events` parent (whose policy propagates to partitions) (depends on T005). (FR-005, SC-008)
- [ ] T012 [P] Unit test `tests/unit/test_migration_offline_alerts.py` — `alembic upgrade head --sql` (offline, no DB) renders all three `CREATE TABLE`s, the two `unique(workspace_id, product_variant_id)`, the `price_alert_events` `PARTITION BY RANGE (created_at)` parent + current + next-month `PARTITION OF` children + composite PK, and the RLS statements for all three; `alembic heads` yields a single head; `down_revision == a6b0234cd4ad` (depends on T008). (FR-006)
- [ ] T013 [P] Unit test `tests/unit/test_alerts_scoping_guard.py` — the workspace-scoping AST CI guard flags a planted unscoped `select` on `VariantPriceState` / `VariantAlertState` / `PriceAlertEvent` (all in the guarded `WORKSPACE_OWNED_MODELS` set) (depends on T007). (FR-005, SC-008)

**Checkpoint**: Models + migration + RLS + celery `price_analysis` queue wired; DB-independent shape/RLS/offline-migration/scoping tests green. User stories can begin.

---

## Phase 3: User Story 1 - Deterministic variant alert after a scrape (Priority: P1) 🎯 MVP

**Goal**: The pure engine turns a variant's client price + its comparable `match_current_prices` into a deterministic alert type/severity + benchmarks via the ordered §23 tree; the `price_analysis.recompute_variant` task runs it and upserts `variant_price_states` (benchmarks, count, latest type/severity, `calculated_at`) + the current `variant_alert_states` row (type/severity/status + lifecycle timestamps), idempotently; and `GET /v1/variants/{variant_id}/price-comparison` returns the stored position, workspace-scoped.

**Independent Test**: Seed a variant with a client price + several comparable matches with known prices; run `recompute_variant`; assert `variant_price_states` records the correct cheapest/average/highest + comparable count and the alert type matches the §23 tree (RISK above all, NORMAL at exactly 1%/5% below, CHANCE_TO_INCREASE_PRICE beyond 5%, NO_COMPETITOR_DATA when none comparable); a currency-mismatched competitor is flipped `comparable=false`/`CURRENCY_MISMATCH` and excluded; re-running with identical inputs yields byte-identical state (only timestamps advance); `GET …/price-comparison` returns those values and 404s an unknown/cross-workspace/never-analyzed variant.

### Implementation for User Story 1

- [ ] T014 [P] [US1] Create `libs/shared/app_shared/alerts/engine.py` (pure — stdlib `decimal` + `app_shared.enums`, optional `app_shared.money.parse_money`; NO sqlalchemy/celery/fastapi/scrapy/redis) per contracts/alert-engine.md: `QUANT = Decimal("0.0001")`; the `SEVERITY_BY_TYPE` map (FR-004); frozen value objects `CompetitorPrice`, `ComparableSplit`, `AlertOutcome`; `filter_comparable(client_currency, rows) -> ComparableSplit` (included iff `success ∧ comparable ∧ price is not None ∧ currency == client`; `mismatched_match_ids` = rows with a present, differing currency); `discount_vs_average(average, client_price) -> Decimal` (`((avg-price)/avg)*100` then `.quantize(QUANT, ROUND_HALF_UP)`); `decide(client_price, cheapest, average, highest, comparable_count) -> (AlertType, Decimal | None)` (the ordered §23 steps 1–8, all Decimal-vs-Decimal, `>` strict for RISK/HIGH_PRICE, boundaries `> Decimal("5")` / `Decimal("1")<=d<=Decimal("5")` / `Decimal("0")<=d<Decimal("1")`, step 8 defensive → HIGH_PRICE); `severity_for(type) -> AlertSeverity` (map only); `analyze(client_price, client_currency, competitor_rows) -> AlertOutcome` (benchmarks min/mean/max of included, `None`×3 when count 0, `benchmark_price` = highest for RISK / cheapest for HIGH_PRICE / average for the discount types / `None` for NO_COMPETITOR_DATA, deterministic time-free message/details); `transition(prev_type, prev_severity, new_type, new_severity, *, had_history) -> AlertEventType | None` (the ordered D5 rule). Reject NaN/Infinity/over-scale at the boundary (reuse `parse_money` semantics). Update `alerts/__init__.py` to re-export the engine API + constants (depends on T001, T004). (FR-007, FR-008, FR-009, FR-010, FR-011, SC-001)
- [ ] T015 [P] [US1] Unit test `tests/unit/test_alert_engine.py` — EXHAUSTIVE per contracts/alert-engine.md "Determinism guarantees": every §23 branch 1–8 (step 8 via a constructed degenerate input); the boundary table (`0.0000` → CLOSE_TO_COMPETITORS, `0<..<1` → CLOSE_TO_COMPETITORS, `1.0000` → NORMAL, `1<..<5` → NORMAL, `5.0000` → NORMAL, `>5` → CHANCE_TO_INCREASE_PRICE); a half-up case whose 5th decimal is 5 rounds up before compare; RISK (`> highest`) and HIGH_PRICE (`> cheapest`, `==` falls through); `filter_comparable` currency filtering (mismatched ids surfaced, non-success/None-price simply excluded not flagged); empty comparable set → NO_COMPETITOR_DATA with `None` benchmarks + count 0 and no divide-by-zero; NaN/Infinity/over-scale client or competitor price rejected (raises); the severity map parametrized total over all six types; `analyze` byte-identical across two identical-input runs (depends on T014). (FR-007, FR-008, FR-009, FR-010, FR-011, SC-001, SC-006)
- [ ] T016 [P] [US1] Unit test `tests/unit/test_alert_transitions.py` — `transition` truth table (all six cases + both `None` cases per D5): `prev None → NORMAL` ⇒ `None`; `prev None → non-NORMAL` ⇒ CREATED; `prev == new` ⇒ `None` (UNCHANGED, not persisted); `non-NORMAL → NORMAL` ⇒ RESOLVED; `NORMAL/resolved → non-NORMAL` with `had_history` ⇒ REOPENED; `non-NORMAL → different non-NORMAL` (type change or same-type severity change) ⇒ UPDATED (depends on T014). (FR-013, SC-004)
- [ ] T017 [US1] Create `apps/workers/app/workers/tasks_analysis.py` — `@app.task(name=PRICE_ANALYSIS_RECOMPUTE) def recompute_variant(*, workspace_id, product_variant_id, product_id=None, scrape_job_id=None) -> None` per contracts/price-analysis-task.md: within one `get_session()` transaction after `set_workspace_context(session, workspace_id)` — (1) `scoped_get(ProductVariant, variant_id, ws)` (missing → no-op return), read `current_price`/`currency`; (2) `scoped_select(MatchCurrentPrice, ws).where(product_variant_id == variant_id)` → `list[CompetitorPrice]`; (3) `outcome = engine.analyze(...)`; (4) currency-mismatch write-back: scoped `UPDATE match_current_prices SET comparable=false, error_code='CURRENCY_MISMATCH'` for each `outcome.mismatched_match_ids` (only flips currently-comparable rows; idempotent); (5) upsert `variant_price_states` via `pg_insert(...).on_conflict_do_update(index_elements=["workspace_id","product_variant_id"], set_={benchmarks, comparable_competitor_count, latest_alert_type, latest_alert_severity, calculated_at=now, updated_at=now})`; (6) `scoped_get` prior `VariantAlertState` → `prev_type,prev_severity,had_history`; (7) `ev = engine.transition(...)`; (8) upsert `variant_alert_states` (status ACTIVE if non-NORMAL else RESOLVED; `first_seen_at`=now on CREATED/REOPENED else keep; `last_seen_at`=now always; `resolved_at`=now on RESOLVED, NULL on REOPENED, else unchanged; write type/severity/client_price/benchmark_price/cheapest+average/message/details/updated_at). **The `price_alert_events` insert (step 9) + `latest_alert_state_id` linkage are added in US2 (T023).** `session.commit()`. SQLAlchemy + engine only; reuses the `worker_process_init` dispose hook (depends on T009, T014, T005). (FR-012, FR-013, FR-014, SC-002, SC-006)
- [ ] T018 [US1] Create `apps/api/app/schemas/alerts.py` (Pydantic v2) — `PriceComparisonResponse { product_variant_id, client_price, currency, cheapest_competitor_price, average_competitor_price, highest_competitor_price, comparable_competitor_count, alert_type, alert_severity, calculated_at }` (Money serialized as decimal strings, nullable benchmarks → `null`). (The alert/event response models are added in US2.) Per contracts/api-alerts.md (depends on T001). (FR-017)
- [ ] T019 [US1] Extend `apps/api/app/routers/variants.py` with `GET /v1/variants/{variant_id}/price-comparison` (require `alerts:read`; distinguish 404 unknown/cross-workspace vs never-analyzed via the unscoped-lookup-then-`scoped_get(VariantPriceState by unique(ws, variant))` pattern → **404** `NOT_FOUND` when no price state exists yet or the variant is unknown/cross-ws; **200** `PriceComparisonResponse` from the stored `variant_price_states` row). Never imports `apps/workers` (depends on T018, T005). (FR-017, FR-020, SC-005)
- [ ] T020 [US1] Confirm the `variants` router carrying the new `price-comparison` route is included under `/v1` in `apps/api/app/main.py` (it already is — this task verifies the route is reachable and adds no duplicate mount; the new `alerts` router is mounted in US2 T026) (depends on T019). (FR-017)

### Tests for User Story 1

- [ ] T021 [US1] Unit test `tests/unit/test_price_analysis_task.py` — `recompute_variant` (fake session + fake `MatchCurrentPrice` rows): `set_workspace_context` invoked before any query; a missing variant → no-op; a set of comparable competitors → `variant_price_states` gets correct cheapest/average/highest + comparable count + `latest_alert_type`/`severity` matching the engine, and `variant_alert_states` gets the type/severity/status + lifecycle timestamps; a currency-mismatched competitor is excluded from benchmarks and its `match_current_prices` row flipped `comparable=false`/`CURRENCY_MISMATCH`; re-running with unchanged inputs writes identical state (only `calculated_at`/`updated_at` advance). (Event-write cases added in US2.) (depends on T017). (FR-012, FR-013, FR-014, SC-002, SC-006, SC-007)
- [ ] T022 [US1] Unit test `tests/unit/test_alerts_router.py` (dependency-overridden session) — `GET /v1/variants/{id}/price-comparison` → 200 `PriceComparisonResponse` shape from a seeded `variant_price_states`; unknown/cross-workspace variant → 404; a variant with no price state yet → 404 ("no comparison computed yet"); the route declares `require_scopes("alerts:read")` and a missing scope → 403. (alerts/current + alert-events cases added in US2.) (depends on T019, T020). (FR-017, FR-020, SC-005, SC-008)

**Checkpoint**: The scrape→analysis→state→comparison core works at fixture scale (pure engine + task state-upsert + comparison endpoint, all against fakes). US1 is independently testable. MVP demoable.

---

## Phase 4: User Story 2 - Alert history when the signal changes (Priority: P2)

**Goal**: When a variant's alert type or severity changes between analyses the task records exactly one `price_alert_events` row (CREATED/UPDATED/RESOLVED/REOPENED) and links `variant_price_states.latest_alert_state_id`; an unchanged run writes no event and only advances `last_seen_at`. The operator can page the alert-event history and the current-alert list, workspace-scoped and filterable.

**Independent Test**: Drive a variant NORMAL → HIGH_PRICE → NORMAL → HIGH_PRICE through repeated `recompute_variant` calls and assert exactly one CREATED, one RESOLVED, one REOPENED, and zero events on an unchanged re-run (only `last_seen_at` advances); then `GET /v1/alerts/current` pages + filters by type/severity, `GET /v1/alerts/current/{variant_id}` returns one, and `GET /v1/alert-events` pages + filters by variant — all workspace-scoped.

### Implementation for User Story 2

- [ ] T023 [US2] Extend `apps/workers/app/workers/tasks_analysis.py` with the event-write path (contracts/price-analysis-task.md step 9): when `ev is not None` insert one `price_alert_events` row (`event_type=ev`, `previous_type`/`new_type`, `previous_severity`/`new_severity`, `message`, `details`, `created_at=now`, `alert_state_id`=the upserted alert-state id); when `ev is None` (no-event / UNCHANGED) write **no** event; set `variant_price_states.latest_alert_state_id` to the alert-state id in the same transaction. Idempotent — an unchanged re-run yields `ev is None` ⇒ zero duplicate events (depends on T017). (FR-013, FR-014, SC-004)
- [ ] T024 [US2] Extend `apps/api/app/schemas/alerts.py` with `AlertStateResponse { product_variant_id, type, severity, status, client_price, benchmark_price, cheapest_competitor_price, average_competitor_price, message, details, first_seen_at, last_seen_at, resolved_at }`, `AlertStateListResponse { items, next_cursor }`, `AlertEventResponse { id, product_variant_id, alert_state_id, event_type, previous_type, new_type, previous_severity, new_severity, message, details, created_at }`, `AlertEventListResponse { items, next_cursor }`. Per contracts/api-alerts.md (depends on T018). (FR-018, FR-019)
- [ ] T025 [US2] Create `apps/api/app/routers/alerts.py` (per contracts/api-alerts.md; on the `get_current_principal` auth seam with RLS already set on the yielded session) — `GET /v1/alerts/current` (require `alerts:read`; `scoped_select(VariantAlertState, ws)` + optional `type`/`severity` `WHERE` + `keyset_predicate` on cursor, `.order_by(created_at, id).limit(limit+1)`, `paginate(...)` with `clamp_limit` default 50/max 500 → `AlertStateListResponse`), `GET /v1/alerts/current/{variant_id}` (require `alerts:read`; `scoped_get(VariantAlertState by unique(ws, variant))` → 404 if none → `AlertStateResponse`), `GET /v1/alert-events` (require `alerts:read`; `scoped_select(PriceAlertEvent, ws)` + optional `product_variant_id == variant_id` + `keyset_predicate`, `.order_by(created_at, id).limit(limit+1)`, `paginate(...)` → `AlertEventListResponse`). Invalid `type`/`severity`/cursor → 422. Reuses `app_shared.pagination`; never imports `apps/workers` (depends on T024, T005). (FR-018, FR-019, FR-020)
- [ ] T026 [US2] Register the new `alerts` router in `apps/api/app/main.py` under `/v1` (depends on T025). (FR-018, FR-019)

### Tests for User Story 2

- [ ] T027 [US2] Extend `tests/unit/test_price_analysis_task.py` — event-write cases: NORMAL → HIGH_PRICE → NORMAL → HIGH_PRICE yields exactly one CREATED, one RESOLVED, one REOPENED (correct `previous_*`/`new_*`), a same-type severity change yields UPDATED, and an unchanged re-run writes **zero** events while advancing `last_seen_at`; `latest_alert_state_id` links the alert-state row (depends on T021, T023). (FR-013, FR-014, SC-004)
- [ ] T028 [US2] Extend `tests/unit/test_alerts_router.py` — `GET /v1/alerts/current` → 200 list shape, filters by `type` and `severity`, paginates deterministically over `(created_at, id)` with `next_cursor`, malformed cursor → 422; `GET /v1/alerts/current/{variant_id}` → 200 one / 404 none; `GET /v1/alert-events` → 200 list, filters by `variant_id`, paginates; every route declares `require_scopes("alerts:read")` and a missing scope → 403 (depends on T022, T025, T026). (FR-018, FR-019, FR-020, SC-008)

**Checkpoint**: The task records history exactly on a type/severity change (never on an unchanged run), and the current-alert + alert-events endpoints page/filter workspace-scoped. US2 is independently testable.

---

## Phase 5: User Story 3 - Client price change reflected immediately + per-variant-per-job dedup (Priority: P3)

**Goal**: All three FR-015 triggers route to the one idempotent `PRICE_ANALYSIS_RECOMPUTE` task **by name** (no caller imports `apps/workers`): (a) scrape completion — emitted post-commit from the pipeline, **deduplicated per variant per job** via Redis `SET NX` so many match completions of one variant in one job collapse to a single recompute; (b) a variant's client price/currency change via PATCH or bulk-upsert — reflected immediately, no scrape; (c) a match archived/paused — the comparable set changed.

**Independent Test**: Simulate N match completions of one variant in one job (fake Redis honoring `SET NX`) → exactly one enqueue for that variant/job; PATCH a variant's `price`/`currency` → one enqueue, PATCH only `title` → none; archive a match → one enqueue for its variant.

### Implementation for User Story 3

- [ ] T029 [US3] Extend `libs/scrape-core/scrape_core/pipelines.py` `_flush_batch` per contracts/recompute-triggers.md trigger (a): **after** the persistence transaction commits (the same off-reactor continuation that already enqueues `SCRAPE_FINALIZE_JOBS`), iterate the batch's distinct `(workspace_id, scrape_job_id, product_variant_id)`; for items with a non-null `scrape_job_id`, claim `analysis:enqueued:{scrape_job_id}:{product_variant_id}` via `get_redis_client().set(key, "1", nx=True, ex=settings.PRICE_ANALYSIS_DEDUP_TTL_SECONDS)` and skip on a loss; then `enqueue(PRICE_ANALYSIS_RECOMPUTE, queue="price_analysis", kwargs={workspace_id, product_variant_id, product_id, scrape_job_id})` (ad-hoc items with `scrape_job_id is None` enqueue directly, no dedup key). Keep `scrape-core` import-clean (`app_shared.messaging.enqueue` + `app_shared.redis_client` only; no fastapi/apps.workers; never on the reactor thread mid-transaction) (depends on T002, T003). (FR-012, FR-015, SC-002, SC-007)
- [ ] T030 [US3] Extend `apps/api/app/routers/variants.py` per contracts/recompute-triggers.md trigger (b): in `update_variant` (PATCH), after a successful `session.flush()`, if `"current_price" in updates or "currency" in updates` enqueue `PRICE_ANALYSIS_RECOMPUTE` (`queue="price_analysis"`, `scrape_job_id=None`, `product_id=variant.product_id`) via `app_shared.messaging.enqueue`; in `bulk_upsert_variants`, after the upsert flush, enqueue once per variant whose `current_price`/`currency` was inserted or changed. Reflected immediately, no scrape; API MUST NOT import `apps/workers` (depends on T003, T017). (FR-015, FR-016, SC-003)
- [ ] T031 [US3] Extend the match status-change path in `apps/api/app/routers/matches.py` per contracts/recompute-triggers.md trigger (c): where a `CompetitorProductMatch` transitions to an archived/paused (non-active) status — `update_match` (PATCH changing `status`) and `delete_match` (archive) — after the change is flushed, enqueue `PRICE_ANALYSIS_RECOMPUTE` (`queue="price_analysis"`, `scrape_job_id=None`) for that match's `product_variant_id` via `app_shared.messaging.enqueue`. Same by-name seam; no `apps/workers` import (depends on T003, T017). (FR-015)

### Tests for User Story 3

- [ ] T032 [P] [US3] Unit test `tests/unit/test_recompute_triggers_pipeline.py` — trigger (a) over a fake batch + fake Redis honoring `SET NX` + fake `enqueue`: N completed matches of one variant in one job ⇒ exactly **one** `PRICE_ANALYSIS_RECOMPUTE` enqueued for that variant/job (SC-007); distinct variants each enqueue once; an item with `scrape_job_id is None` enqueues directly (no dedup key); emission happens after commit, never on the reactor thread (depends on T029). (FR-012, FR-015, SC-007)
- [ ] T033 [P] [US3] Unit test `tests/unit/test_recompute_triggers_api.py` (dependency-overridden session + fake `enqueue`) — trigger (b): a PATCH changing `price`/`currency` enqueues one recompute (`scrape_job_id=None`, correct kwargs), a PATCH changing only `title` enqueues none, bulk-upsert enqueues once per changed variant; trigger (c): archiving/pausing a match enqueues one recompute for its variant; no path imports `apps/workers` (depends on T030, T031). (FR-015, FR-016, SC-003)

**Checkpoint**: Client price/currency changes recompute immediately, match archive/pause recomputes, and many completions of one variant in one job collapse to a single recompute. US3 is independently testable.

---

## Phase 6: Integration (live-stack, authored + skip-marked) — ⏸ deferred live verification

**Purpose**: End-to-end scenarios against a real Postgres/Redis/Celery stack. Authored now and **skip cleanly** where those services are unreachable (no container engine in this build env — SPEC-02→08 deferred-verification pattern). Each is deferred live verification. Zero real-competitor network calls; fixtures/loopback only.

- [ ] T034 [P] ⏸ DEFERRED (needs live Postgres/Redis) Author `tests/integration/test_price_analysis_recompute_live.py` (US1/SC-002/SC-005) — seed ws/product/variant + several comparable `match_current_prices`; run `recompute_variant`; assert `variant_price_states` benchmarks/count/type + `variant_alert_states` match the engine; `GET /v1/variants/{id}/price-comparison` returns those values; re-run ⇒ identical state, only timestamps advance.
- [ ] T035 [P] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_alert_events_history_live.py` (US2/SC-004) — drive NORMAL → HIGH_PRICE → NORMAL → HIGH_PRICE ⇒ exactly one CREATED, one RESOLVED, one REOPENED in `price_alert_events`; an unchanged re-run writes zero events + advances `last_seen_at`; `GET /v1/alert-events` pages + filters by variant, `GET /v1/alerts/current` filters by type/severity.
- [ ] T036 [P] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_alerts_isolation_live.py` (Isolation/SC-008) — cross-workspace read blocked (app scoping + RLS) on `variant_price_states`/`variant_alert_states`/`price_alert_events`; no workspace context ⇒ 0 rows from each; missing `alerts:read` scope ⇒ 403.
- [ ] T037 [P] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_currency_mismatch_live.py` (US3/SC-006) — a competitor `match_current_prices` in a non-matching currency is excluded from benchmarks, flipped `comparable=false`, and stamped `CURRENCY_MISMATCH`; the alert reflects only the matching-currency competitors; no FX.
- [ ] T038 [P] ⏸ DEFERRED (needs live Redis) Author `tests/integration/test_recompute_dedup_live.py` (US3/SC-007) — with a real Redis `SET NX`, N match completions of one variant in one job ⇒ exactly one `PRICE_ANALYSIS_RECOMPUTE` enqueued for that variant/job; a fresh job re-enqueues.
- [ ] T039 [P] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_migration_alerts_live.py` (FR-006) — `alembic upgrade head` creates all three tables + the current+next-month `price_alert_events` partitions; RLS present + FORCED on all three; `downgrade` is clean; `alembic heads` shows one head.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T040 [P] Extend `tests/unit/test_import_boundaries.py` — assert `app_shared.alerts.engine` (and the `alerts` package) import **no** sqlalchemy/celery/fastapi/scrapy/redis (only stdlib `decimal` + `app_shared.enums` + optional `app_shared.money`); `app_shared.models.alerts` imports no scrapy/twisted/fastapi; the API routers `apps/api/app/routers/alerts.py` + the SPEC-09 additions to `routers/variants.py`/`routers/matches.py` import `app_shared.messaging`, never `apps.workers`; `scrape_core.pipelines` (trigger a) imports `app_shared.messaging`/`app_shared.redis_client`, never fastapi/apps.workers (depends on T014, T005, T025, T029, T030, T031). (Principle I, D1)
- [ ] T041 Run the DB/Redis-independent validation from `specs/009-current-prices-alerts/quickstart.md`: `uv run pytest tests/unit -q` green (engine boundary/transition suites pass with zero infra) + `SPECIFY_FEATURE_DIRECTORY=specs/009-current-prices-alerts uv run alembic upgrade head --sql` renders all three tables + `unique(workspace_id, product_variant_id)` on both current-state tables + the `price_alert_events` `PARTITION BY RANGE (created_at)` parent + current/next-month partitions + composite PK + RLS on all three (single head, `down_revision == a6b0234cd4ad`) + `uv run python scripts/check_workspace_scoping.py` exit 0 + `bash scripts/check_single_head.sh` single head + import-boundary green. (If deps missing, `uv sync --all-packages` first — never plain `uv sync`.)
- [ ] T042 [P] Confirm `apps/workers/app/workers/celery_app.py` registers the `price_analysis` queue + route (`PRICE_ANALYSIS_RECOMPUTE → price_analysis`) and includes `app.workers.tasks_analysis` so `recompute_variant` is discoverable by name (deployment sanity; no live worker run required here) (depends on T009, T017). (FR-012)

---

## FR / SC Coverage

| Requirement | Task(s) |
|-------------|---------|
| FR-001 `variant_price_states` (§22 shape) | T005, T006, T008, T010, T012, T034 |
| FR-002 `variant_alert_states` (§22 shape) | T005, T006, T008, T010, T012, T034 |
| FR-003 `price_alert_events` partitioned monthly | T005, T006, T008, T010, T012, T039 |
| FR-004 alert type/severity/status/event enums + severity map | T001, T014, T015 |
| FR-005 workspace isolation (app scoping + RLS) | T007, T008, T011, T013, T036 |
| FR-006 single-head forward migration + partitions | T008, T012, T039, T041 |
| FR-007 ordered §23 decision tree | T014, T015 |
| FR-008 Decimal quantize 4dp ROUND_HALF_UP, NaN/Inf rejected | T014, T015 |
| FR-009 exact 0%/1%/5% boundary behavior | T014, T015 |
| FR-010 currency filter (comparable set) | T014, T015, T017, T021, T037 |
| FR-011 severity solely from the map | T014, T015 |
| FR-012 `price_analysis` task, own queue, idempotent, dedup/job | T009, T017, T021, T029, T032, T042 |
| FR-013 upserts 3 tables, event only on change, transition rule | T014, T016, T017, T023, T027 |
| FR-014 idempotent (identical state, no duplicate events) | T017, T021, T023, T027, T034 |
| FR-015 three recompute triggers → one task | T029, T030, T031, T032, T033 |
| FR-016 client price/currency change reflected immediately | T030, T033 |
| FR-017 `GET …/price-comparison` | T018, T019, T020, T022, T034 |
| FR-018 `GET /v1/alerts/current` (+`/{variant_id}`) | T024, T025, T026, T028 |
| FR-019 `GET /v1/alert-events` | T024, T025, T026, T028 |
| FR-020 read scope + workspace scoping/RLS on every read | T019, T025, T022, T028, T036 |
| SC-001 deterministic type incl. exact boundaries | T014, T015 |
| SC-002 scrape completion triggers recompute | T017, T029, T032, T034 |
| SC-003 price/currency change reflected without scrape | T030, T033 |
| SC-004 event written exactly on change, not unchanged | T016, T023, T027, T035 |
| SC-005 comparison endpoint consistent with stored state | T019, T022, T034 |
| SC-006 currency mismatch never affects benchmarks | T015, T017, T021, T037 |
| SC-007 many completions → single recompute per variant/job | T029, T032, T038 |
| SC-008 no cross-workspace observe; no-context 0 rows | T011, T013, T022, T028, T036 |

Every FR-001..FR-020 and SC-001..SC-008 maps to ≥1 task.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies (T001–T004 all `[P]`, different files).
- **Foundational (Phase 2)**: depends on Setup (T001 enums → T005 models; T003 task-names → T009 celery). **Blocks all user stories.** T006/T007/T008 depend on T005; T009 depends on T003; tests T010–T013 depend on their targets.
- **US1 (Phase 3)**: depends on Foundational. Pure `engine.py` (T014) `[P]` after enums; its tests T015/T016 `[P]` after T014; the task (T017) needs engine+models+celery; schemas (T018) then the `price-comparison` route (T019) then `main.py` verify (T020); tests T021/T022 after their targets.
- **US2 (Phase 4)**: depends on US1 — extends `tasks_analysis.py` (T023 after T017), `schemas/alerts.py` (T024 after T018), adds `routers/alerts.py` (T025) + `main.py` mount (T026), and extends the US1 task/router test files (T027/T028).
- **US3 (Phase 5)**: depends on Foundational + the task existing (T017). The three trigger wirings (T029 pipeline, T030 variants, T031 matches) are independent files; tests T032/T033 `[P]`.
- **Integration (Phase 6)**: depends on the corresponding user-story implementations; authored anytime, skip-marked. ⏸ deferred live verification.
- **Polish (Phase 7)**: after the desired stories (T040 import-boundary needs the engine + models + routers + pipeline trigger; T041 runs the full unit suite + offline migration; T042 needs the task + celery wiring).

### Story-level & shared-file notes

- **US1**: independent after Foundational. Creates `alerts/engine.py`, `tasks_analysis.py`, `schemas/alerts.py`; extends `routers/variants.py` (the `price-comparison` route). The engine is the acceptance core.
- **US2**: **extends US1's `tasks_analysis.py` (T023 after T017) and `schemas/alerts.py` (T024 after T018)**, and the US1 task/router **test files** (T027/T028) — sequential edits to shared files; the new `routers/alerts.py` + `main.py` mount are independent.
- **US3**: three independent trigger edits (`scrape_core/pipelines.py`, `routers/variants.py`, `routers/matches.py`), all after the task (T017) exists so the enqueue kwargs are settled.
- Deferred (⏸) integration tasks are authored anytime but only pass on a Postgres/Redis/Celery host.

### Within a story

- The pure `engine.py` + its unit tests before the `tasks_analysis.py` task and the router that surface its output.
- `schemas/*.py` before/with the router that imports them; router before its `main.py` registration.
- Task module before its celery route/discovery confirmation (T042).

---

## Parallel Opportunities

- **Setup**: T001–T004 all `[P]` (different files).
- **Foundational**: tests T010–T013 `[P]` once their targets land.
- **US1**: T014 `[P]`; engine tests T015, T016 `[P]`.
- **US2**: T027/T028 extend shared test files; the router/schema edits are sequential on their files.
- **US3**: T032, T033 `[P]`; the three trigger wirings touch different files and can proceed in parallel once T017 lands.
- **Integration**: T034–T039 all `[P]` (distinct files).
- **Cross-story**: with Foundational done, US1's engine + task can be built while US3's trigger wirings are drafted against the settled task signature; US2 slots in once US1's `tasks_analysis.py`/`schemas/alerts.py` land.

### Parallel Example: US1 pure engine + its corpora

```bash
# The pure engine and its exhaustive boundary/transition suites (different files):
Task: "Create libs/shared/app_shared/alerts/engine.py"
Task: "Unit test tests/unit/test_alert_engine.py"
Task: "Unit test tests/unit/test_alert_transitions.py"
```

### Parallel Example: Foundational unit tests

```bash
# After T005/T007/T008 land, run the DB-independent shape/RLS/offline/scoping tests together:
Task: "Unit test tests/unit/test_alerts_models.py"
Task: "Unit test tests/unit/test_alerts_rls.py"
Task: "Unit test tests/unit/test_migration_offline_alerts.py"
Task: "Unit test tests/unit/test_alerts_scoping_guard.py"
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1 Setup → Phase 2 Foundational (models + partitioned migration + RLS + celery queue; all shape/RLS/offline/scoping tests green).
2. Phase 3 US1 → the pure engine (exhaustive boundaries/severity/currency/transition) + `recompute_variant` state-upsert + `price-comparison` endpoint against fakes.
3. **STOP & VALIDATE**: run the unit suite (engine determinism passes with zero infra); author the deferred live recompute/comparison test.

### Incremental Delivery

1. Setup + Foundational → foundation ready.
2. US1 (P1, MVP) → deterministic engine + state upsert + comparison endpoint.
3. US2 (P2) → event history (write-on-change) + current-alert/alert-events endpoints.
4. US3 (P3) → three recompute triggers + per-variant-per-job dedup.
5. Integration (⏸ skip-marked) + Polish (import boundaries, quickstart validation, celery discovery sanity).

### Deferred (live-Postgres/Redis/Celery) tasks

T034, T035, T036, T037, T038, T039 — authored here, left unchecked `- [ ]`, marked ⏸ DEFERRED (needs live infra). They cover the live halves of SC-001..SC-008: real scrape→recompute→comparison, event-history transitions on real rows, cross-workspace + no-context RLS denial across all three tables, currency-mismatch write-back, real-Redis per-variant-per-job dedup, and `alembic upgrade head` partition + RLS creation. No test contacts a real competitor domain.

---

## Notes

- `[P]` = different files, no dependency on an incomplete task.
- `[Story]` label maps a task to a user story for traceability; Setup / Foundational / Integration / Polish carry none.
- `app_shared.alerts.engine` is **pure** (stdlib `decimal` + `app_shared.enums` + optional `app_shared.money`) — T040 guards this; the Celery task merely orchestrates it (mirrors SPEC-08's pure `app_shared/jobs/*`). The API routers import `app_shared.messaging`, never `apps/workers` (Constitution I).
- Severity is **derived from type** via the fixed FR-004 map (`SEVERITY_BY_TYPE`) — no independent severity logic (FR-011).
- `discount_vs_average` is quantized 4dp `ROUND_HALF_UP` **before** any boundary compare; all boundary compares are `Decimal` vs `Decimal`; NaN/Infinity/over-scale rejected at the boundary (no float ever touches a compare — SC-001).
- A `price_alert_events` row is written **only** on a type/severity change (CREATED/UPDATED/RESOLVED/REOPENED); UNCHANGED is defined but never persisted (avoids history spam + hot-row contention — §26).
- Dedup per variant per job is an **emission-side** Redis `SET NX` (`analysis:enqueued:{job}:{variant}`) — a contention reducer, not a correctness guard; the task is fully idempotent so at-least-once delivery is always safe (D4).
- `price_alert_events` mirrors the SPEC-07 `price_observations` partition pattern verbatim (`_month_partition_bounds`, `PARTITION BY RANGE (created_at)`, composite PK); retention/future-partition maintenance is SPEC-15 (out of scope).
- `created_at` on the two current-state tables is a deliberate, precedent-backed benign superset of the §22 shape (matches `MatchCurrentPrice`), giving the shared cursor a stable `(created_at, id)` key (D10).
- No new scope minted — `alerts:read` already exists (D9); the migration chains onto the single head `a6b0234cd4ad` and preserves a single head.
- Live-stack tests (Phase 6) are authored and skip cleanly — no Postgres/Redis/Celery in this build env.
- Do NOT commit — the orchestrator commits after this step.
