---
description: "Task list for SPEC-12 — Domain Strategy Optimizer"
---

# Tasks: Domain Strategy Optimizer

**Input**: Design documents from `/specs/012-domain-strategy-optimizer/`

**Prerequisites**: plan.md (required), spec.md (user stories US1–US5 / FR-001..FR-028), research.md (D1–D11), data-model.md, contracts/ (stats-buffer, promotion, rediscovery, discovery, consumption, rls-and-migration, api-and-observability)

**Tests**: INCLUDED. quickstart.md names the exact suites and every user story has an explicit Independent Test, so test tasks are authored. Per project memory (no Docker daemon in this build env), pure logic (URL-pattern reuse, promotion/rediscovery/consumption evaluators, Redis buffer record/drain/read math against a fake/real Redis, RLS-DDL string rendering, enum validation, single-head) runs as **unit tests that actually pass**; anything needing a live Postgres/Redis/Scrapyd is authored as an **integration test that SKIPS cleanly** when the service is absent (SPEC-05..11 convention).

**Organization**: Grouped by user story (US1 P1 → US5 P5) for independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: `[US1]`..`[US5]` — the user story this task serves (Setup/Foundational/Polish carry no story tag)
- Every task carries an exact repo-relative file path

## Path & build conventions

- **uv workspace**: dependency sync is `uv sync --all-packages` (plain `uv sync` wipes workspace member deps — project rule); tests run with `uv run pytest`.
- **Pure learning logic** → new package `libs/shared/app_shared/strategy/` (SQLAlchemy / injected `redis.Redis` client / stdlib; **no** Scrapy/Twisted/FastAPI/`apps.*` import — the sibling of `app_shared/access/`). ORM models → `libs/shared/app_shared/models/strategy.py`; new RLS emitter → `libs/shared/app_shared/models/rls.py`.
- **Reactor-adjacent recording** → extend the already-off-reactor `libs/scrape-core/scrape_core/pipelines.py::_flush_batch` (no new reactor hop); **group-resolution seam** → `apps/scrapers/price_monitor/spiders/generic_price_spider.py::load_targets`.
- **Orchestration (Celery)** → `apps/workers/app/workers/tasks_strategy.py` (new) + job-finalization flush in `apps/workers/app/workers/tasks_jobs.py`; periodic entries → `apps/scheduler/app/scheduler/scheduler_app.py`.
- **Operator API** → `apps/api/app/routers/strategy.py`, `schemas/strategy.py`, `services/strategy.py` (new, `/v1`).
- **Migration** → `alembic/versions/<rev>_domain_strategy_optimizer_tables.py`, `down_revision = "851220acab90"` (current single head).
- Tests live at repo root: `tests/unit/`, `tests/integration/`.
- **Reuse, do not rebuild**: `app_shared.url_pattern.derive_url_pattern` + `URL_PATTERN_ALGORITHM_VERSION`; existing `AccessMethod`/`ExtractionMethod` enums (as `method_name` values, D1); the SPEC-07 spider attempt path + off-reactor batched `_flush_batch`; the SPEC-10 Redis atomic-counter pattern (`app_shared/access/budget.py` shape); the models `Base`/`WorkspaceScopedBase` + RLS helpers; `app_shared.messaging.enqueue`. **Do NOT** create tasks that rebuild fetch/extract.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Sync the workspace and scaffold the new pure-logic package so all later phases have a home.

- [X] T001 Sync workspace dependencies from repo root: `uv sync --all-packages` (NEVER plain `uv sync` — it wipes workspace member deps). No new third-party dependency is added; confirm `redis`, `sqlalchemy`, `alembic`, `celery`, `scrapy`, `twisted`, `fastapi` resolve.
- [X] T002 [P] Create the new pure-logic package `libs/shared/app_shared/strategy/__init__.py` (empty package marker; public re-exports are added in T042 as modules land).

**Checkpoint**: `uv run pytest -q` collects cleanly; the new package is importable.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Enums, tuning knobs, task-name constants, the three ORM models, the new transitive-RLS emitter, the single Alembic migration, and the registry/repository wiring that every user story depends on.

**⚠️ CRITICAL**: No user-story work can begin until this phase is complete.

- [X] T003 Add three app-validated `StrEnum`s (VARCHAR via `enum_column`, no DB enum — D2) to `libs/shared/app_shared/enums.py`: `StrategyStatus` (`DISCOVERY_REQUIRED`, `LEARNING`, `ACTIVE`, `DEGRADED`, `DISABLED`), `MethodType` (`ACCESS`, `EXTRACTION`), `DiscoveryRunStatus` (`PENDING`, `RUNNING`, `COMPLETED`, `NO_WINNER`, `FAILED`). Do NOT redeclare `AccessMethod`/`ExtractionMethod` — they are reused as `method_name` values (data-model §1, FR-007/FR-008).
- [X] T004 [P] Add the 10 env-tunable `Settings` knobs to `libs/shared/app_shared/config.py` with defaults from data-model §7 / research D11: `STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD=0.85`, `STRATEGY_PROMOTION_MIN_SUCCESSES=3`, `STRATEGY_PROMOTION_MIN_DISTINCT_URLS=3`, `STRATEGY_REDISCOVERY_SUCCESS_RATE_FLOOR=0.80`, `STRATEGY_REDISCOVERY_LOW_CONFIDENCE=0.75`, `STRATEGY_REDISCOVERY_CONSECUTIVE_FAILURES=3`, `STRATEGY_DISCOVERY_MIN_SAMPLE=3`, `STRATEGY_DISCOVERY_MAX_SAMPLE=10`, `STRATEGY_STATS_FLUSH_INTERVAL_SECONDS=60`, `STRATEGY_STATS_KEY_TTL_SECONDS=3600` (FR-010/FR-019/FR-020, Constitution IV — no hardcoded constants).
- [X] T005 [P] Add the four Celery task-name constants to `libs/shared/app_shared/task_names.py` (data-model §8): `STRATEGY_DISCOVERY_RUN = "strategy_discovery.run_discovery"`, `STRATEGY_STATS_FLUSH = "maintenance.strategy_stats_flush"`, `STRATEGY_LIGHT_RECHECK = "maintenance.strategy_light_recheck"`, `STRATEGY_PATTERN_BACKFILL = "maintenance.strategy_pattern_backfill"`.
- [X] T006 Add `emit_fk_transitive_rls_policy(table_name, *, parent_table, fk_column, parent_pk="id", workspace_column="workspace_id", policy_name=None) -> tuple[str, ...]` to `libs/shared/app_shared/models/rls.py`, alongside the existing `emit_rls_policy`. Return exactly three statements (ENABLE / FORCE / CREATE POLICY) where the policy is `USING (EXISTS (SELECT 1 FROM {parent} p WHERE p.{pk} = {table}.{fk} AND p.{ws_col} = NULLIF(current_setting('app.workspace_id', true), '')::uuid))` — the same fail-closed `NULLIF` guard as `emit_rls_policy` (contracts/rls-and-migration.md, FR-026, SC-005).
- [X] T007 Create the three ORM models in `libs/shared/app_shared/models/strategy.py` (columns/types/nullability/defaults **verbatim** from data-model §2–§4; UUIDv7 PKs via shared `Base`, FR-028) and register them in `libs/shared/app_shared/models/__init__.py`: `DomainStrategyProfile(Base, WorkspaceScopedBase, TimestampMixin)` (status default `DISCOVERY_REQUIRED`; nullable preferred access/extraction methods + `Numeric(5,4)` confidences; `confirmed_success_count`/`recent_failure_count` default 0; `url_pattern_version`; unique `uq_dsp_ws_competitor_domain_pattern` on `(workspace_id, competitor_id, domain, url_pattern)`; composite FK `(workspace_id, competitor_id)→competitors(workspace_id, id)`; consumption lookup index on `(workspace_id, competitor_id, domain, url_pattern, url_pattern_version)`); `StrategyAttemptStats(Base, TimestampMixin)` — **no `WorkspaceScopedBase`** (§22 has no `workspace_id`); `method_name` a plain `Text` (not `enum_column`, D1); unique `uq_sas_profile_method_type_name` on `(domain_strategy_profile_id, method_type, method_name)`; FK → `domain_strategy_profiles.id`; `StrategyDiscoveryRun(Base, WorkspaceScopedBase)` (`sample_size`, status default `PENDING`, nullable winning access/extraction methods, `completed_at`; composite competitor FK; lookup index; **no** unique constraint).
- [X] T008 Register the two workspace-owned models in `libs/shared/app_shared/repository.py::WORKSPACE_OWNED_MODELS` (`DomainStrategyProfile`, `StrategyDiscoveryRun`) for `scoped_select` + the `check_workspace_scoping.py` CI guard; deliberately **exclude** `StrategyAttemptStats` (no `workspace_id`), documenting inline the SPEC-10 dual-scope exclusion precedent (contracts/rls-and-migration.md §Registry wiring).
- [X] T009 Implement `libs/shared/app_shared/strategy/repository.py`: workspace-scoped profile queries (`resolve_profile(session, ws, competitor_id, domain, url_pattern)`, list/get helpers via `scoped_select(DomainStrategyProfile, ws)`) and a `stats_for_profile(session, ws, profile_id)` helper that joins `StrategyAttemptStats` to its scoped parent profile (the only way the un-scoped stats table is read — FR-026, D3).
- [X] T010 Author the single Alembic migration `alembic/versions/<rev>_domain_strategy_optimizer_tables.py`, `down_revision = "851220acab90"`. Hand-author (no live Postgres in this env) reproducing the three ORM shapes exactly; create in order `domain_strategy_profiles`, `strategy_attempt_stats`, `strategy_discovery_runs` with the real FKs (workspace→workspaces, composite `(workspace_id, competitor_id)→competitors`, `domain_strategy_profile_id→domain_strategy_profiles`), the two unique constraints, and all indexes; emit RLS **in the same migration** — `emit_rls_policy("domain_strategy_profiles")`, `emit_fk_transitive_rls_policy("strategy_attempt_stats", parent_table="domain_strategy_profiles", fk_column="domain_strategy_profile_id")`, `emit_rls_policy("strategy_discovery_runs")`; `downgrade()` drops the three tables in reverse order; **not partitioned** (contracts/rls-and-migration.md, FR-026/FR-027/FR-028).
- [X] T011 [P] Unit test `tests/unit/test_strategy_enums.py`: assert the three new enums' members/values (data-model §1) and that `method_name` validation accepts an `AccessMethod` value only when `method_type=ACCESS` and an `ExtractionMethod` value only when `EXTRACTION` (D1) — runs without infra.
- [X] T012 [P] Unit test `tests/unit/test_strategy_rls_ddl.py`: assert the rendered DDL strings for all three tables — two standard `emit_rls_policy` triples plus the `emit_fk_transitive_rls_policy` triple (ENABLE/FORCE/CREATE with the exact `EXISTS (SELECT 1 FROM domain_strategy_profiles p WHERE p.id = strategy_attempt_stats.domain_strategy_profile_id AND p.workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)` predicate) — the SPEC-02 `test_rls_policy.py` precedent, no infra (FR-026, SC-005).
- [X] T013 [P] Verify single-head + scoping guards: `uv run scripts/check_single_head.sh` (one Alembic head after T010) and `uv run scripts/check_workspace_scoping.py` (profiles/runs registered, stats excluded). Add/extend a unit test `tests/unit/test_strategy_single_head.py` asserting a single head if the repo's single-head convention is test-driven.

**Checkpoint**: Schema, enums, config, RLS emitter, migration, and scoped repository are in place — user stories can begin. RLS-DDL + enum + single-head unit tests green.

---

## Phase 3: User Story 1 — Learn & store the winning access/extraction method (Priority: P1) 🎯 MVP

**Goal**: Per-method attempt stats keyed uniquely by `(profile_id, method_type, method_name)`; a pure `evaluate_promotion` promotes a method after **3 qualifying successes across ≥3 distinct URLs** (confidence ≥ 0.85, valid numeric price, valid currency-when-required), setting the profile's preferred access/extraction methods (each learned **separately**) + confidences and moving it to `ACTIVE` via a guarded atomic UPDATE. URL grouping reuses the shipped `derive_url_pattern` at the stamped version.

**Independent Test**: Seed a `LEARNING` profile; feed a qualifying-success sequence across 3 distinct URLs of one pattern for one access + one extraction method → `evaluate_promotion` returns `promote=True`, apply sets preferred methods/confidences, bumps `confirmed_success_count`, status `ACTIVE`; 3 successes across only 2 URLs → `promote=False`; a below-threshold/invalid-price/missing-currency success never counts; `derive_url_pattern` groups two differing product URLs under one `example.com/products/*` at the current version.

### Tests for User Story 1

- [ ] T014 [P] [US1] Unit test `tests/unit/test_url_pattern_grouping.py`: assert `derive_url_pattern("https://www.example.com/products/red-shoe-123")` and `derive_url_pattern("http://example.com/products/blue-shoe-999?ref=x#frag")` both → `example.com/products/*` at `URL_PATTERN_ALGORITHM_VERSION` (reuses the shipped `app_shared.url_pattern`, no new derivation — AS4, FR-001..FR-004, D10). Include locale-prefix + `:id` cases from Edge Cases.
- [ ] T015 [P] [US1] Unit test `tests/unit/test_promotion.py`: boundary tests for `evaluate_promotion` — 3 qualifying successes / ≥3 distinct URLs → `promote=True` (AS1); 3 successes / 2 distinct URLs → `promote=False` (AS2); below-threshold / invalid-price / missing-required-currency never enters the qualifying count (AS3); access and extraction evaluated independently (AS5). Runs without infra (FR-010/FR-011).

### Implementation for User Story 1

- [ ] T016 [US1] Implement the pure evaluator `libs/shared/app_shared/strategy/promotion.py::evaluate_promotion(combined, distinct_url_count, thresholds) -> PromotionDecision(promote, confidence, reason)`: `promote = qualifying_success_count ≥ min_successes AND distinct_url_count ≥ min_distinct_urls`; deterministic, framework-agnostic (contracts/promotion.md, D9). Document the "qualifying success" gate (confidence ≥ threshold ∧ valid numeric `Decimal` price ∧ valid currency-when-required) that the caller enforces at record time.
- [ ] T017 [US1] Implement guarded promotion apply in `libs/shared/app_shared/strategy/promotion.py` (used by the flush task in US5): on a qualifying access method set `preferred_access_method` + `access_confidence`; on a qualifying extraction method set `preferred_extraction_method` + `extraction_confidence`; bump `confirmed_success_count`; move to `ACTIVE` via a single atomic `UPDATE domain_strategy_profiles SET … WHERE id=:pid AND status IN ('DISCOVERY_REQUIRED','LEARNING','DEGRADED') AND (preferred_* IS NULL OR preferred_* <> :m)` so concurrent workers cannot double-promote or corrupt the count (contracts/promotion.md, Edge Cases "Concurrent promotion", FR-011).
- [ ] T018 [P] [US1] Skip-clean integration test `tests/integration/test_promotion_apply.py`: seed a profile + qualifying stats directly (US1 is testable with direct writes), run apply, assert preferred methods/confidences/`confirmed_success_count`/`status=ACTIVE`, and that a non-qualifying sequence leaves the profile un-promoted and a second concurrent apply does not double-promote. `pytest.skip(...)` when Postgres absent (FR-010/FR-011, SC-001).

**Checkpoint**: A qualifying sequence promotes the winning access + extraction methods and moves the profile to `ACTIVE`; URL grouping + promotion unit tests green.

---

## Phase 4: User Story 2 — Start future scrapes from the learned strategy (Priority: P2)

**Goal**: A pure, version-guarded, workspace-scoped `resolve_strategy_start` returns the preferred `(access, extraction)` for `ACTIVE`/`LEARNING`-with-preference profiles (never `DISABLED`, never a mismatched `url_pattern_version`); the spider group-resolution seam looks the profile up (get-or-create, auto-enqueuing discovery for a new key), seeds the first attempt from the learned start, and falls back to the default ladder otherwise.

**Independent Test**: `resolve_strategy_start(active_profile_PROXY_HTTP+CSS, algorithm_version=1)` → `(PROXY_HTTP, CSS)`; `None` profile → `None` (caller uses default ladder); `DISABLED` → `None`; `url_pattern_version != current` → `None` (never mix versions).

### Tests for User Story 2

- [ ] T019 [P] [US2] Unit test `tests/unit/test_resolution.py`: `resolve_strategy_start` returns the preferred pair for an `ACTIVE` profile (AS1) and for `LEARNING`-with-preference; returns `None` for a missing profile (AS2), a `DISABLED` profile (AS3), a `DEGRADED`-without-preference profile, and a `url_pattern_version` mismatch (AS4). Runs without infra (FR-013/FR-014/FR-015).

### Implementation for User Story 2

- [ ] T020 [US2] Implement the pure resolver `libs/shared/app_shared/strategy/resolution.py::resolve_strategy_start(profile, *, algorithm_version) -> StrategyStart | None` per contracts/consumption.md rules (D6): return the learned `(access_method, extraction_method)` iff profile present, status `ACTIVE` or (`LEARNING` with a preferred method), not `DISABLED`, and `profile.url_pattern_version == algorithm_version`; else `None`. Framework-agnostic, exhaustively unit-tested (FR-013/FR-014/FR-015).
- [ ] T021 [US2] Implement `resolve_or_create_strategy_profile(session, redis, *, workspace_id, competitor_id, domain, url_pattern)` in `libs/shared/app_shared/strategy/resolution.py` (or `repository.py`): workspace-scoped lookup by the unique key honoring a manual `url_pattern` override / `domain_access_rules.url_pattern_override` (FR-006); if absent, insert a `DISCOVERY_REQUIRED` profile stamped with `URL_PATTERN_ALGORITHM_VERSION` and enqueue `STRATEGY_DISCOVERY_RUN` on the `strategy_discovery` queue via `app_shared.messaging.enqueue` (the automatic discovery trigger — D5, FR-016; feeds US3). Emit the `strategy_profile_seeded` event (source=AUTO).
- [ ] T022 [US2] Extend `apps/scrapers/price_monitor/spiders/generic_price_spider.py::load_targets` (off-reactor, per `(competitor_id, url_pattern)` group — no new reactor hop, no new spider): derive the lookup pattern (override else `derive_url_pattern`), call `resolve_or_create_strategy_profile(...)`, then `resolve_strategy_start(profile, algorithm_version=URL_PATTERN_ALGORITHM_VERSION)` — non-`None` seeds the group's first `AttemptPlan` access method to `preferred_access_method` and reorders extraction to try `preferred_extraction_method` first (fallback to full order on failure); `None` leaves the default ladder unchanged. Thread `profile_id` + resolved start onto each `TargetBundle`/`ScrapeResult` so US5 recording has the profile id without a second query (contracts/consumption.md, D5/D6, SC-001).
- [ ] T023 [P] [US2] Skip-clean integration test `tests/integration/test_consumption_seam.py`: an `ACTIVE` profile → the group's first attempt uses the preferred access + extraction methods; an unseen key → default ladder + a `DISCOVERY_REQUIRED` profile created and `STRATEGY_DISCOVERY_RUN` enqueued once; a `DISABLED` profile → default ladder. `pytest.skip(...)` when Postgres/Redis/Scrapyd absent (FR-013/FR-016, SC-001).

**Checkpoint**: US1 + US2 both work independently — confirmed keys start from the learned methods; unseen keys fall back and auto-enqueue discovery.

---

## Phase 5: User Story 3 — Discover a strategy for a brand-new domain (Priority: P3)

**Goal**: A Celery task on the existing `strategy_discovery` queue validates a 3–10 URL sample, records a `strategy_discovery_runs` row through its lifecycle, probes candidate access then extraction methods over the sample via the **existing** fetch/extract pipeline (the one path allowed to try multiple methods), selects the winner, and seeds/updates the profile out of `DISCOVERY_REQUIRED` via a shared seed helper. Triggerable both automatically (US2 T021) and by explicit operator API — both converge on this one task + seed path.

**Independent Test**: Trigger discovery for a key with 5 sample URLs → a run row with `sample_size=5` progresses `PENDING→RUNNING→COMPLETED`, records `winning_access_method`/`winning_extraction_method`/`completed_at`, and the profile leaves `DISCOVERY_REQUIRED` → `LEARNING`/`ACTIVE`; a sample of 2 or 11 is rejected (422 / `FAILED`); a no-winner run records `NO_WINNER` and the profile stays `DISCOVERY_REQUIRED`.

### Tests for User Story 3

- [ ] T024 [P] [US3] Unit test `tests/unit/test_discovery_seed.py`: `sample_size` 2 and 11 rejected, 3..10 accepted (AS2, FR-019); `seed_from_discovery` on a winner moves `DISCOVERY_REQUIRED → ACTIVE` when the sample already satisfies the 3-confirmation rule (reuses `evaluate_promotion`) else `→ LEARNING` (AS3), and `NO_WINNER` leaves the profile `DISCOVERY_REQUIRED` (AS4). Runs without infra.
- [ ] T025 [P] [US3] Skip-clean integration test `tests/integration/test_discovery_run.py`: trigger `STRATEGY_DISCOVERY_RUN` for a key with 5 sample URLs → run `sample_size=5` progresses to `COMPLETED` with winning methods + `completed_at`, profile seeded out of `DISCOVERY_REQUIRED` (AS1/AS3); no-winner path → `NO_WINNER`, profile unchanged (AS4); and `POST /v1/strategy/discovery-runs` with 2/11 samples → HTTP 422 (AS2). `pytest.skip(...)` when Postgres/Redis/Scrapyd absent (FR-016..FR-019, SC-006).

### Implementation for User Story 3

- [ ] T026 [US3] Implement the shared seed helper `libs/shared/app_shared/strategy/seed.py::seed_from_discovery(session, run, *, winning_access, winning_extraction, confidences)`: upsert the `domain_strategy_profiles` row for the key (unique `(workspace, competitor, domain, url_pattern)`), set preferred methods + confidences + `last_discovery_at`, and move it out of `DISCOVERY_REQUIRED` (`→ ACTIVE` when the sample already satisfies the 3-confirmation rule via `evaluate_promotion`, else `→ LEARNING`); `NO_WINNER` leaves it `DISCOVERY_REQUIRED`. Shared by the auto and operator paths and by rediscovery re-runs against `DEGRADED`/`LEARNING` profiles (contracts/discovery.md, FR-018, D9).
- [ ] T027 [US3] Implement the discovery task in `apps/workers/app/workers/tasks_strategy.py` (new): `STRATEGY_DISCOVERY_RUN` on the `strategy_discovery` queue — validate `3 ≤ len(sample_urls) ≤ 10` (out of bounds → `FAILED`), `validate_competitor_url` each sample (SSRF guard, Constitution VI), insert a `strategy_discovery_runs` row `PENDING→RUNNING`, drive the sample through the **existing** SPEC-07/10/11 dispatch + spider + extraction pipeline testing the internal access ladder (`DIRECT_HTTP→DIRECT_HTTP_RETRY→PROXY_HTTP`; **`PLAYWRIGHT_PROXY` is reserved vocabulary and MUST be skipped/short-circuited during discovery until SPEC-14 can execute it** — F2) then extraction methods (no external unlocker), select the winner by the promotion-quality bar (valid numeric price + valid currency-when-required + confidence ≥ threshold across the most sample URLs; ties broken by the deterministic **cost order** in contracts/discovery.md §Select winner — cheapest access `DIRECT_HTTP < DIRECT_HTTP_RETRY < PROXY_HTTP < PLAYWRIGHT_PROXY`, then §16 extraction order — F5), write `winning_*`/`completed_at` or `NO_WINNER`/`FAILED`, and call `seed_from_discovery`. Emit `strategy_discovery_completed`. Fully off-reactor (contracts/discovery.md, D7, FR-016..FR-019). Do NOT rebuild fetch/extract.
- [ ] T028 [US3] Implement the operator API in `apps/api/app/routers/strategy.py`, `apps/api/app/schemas/strategy.py`, `apps/api/app/services/strategy.py` (new, workspace-scoped `/v1`): `POST /v1/strategy/discovery-runs` validates 3..10 `sample_urls` → **422** on out-of-bounds (FR-019) and enqueues the same `STRATEGY_DISCOVERY_RUN` task (FR-016), returning the created `PENDING` run; `GET /v1/strategy/discovery-runs` (cursor list) and `GET /v1/strategy/discovery-runs/{id}` (`scoped_get`, 404 cross-workspace). `Decimal`/confidence fields serialize as strings (Constitution VII). Register the router in the API app (contracts/api-and-observability.md).

**Checkpoint**: US1–US3 functional — a new domain can be discovered (auto or operator), the run is recorded, and the profile is seeded; sample bounds enforced.

---

## Phase 6: User Story 4 — Detect degradation and trigger rediscovery (Priority: P4)

**Goal**: A pure `evaluate_rediscovery` marks an `ACTIVE` profile `DEGRADED` and enqueues discovery on any FR-020 condition (3 consecutive preferred-method failures via `recent_failure_count`; cumulative `success_rate < 0.80` from persisted + pending deltas; repeated empty selector / low confidence / 403-429 / currency-gone / unrealistic price / template change). Driven both inline at flush (US5) and by a periodic light re-check.

**Independent Test**: `recent_failure_count = 3` on an `ACTIVE` profile → `trigger=True`, apply → `DEGRADED` + `STRATEGY_DISCOVERY_RUN` enqueued; combined `success_rate = 0.79` → trigger; repeated low-confidence/empty-selector/403-429/currency-gone/unrealistic-price/template-change → trigger; healthy signals (rate ≥ 0.80, no consecutive failures, confidence ≥ 0.75) → `trigger=False` (stays `ACTIVE`); light re-check detects degradation without a full failed batch.

### Tests for User Story 4

- [ ] T029 [P] [US4] Unit test `tests/unit/test_rediscovery.py`: boundary tests for each of the 8 FR-020 conditions firing `trigger=True` (AS1/AS2/AS3), and healthy signals → `trigger=False` (AS boundary). Conditions 1–2 driven via `combined_stats` (assert the combined-count read uses persisted + pending deltas for the success-rate condition); conditions 3,5,6,7,8 driven via a `recent_signals` (`RecentSignals`) fixture per FR-020a (consecutive-occurrence threshold, default 3), including the FR-020b detection rules (unrealistic = §18 bound failure; template-change = re-derived `url_pattern` ≠ profile pattern). Runs without infra.

### Implementation for User Story 4

- [ ] T030 [US4] Implement `libs/shared/app_shared/strategy/rediscovery.py::evaluate_rediscovery(profile, combined_stats, recent_signals, thresholds) -> RediscoveryDecision(trigger, reason)` (pure, deterministic — all 8 FR-020 conditions; `combined_stats` for conditions 1–2, `recent_signals` for conditions 3,5,6,7,8 per FR-020a/FR-020b — **no hot-path stats-schema widening**) plus a `RecentSignals` builder `build_recent_signals(session, profile)` that reads the last-N consecutive `request_attempts` for the preferred access+extraction method (error_code, HTTP status, extracted price, currency-present, confidence, observed URL) off the hot path, and `apply`: on `trigger` set `status=DEGRADED` via guarded `UPDATE … WHERE id=:pid AND status='ACTIVE'` (never rediscover `DISABLED`), enqueue `STRATEGY_DISCOVERY_RUN` on the `strategy_discovery` queue via `app_shared.messaging.enqueue`, record `last_failed_at`, and emit `strategy_rediscovery_triggered` (contracts/rediscovery.md, D8, FR-020/FR-020a/FR-020b, SC-004).
- [ ] T031 [US4] Implement the periodic light re-check task `STRATEGY_LIGHT_RECHECK` in `apps/workers/app/workers/tasks_strategy.py` (`maintenance` queue): scan `ACTIVE` profiles workspace-scoped in batches, build `recent_signals` (T030 `build_recent_signals`) + combined counts, evaluate `evaluate_rediscovery`, and apply without a full failed batch (FR-021, US4 AS4); and add the periodic enqueue entry to `apps/scheduler/app/scheduler/scheduler_app.py`.

**Checkpoint**: US1–US4 functional — degraded strategies are detected (inline + patrol) and rediscovery is enqueued within one evaluation cycle.

---

## Phase 7: User Story 5 — Buffer attempt stats and flush atomically (Priority: P5)

**Goal**: Per-method counters buffer in Redis (atomic `HINCRBY`/`SADD`, keyed by profile id), recorded **inside the existing off-reactor `_flush_batch`** (no new reactor hop, no blocking Redis/DB on the reactor), and flushed to Postgres by a Celery task + at job finalization with a **single atomic `count = count + delta` per key**; promotion/rediscovery read persisted counts **plus** pending deltas.

**Independent Test**: N `record_attempt` calls for one `(profile, method_type, method_name)` → the `stratstat:` HASH accumulates via `HINCRBY` with **zero** primary-store writes; `drain` + flush → exactly one `count = count + delta` UPSERT per key, a second flush with no new activity writes nothing; `read_pending` before a flush → promotion/rediscovery see persisted + pending; the reactor-safety grep stays green.

### Tests for User Story 5

- [ ] T032 [P] [US5] Unit test `tests/unit/test_stats_buffer.py` (fake/real Redis, `REDIS_URL` if set): `record_attempt` N times → `stratstat:` HASH fields accumulate via `HINCRBY`, `straturl:` SET grows only on qualifying successes, `stratdirty:{ws}` gains the profile id; **no** primary-store write occurs (AS1, SC-003); `read_pending` returns the non-destructive delta (AS3, FR-024); `drain` reads-and-resets the stat hash in one Lua round-trip while leaving the url SET intact until promotion (AS2). MUST actually run and pass.
- [ ] T033 [P] [US5] Skip-clean integration test `tests/integration/test_flush_promote.py`: buffer qualifying stats for one profile, run `flush_profile` → exactly one UPSERT per `(method_type, method_name)` key with `count = count + delta`, a second flush with no new activity writes nothing, and inline promotion moves the profile to `ACTIVE`; combined persisted+pending counts drive the decision (AS2/AS3, FR-023/FR-024, SC-003). `pytest.skip(...)` when Postgres/Redis absent.

### Implementation for User Story 5

- [ ] T034 [US5] Implement `libs/shared/app_shared/strategy/stats_buffer.py` (redis-client param, stdlib only — the `app_shared/access/budget.py` shape, **no** Scrapy/Twisted/FastAPI/SQLAlchemy import): `record_attempt(redis, *, workspace_id, profile_id, method_type, method_name, success, response_time_ms, confidence, url, qualifying)` (atomic `HINCRBY` attempt/success|failure/rt_ms_sum, `HINCRBY conf_sum int(confidence*10000)` on success, `HINCRBY qual_success` + `SADD straturl sha1(url)` when qualifying, `SADD stratdirty:{ws}`, `PEXPIRE` all touched keys with `STRATEGY_STATS_KEY_TTL_SECONDS`; Redis errors logged + swallowed — best-effort telemetry); `read_pending(...)` (non-destructive `HGETALL` + `SCARD`); `drain(...)` (single Lua `EVAL`/`register_script`: `HGETALL`+`DEL` the stat hash, `SCARD` the url set without deleting it). Keys per contracts/stats-buffer.md §Keys / data-model §6 (FR-022/FR-024/FR-025, D4). **This recorder stays success/failure/rt/confidence/URL only — it is NOT widened with per-error/currency/template signals (FR-020a): rediscovery's outcome conditions read those off the hot path from `request_attempts` via T030 `build_recent_signals`.**
- [ ] T035 [US5] Implement `libs/shared/app_shared/strategy/flush.py::flush_profile(session, redis, profile_id)` (SQLAlchemy): for each dirty `(method_type, method_name)` key, `drain` then a **single** `INSERT … ON CONFLICT (domain_strategy_profile_id, method_type, method_name) DO UPDATE SET count = count + EXCLUDED.count, success_rate = new_success/NULLIF(new_attempt,0), avg_* = running_avg, last_*_at = GREATEST(...)` (no app-side read-modify-write, FR-023); update the profile's `recent_failure_count` (++ on a preferred-method failure delta, reset to 0 on a qualifying-success delta — Clarification #2), `last_success_at`/`last_failed_at` (FR-012); evaluate promotion (T017) on persisted + pending counts and rediscovery (T030) on persisted + pending counts **plus a freshly built `recent_signals`** (T030 `build_recent_signals`, FR-020a); `SREM stratdirty:{ws} profile_id` once no pending deltas remain (contracts/stats-buffer.md §Flush).
- [ ] T036 [US5] Implement the flush task `STRATEGY_STATS_FLUSH` in `apps/workers/app/workers/tasks_strategy.py` (`maintenance` queue): enumerate dirty profiles from `stratdirty:{ws}` and call `flush_profile` for each; emit `strategy_stats_flushed`. Add the periodic enqueue entry to `apps/scheduler/app/scheduler/scheduler_app.py`, and trigger a flush for the job's profiles at job finalization in `apps/workers/app/workers/tasks_jobs.py` (FR-023, SC-003).
- [ ] T037 [US5] Extend `libs/scrape-core/scrape_core/pipelines.py::_flush_batch` (already inside `run_in_thread`, off-reactor): after persisting each `RequestAttempt`/`PriceObservation`, call `app_shared.strategy.stats_buffer.record_attempt(...)` **only for items whose group resolved a `profile_id`** (threaded on in US2 T022), passing `qualifying` computed from the SPEC-06/07 money/confidence validation. No new reactor hop, no blocking Redis on the reactor (contracts/stats-buffer.md §Called only from, FR-025, SC-007).
- [ ] T038 [P] [US5] Extend `tests/unit/test_reactor_safety_grep.py` (or assert it stays green): ZERO `time.sleep` / synchronous Redis/`EVAL` outside a `run_in_thread`/`deferToThread` boundary across the spider and `scrape_core/pipelines.py`, and assert the strategy stats recorder is only reachable from `_flush_batch` (already an off-reactor entry point) — runs without infra (FR-025, SC-007).

**Checkpoint**: All five stories functional — thousands of attempts on one domain issue zero per-attempt stats writes; ≤1 UPDATE per key per flush; promotion/rediscovery never read stale counts; reactor-safety proof green.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Operator read/management API, structured observability, the version-bump backfill mechanism, isolation tests, package hygiene, lint, and full validation.

- [ ] T039 [P] Extend `apps/api/app/routers/strategy.py` + `schemas/strategy.py` + `services/strategy.py`: `GET /v1/strategy/profiles` (cursor list, filterable by competitor/domain/status), `GET /v1/strategy/profiles/{id}` (`scoped_get` + per-method stats via `strategy/repository.py`), `PATCH /v1/strategy/profiles/{id}` (operator `url_pattern` override / `status=DISABLED`/re-enable — FR-006/FR-014, guarded, workspace-scoped) (contracts/api-and-observability.md).
- [ ] T040 [P] Emit the six structured `strategy_*` events (JSON logs + counters, Constitution §31) at their sites: `strategy_profile_seeded` (T021/T026), `strategy_method_promoted` (T017), `strategy_rediscovery_triggered` (T030), `strategy_discovery_completed` (T027), `strategy_stats_flushed` (T036), `strategy_learned_start_used` (T022 resolver hit, SC-001) with the fields in contracts/api-and-observability.md §Observability.
- [ ] T041 [P] Implement the version-bump backfill task `STRATEGY_PATTERN_BACKFILL` in `apps/workers/app/workers/tasks_strategy.py` (`maintenance` queue): when `URL_PATTERN_ALGORITHM_VERSION` changes, re-derive + re-link (or re-queue discovery for) affected profiles so lookups never mix versions (FR-005, D10 — defined mechanism, not exercised at version 1). Add its scheduled/on-demand enqueue path.
- [ ] T042 [P] Add public re-exports to `libs/shared/app_shared/strategy/__init__.py` (promotion, rediscovery, resolution, seed, stats_buffer, flush, repository) and confirm `app_shared` imports NO Scrapy/Twisted/FastAPI/`apps.*` (Constitution I) — assert via the existing import-boundary test or a new `tests/unit/test_strategy_import_boundary.py`.
- [ ] T043 [P] Skip-clean integration test `tests/integration/test_strategy_rls_isolation.py`: with no `app.workspace_id` GUC set, a select on each of the three tables returns **0 rows** (profiles/runs via `emit_rls_policy`; stats via the transitive EXISTS policy); workspace A cannot read workspace B's profile/run/stats (cross-workspace denied). `pytest.skip(...)` when Postgres absent (FR-026, SC-005).
- [ ] T044 Run `uv run ruff check libs/shared/app_shared/strategy libs/shared/app_shared/models libs/scrape-core/scrape_core apps/scrapers apps/workers apps/api apps/scheduler` and fix findings.
- [ ] T045 Run the full suite `uv run pytest -q` from repo root: confirm all unit tests PASS and every infra-dependent integration test SKIPS cleanly (no failures, no fakes); confirm `uv run alembic heads` / `scripts/check_single_head.sh` show one head and `scripts/check_workspace_scoping.py` passes. Then execute the quickstart.md walkthrough (Scenarios 1–6) and confirm the SC-001..SC-007 mapping holds.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies — start immediately.
- **Foundational (Phase 2)**: depends on Setup — BLOCKS all user stories (enums, config, task names, RLS emitter, models, migration, repository wiring).
- **User Stories (Phases 3–7)**: all depend on Foundational.
  - US1 (P1) is the MVP — pure promotion + apply; independently testable with direct writes.
  - US2 (P2) depends on Foundational + the profile model; the resolver is independent of US1; the spider seam (T022) threads `profile_id` that US5 recording consumes.
  - US3 (P3) depends on US1 (`evaluate_promotion` reused by `seed_from_discovery`) and on the auto-enqueue from US2 T021; the operator API is independent.
  - US4 (P4) depends on Foundational; its inline call site is US5's flush task (T035), its periodic call site is standalone.
  - US5 (P5) depends on US1 (promotion apply) + US4 (rediscovery apply) for the inline flush evaluation, and on US2 T022 for the threaded `profile_id` at record time.
- **Polish (Phase 8)**: depends on the targeted stories (API extends US3's router; observability wires all sites; RLS isolation tests need the migration).

### Within Each User Story

- Pure evaluators (`promotion.py`, `resolution.py`, `rediscovery.py`, `stats_buffer.py`) before their callers (flush task, spider seam, worker tasks).
- Unit tests are authored alongside their pure logic (must pass); integration tests skip-clean.

### Parallel Opportunities

- **Setup**: T002 in parallel with T001's tail.
- **Foundational**: T004, T005 are `[P]` (distinct files); T011, T012, T013 (tests/guards) are `[P]`; T006→T007→T008/T009→T010 serialize on the model/RLS/migration chain.
- **US1**: T014, T015, T018 `[P]`; T016→T017 share `promotion.py` (serialize).
- **US2**: T019, T023 `[P]`; T020→T021 (resolution.py) before T022 (spider).
- **US3**: T024, T025 `[P]`; T026→T027→T028.
- **US4**: T029 `[P]`; T030→T031.
- **US5**: T032, T033 `[P]`; T034→T035→T036, T037 after T034, T038 after T037.
- **Polish**: T039, T040, T041, T042, T043 are `[P]` (distinct files/tests); T044→T045 last.

---

## Parallel Example: User Story 1

```bash
# Author the US1 tests and the pure promotion evaluator together (different files):
Task: "Unit test test_url_pattern_grouping.py"   # T014
Task: "Unit test test_promotion.py"              # T015
Task: "Skip-clean test_promotion_apply.py"       # T018
```

## Parallel Example: User Story 5

```bash
Task: "Unit test test_stats_buffer.py"           # T032
Task: "Skip-clean test_flush_promote.py"         # T033
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1 Setup → Phase 2 Foundational (CRITICAL — blocks everything).
2. Phase 3 US1 → **STOP and VALIDATE**: a qualifying sequence promotes the winning access + extraction methods and moves the profile to `ACTIVE`; an operator can already see which method won for a domain.

### Incremental Delivery

1. Setup + Foundational → schema, enums, migration, RLS, repository ready.
2. US1 (P1) → learn & promote (MVP).
3. US2 (P2) → consume the learned start (+ auto-enqueue discovery for new keys).
4. US3 (P3) → discover a strategy for a brand-new domain.
5. US4 (P4) → detect degradation & trigger rediscovery.
6. US5 (P5) → atomic buffered stats + off-reactor flush (the scale-safety guarantee that underpins US1/US4 counting).
7. Polish → operator read/management API, observability, backfill, isolation tests, lint, full skip-clean suite, quickstart walkthrough.

### Parallel Team Strategy

After Foundational: Developer A takes US1 (promotion) then US5 (buffer/flush — the counting substrate); Developer B takes US2 (resolver + spider seam); Developer C takes US3 (discovery + API) then US4 (rediscovery). The pure evaluators are independent; the flush task (US5) is the convergence point that calls promotion (US1) + rediscovery (US4).

---

## Notes

- `[P]` = different files, no dependency on an incomplete task.
- `[USn]` maps a task to its user story for traceability; Setup/Foundational/Polish carry no story tag.
- **No Docker daemon in this build env**: unit tests (URL-pattern reuse, promotion/rediscovery/resolution evaluators, Redis buffer math, RLS-DDL rendering, enum validation, single-head) MUST actually run and pass; every integration test (`test_promotion_apply`, `test_consumption_seam`, `test_discovery_run`, `test_flush_promote`, `test_strategy_rls_isolation`) MUST `pytest.skip(...)` cleanly when Postgres/Redis/Scrapyd are absent — never fake success (SPEC-05..11 convention).
- **Reuse, do not rebuild**: `derive_url_pattern` + `URL_PATTERN_ALGORITHM_VERSION`, the `AccessMethod`/`ExtractionMethod` enums, the SPEC-07 spider attempt path + off-reactor `_flush_batch`, the SPEC-10 Redis atomic-counter pattern, the models `Base`/`WorkspaceScopedBase`/RLS helpers, `app_shared.messaging.enqueue`, alembic single-head (`down_revision = 851220acab90`). Do NOT create tasks that rebuild fetch/extract.
- **Isolation is non-negotiable**: all three tables RLS-protected in the creating migration; `strategy_attempt_stats` (no `workspace_id`) isolated transitively via the new fail-closed EXISTS policy on its parent profile; no-context query → 0 rows (FR-026, SC-005).
- Commit after each task or logical group; stop at any checkpoint to validate a story independently.
