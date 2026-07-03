---
description: "Task list for Scrapyd HTTP Spider MVP (SPEC-07)"
---

# Tasks: Scrapyd HTTP Spider MVP

**Input**: Design documents from `/specs/007-scrapyd-http-spider/`

**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/, quickstart.md

**Tests**: Included — the spec, plan (Project Structure `tests/unit` + `tests/integration`), and quickstart.md explicitly enumerate the unit and live test suites, matching the SPEC-02→06 test-first pattern. Live-stack tests are **authored but skip-marked** (no Postgres/Redis/Scrapyd container engine in this build env — deferred-verification pattern).

**Organization**: Tasks are grouped by user story (US1..US5) to enable independent implementation and testing. Shared blocking work is in Setup + Foundational.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task serves (US1..US5); omitted for Setup / Foundational / Integration / Polish
- Every task lists an exact file path

## Path Conventions

Backend monorepo (uv workspace). Scraping-side code in `libs/scrape-core/scrape_core/`; models/enums/dispatch client/config in `libs/shared/app_shared/`; the Scrapy project in `apps/scrapers/price_monitor/`; the dispatch task in `apps/workers/app/workers/`; migration at repo-root `alembic/versions/`; tests in `tests/unit/`, `tests/integration/`, and fixtures in `tests/fixtures/html/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Dependencies and test assets needed before any implementation.

- [X] T001 [P] Add `parsel` to `[project.dependencies]` in `libs/scrape-core/pyproject.toml` (pure HTML/JSON-LD/CSS parsing so extraction is unit-testable without booting Scrapy/Twisted), then `uv sync`.
- [X] T002 [P] Pin `requests` explicitly in `libs/shared/app_shared/pyproject.toml` (used by the framework-agnostic Scrapyd dispatch client), then `uv sync`.
- [X] T003 [P] Create the local fixture HTML corpus under `tests/fixtures/html/` — `jsonld_product.html`, `css_only.html`, `regex_only.html`, `single_number.html`, `discount_save_x.html` (old/"save X"/installment/shipping-only), and an SSRF redirect target descriptor — used by unit + live tests; no real-competitor content (FR-021/SC-007).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Enums, config, the three ORM models + migration, the decided-once reactor-safe DB seam, the transport item, and the error vocabulary — every user story depends on these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T004 [P] Extend `libs/shared/app_shared/enums.py` with `AccessMethod` (`DIRECT_HTTP`/`DIRECT_HTTP_RETRY`/`PROXY_HTTP`/`PLAYWRIGHT_PROXY`), `StockStatus` (`IN_STOCK`/`OUT_OF_STOCK`/`UNKNOWN`), `ExtractionMethod` (`JSON_LD`/`CSS`/`REGEX`/`SINGLE_NUMBER` + forward-compat `PLATFORM_JSON`/`EMBEDDED_JSON`/`XPATH`/`PLAYWRIGHT`), and `ScrapeErrorCode` (§34 vocabulary) — all `StrEnum` → `VARCHAR` (per data-model.md, contracts/errors.md).
- [X] T005 [P] Extend `libs/shared/app_shared/config.py` with `SCRAPE_FLUSH_MAX_ITEMS=50` and `SCRAPE_FLUSH_INTERVAL_SECONDS=2.0` (batched-flush thresholds; env/DB-tunable, not hardcoded constants).
- [X] T006 [P] Create `libs/scrape-core/scrape_core/errors.py` — `ScrapeErrorCode` usage constants (§34) + helpers to classify fetch failures (`HTTP_403/404/429`, `TIMEOUT`, `DNS_ERROR`, `PRICE_NOT_FOUND`, `LOW_CONFIDENCE_PRICE`, `CURRENCY_MISMATCH`, `INVALID_PRICE_FORMAT`, `BLOCKED`, `UNKNOWN_ERROR`; SSRF rejection surfaced as `BLOCKED`) per contracts/errors.md.
- [X] T007 Create `libs/shared/app_shared/models/observations.py` — three `WorkspaceScopedBase` ORM models: `PriceObservation` (partitioned, composite `PRIMARY KEY (id, scraped_at)`, `__table_args__` `postgresql_partition_by="RANGE (scraped_at)"`), `RequestAttempt` (partitioned, composite `PRIMARY KEY (id, created_at)`, `postgresql_partition_by="RANGE (created_at)"`), and `MatchCurrentPrice` (`UniqueConstraint(workspace_id, match_id)`). Money via `Money`/`NUMERIC(18,4)`, confidence `Numeric(5,4)`, currency `CHAR(3)`, soft UUID refs (no FK) except real FK on `workspace_id`; enum columns via `enum_column`; timestamps `TZDateTime` (per data-model.md, contracts/models-observations.md). Depends on T004.
- [X] T008 Extend `libs/shared/app_shared/models/__init__.py` to re-export `PriceObservation`, `RequestAttempt`, `MatchCurrentPrice` (Base.metadata visibility for Alembic autogenerate). Depends on T007.
- [X] T009 Extend `libs/shared/app_shared/repository.py` to add the three new models to `WORKSPACE_OWNED_MODELS` (so `scoped_select`/`scoped_get` + the CI scoping guard cover them). Depends on T007.
- [X] T010 Create the Alembic migration `alembic/versions/<rev>_observations_current_prices_tables.py` — create the two partitioned parents (`PARTITION BY RANGE`, composite PK incl. partition key) + `op.execute` current + next month `PARTITION OF` tables, create `match_current_prices` with `unique(workspace_id, match_id)`, and `emit_rls_policy` (ENABLE + FORCE + fail-closed) on all three parents; downgrade drops partitions → parents → current-prices; `down_revision = a4f205e8d7de` (single head). Follows contracts/migration-observations.md. Depends on T007.
- [X] T011 [P] Create `libs/scrape-core/scrape_core/db.py` — the decided-once reactor-safe DB seam: `run_in_thread(fn, *a, **kw) -> Deferred` via `twisted.internet.threads.deferToThread`, and `workspace_txn(workspace_id)` context manager reusing `app_shared.database.get_session` + `set_workspace_context` (RLS active), small per-process pool through PgBouncer, no DB call on the reactor thread (per contracts/reactor-safe-db.md). Depends on nothing new.
- [X] T012 [P] Create `libs/scrape-core/scrape_core/items.py` — `ScrapeResult` dataclass/Item carrying the full observation + request-attempt field set + `workspace_id`/`match_id`/`product_id`/`product_variant_id`/`competitor_id`/`scrape_job_id` (per data-model.md Transport shapes). Depends on T004.

**Foundational tests** (validate the foundational artifacts):

- [X] T013 [P] `tests/unit/test_observations_models.py` — table/column shapes, composite PK incl. partition key, `postgresql_partition_by` option, `unique(workspace_id, match_id)`, Money/`NUMERIC(5,4)`/`CHAR(3)`, enum columns.
- [X] T014 [P] `tests/unit/test_rls_observations.py` — `emit_rls_policy` render (ENABLE+FORCE, fail-closed DDL) for all three tables.
- [X] T015 [P] `tests/unit/test_migration_offline_observations.py` — `alembic upgrade head --sql` renders `PARTITION BY RANGE` parents + current+next month partitions + `unique(workspace_id, match_id)` + RLS on all three; single head preserved.
- [X] T016 [P] `tests/unit/test_observations_scoping_guard.py` — the workspace-scoping CI guard flags a planted unscoped `select` on the three new models.

**Checkpoint**: Enums, models, migration, reactor-safe DB seam, and transport item exist — user stories can now begin.

---

## Phase 3: User Story 1 - Extract and persist a price from a product page (Priority: P1) 🎯 MVP

**Goal**: A `generic_price_spider` run loads a workspace-scoped match + cached resolved profile, fetches the page over `DIRECT_HTTP`, extracts a price via JSON-LD, validates it, and persists a successful `PriceObservation` + one `RequestAttempt` + an upserted `MatchCurrentPrice`.

**Independent Test**: Seed one workspace/product/variant/competitor/match/profile; serve a JSON-LD fixture; schedule the spider with `workspace_id`/`scrape_job_id`/`match_ids`; assert exactly one `price_observations` row (correct price/currency, `extraction_method=JSON_LD`, confidence ≥ threshold, `success=true`), `match_current_prices` upserted, one `request_attempt`, all scoped to the workspace.

### Implementation for User Story 1

- [ ] T017 [P] [US1] Create `libs/scrape-core/scrape_core/extraction/__init__.py` and `libs/scrape-core/scrape_core/extraction/result.py` — `ExtractionCandidate` dataclass (`raw_price_text`, `currency`, `method`, `confidence`, `selector_used`, `raw_title`, `stock`, `matched_text`) per contracts/extraction.md.
- [ ] T018 [P] [US1] Create `libs/scrape-core/scrape_core/extraction/jsonld.py` — pure `parsel` parse of `<script type="application/ld+json">` `Product`/`Offer` → `ExtractionCandidate` (default confidence 0.95 from `app_shared.profiles.confidence`, never a literal). Depends on T017.
- [ ] T019 [US1] Create `libs/scrape-core/scrape_core/extraction/pipeline.py` — ordered `extract(html, profile)` orchestrator (JSON-LD first for this story; CSS/regex added in US3), returns the first-hit candidate else `PRICE_NOT_FOUND`. Depends on T017, T018.
- [ ] T020 [US1] Create `libs/scrape-core/scrape_core/validation.py` — `validate_candidate(candidate, validation_rules, confidence_cfg) -> Accepted | Rejected(error_code)` core path: `app_shared.money.parse_money` (exact `Decimal`; reject float/NaN/Infinity/over-scale — never round → `INVALID_PRICE_FORMAT`), `> 0` guard, and the confidence gate (`>= min_accepted_confidence`, default 0.75 via `resolve_confidence_rules`, else `LOW_CONFIDENCE_PRICE`). Full rules extended in US3. Depends on T017.
- [ ] T021 [US1] Create `libs/scrape-core/scrape_core/pipelines.py` — `BatchedPersistencePipeline`: buffer `ScrapeResult` items, flush at `SCRAPE_FLUSH_MAX_ITEMS` **or** `SCRAPE_FLUSH_INTERVAL_SECONDS` (Twisted `LoopingCall`) + a final flush at `close_spider`; each flush a **single** `run_in_thread` transaction that bulk-inserts observations + attempts and upserts `match_current_prices` (`insert(...).on_conflict_do_update` on `(workspace_id, match_id)`, never overwriting the current price with a failure). Per contracts/persistence-pipeline.md. Depends on T011, T012, T007.
- [ ] T022 [US1] Create `apps/scrapers/price_monitor/spiders/generic_price_spider.py` — parse args (`workspace_id`, `scrape_job_id`, `match_ids`, `mode`); load matches **scoped to `workspace_id`** (skip a match not found for the workspace); consume the **cached resolved** scrape profile (SPEC-06, no per-match re-resolution); yield `DIRECT_HTTP` requests; run extraction + validation in `parse`; yield `ScrapeResult` items for both success and failure (persist-only, no `price_analysis` emission). Per contracts/spider-args.md. Depends on T019, T020, T012.
- [ ] T023 [US1] Extend `apps/scrapers/price_monitor/settings.py` — set `ROBOTSTXT_OBEY=False`, register `BatchedPersistencePipeline` in `ITEM_PIPELINES`, wire the small per-process pool, and read flush knobs from `Settings`/config. Depends on T021.

### Tests for User Story 1

- [ ] T024 [P] [US1] Create `tests/unit/test_extraction_jsonld_css_regex.py` — JSON-LD fixture extracts with confidence 0.95; orchestrator returns JSON-LD first; `PRICE_NOT_FOUND` when nothing matches (CSS/regex/single-number cases added in US3).

**Checkpoint**: JSON-LD happy path extracts, validates, and persists end-to-end (fixture-scale). US1 is independently testable.

---

## Phase 4: User Story 2 - Block unsafe (SSRF) fetches at connection time (Priority: P1)

**Goal**: Before any body download the spider re-resolves the host and validates the **connected IP** against the deny rules, re-validating **every redirect hop**; scheme/userinfo rejected pre-fetch; robots policy honored per competitor (Principle VI: internal-only + legally compliant access).

**Independent Test**: A match URL resolving to a private/loopback/link-local IP (or public → 302 → internal) is refused before body download; no `success=true` observation; the failure is recorded (`BLOCKED`); a disallowed-by-robots path under `RESPECT` is skipped/recorded.

### Implementation for User Story 2

- [ ] T025 [P] [US2] Create `libs/scrape-core/scrape_core/safety/__init__.py` and `libs/scrape-core/scrape_core/safety/fetch.py` — `validate_resolved_target(url, *, resolver, allowlist=None)`: run the save-time `app_shared.url_safety.validate_competitor_url` (scheme allow-list, userinfo rejection, IP-literal deny), then resolve via the **injected** `resolver` and reject any unsafe resolved IP (reuse `_reject_ip`) unless explicitly allowlisted. Per contracts/fetch-url-safety.md.
- [ ] T026 [P] [US2] Create `libs/scrape-core/scrape_core/safety/resolver.py` — `SafeResolver`, a Twisted resolver wrapper installed via the `DNS_RESOLVER` setting that resolves then **refuses to return an unsafe IP** (defeats DNS rebinding at connect time).
- [ ] T027 [US2] Create `libs/scrape-core/scrape_core/safety/middleware.py` — `SsrfGuardMiddleware`: `process_request` re-checks scheme/userinfo before fetch; redirect handling re-validates **every** hop; a rejection short-circuits to a flagged failure item (no body download, `BLOCKED`). Depends on T025.
- [ ] T028 [US2] Create `libs/scrape-core/scrape_core/robots.py` — `RobotsPolicyMiddleware`, a custom **per-request** downloader middleware resolving `robots_policy` (`RESPECT`/`REVIEW_REQUIRED`/`IGNORE_AFTER_APPROVAL`) from the loaded competitor config (never Scrapy's process-global `ROBOTSTXT_OBEY`); injectable robots fetcher for fixtures. Per contracts/robots-middleware.md.
- [ ] T029 [US2] Extend `apps/scrapers/price_monitor/settings.py` — set `DNS_RESOLVER=scrape_core.safety.resolver.SafeResolver` and add `SsrfGuardMiddleware` + `RobotsPolicyMiddleware` to `DOWNLOADER_MIDDLEWARES`. Depends on T027, T028, T023.

### Tests for User Story 2

- [ ] T030 [P] [US2] Create `tests/unit/test_fetch_url_safety.py` — injected public IP accepted; private/loopback/link-local/unique-local/metadata resolved IP rejected; each redirect hop re-validated; scheme/userinfo rejected pre-fetch; production path (no allowlist) uses the real resolver seam.
- [ ] T031 [P] [US2] Create `tests/unit/test_robots_middleware.py` — `RESPECT` skips a disallowed path (`BLOCKED`); `IGNORE_AFTER_APPROVAL` fetches; policy read per-request from config, not global.

**Checkpoint**: Unsafe fetches are refused at connection time and per redirect hop; robots policy is per-competitor. US2 is independently testable.

---

## Phase 5: User Story 3 - Multiple extraction strategies with confidence and price validation (Priority: P2)

**Goal**: The spider tries JSON-LD → CSS → regex in order, attaches a default confidence to each, and fully validates the candidate (currency mismatch, min/max, `reject_if_text_contains`, single-number heuristic) before accepting — a wrong price is worse than a missing one.

**Independent Test**: CSS-only and regex-only fixtures yield observations with the expected method + default confidence (0.85 / 0.75); a single unlabeled number scores 0.40 and is rejected; a discount/"save X"/old/installment/shipping-only candidate is rejected; a currency-mismatch page is saved `comparable=false` + `CURRENCY_MISMATCH`.

### Implementation for User Story 3

- [ ] T032 [P] [US3] Create `libs/scrape-core/scrape_core/extraction/css.py` — pure `parsel` CSS selectors for price/old_price/currency/stock/title → `ExtractionCandidate` (default confidence 0.85 from shared config). Depends on T017.
- [ ] T033 [P] [US3] Create `libs/scrape-core/scrape_core/extraction/regex.py` — DB regex rules → `ExtractionCandidate` (0.75); single unlabeled-number heuristic (0.40). Depends on T017.
- [ ] T034 [US3] Extend `libs/scrape-core/scrape_core/extraction/pipeline.py` to the full ordered chain JSON-LD → CSS → regex (first hit wins, else `PRICE_NOT_FOUND`). Depends on T032, T033.
- [ ] T035 [US3] Extend `libs/scrape-core/scrape_core/validation.py` with the full rule set: currency match/`required_currency` → mismatch marks `comparable=false` + `CURRENCY_MISMATCH` (still saved, no FX), `min_price`/`max_price` bounds, and `reject_if_text_contains` against `matched_text` (old/installment/discount/"save X"/shipping). Per contracts/price-validation.md. Depends on T020.

### Tests for User Story 3

- [ ] T036 [US3] Extend `tests/unit/test_extraction_jsonld_css_regex.py` — CSS-only (0.85) and regex-only (0.75) fixtures extract; fallback order JSON-LD → CSS → regex; single unlabeled number → 0.40. Depends on T024.
- [ ] T037 [P] [US3] Create `tests/unit/test_price_validation.py` — Decimal exactness; float/NaN/Infinity/over-scale/non-positive rejected (never rounded → `INVALID_PRICE_FORMAT`); currency mismatch → `comparable=false` + `CURRENCY_MISMATCH`; min/max; `reject_if_text_contains`; confidence < 0.75 → `LOW_CONFIDENCE_PRICE`.

**Checkpoint**: All three strategies + full validation/confidence gate work. US3 is independently testable.

---

## Phase 6: User Story 4 - Authenticated dispatch to Scrapyd (Priority: P2)

**Goal**: An authenticated, idempotent `schedule.json` client schedules `generic_price_spider`; unauthenticated calls are rejected; a retried dispatch of the same batch does not double-run.

**Independent Test**: Correct credentials schedule the spider (returns a jobid); missing/wrong credentials are rejected (401) and schedule nothing; a second dispatch of the same `(scrape_job_id, batch_index)` is a no-op.

### Implementation for User Story 4

- [ ] T038 [P] [US4] Create `libs/shared/app_shared/scrapyd/__init__.py` and `libs/shared/app_shared/scrapyd/client.py` — `ScrapydDispatchClient.schedule(project, spider, *, workspace_id, scrape_job_id, match_ids, mode, batch_index) -> jobid`: POST `schedule.json` with HTTP **basic auth** (`SCRAPYD_USERNAME`/`SCRAPYD_PASSWORD`, `SCRAPYD_HTTP_URLS`), args passed through unchanged; **idempotency** via stable `dispatch_key = f"dispatched:{scrape_job_id}:{batch_index}"` guarded by Redis `SET NX` (reuse `app_shared.redis_client`) — an existing key returns the persisted jobid without re-scheduling. No scrapy/twisted. Per contracts/scrapyd-dispatch.md.
- [ ] T039 [US4] Create `apps/workers/app/workers/tasks_dispatch.py` — a thin Celery task `dispatch_generic_price_spider(workspace_id, scrape_job_id, match_ids, mode, batch_index)` delegating to `app_shared.scrapyd.client` (full scheduler/orchestration is a later spec). Depends on T038.

### Tests for User Story 4

- [ ] T040 [P] [US4] Create `tests/unit/test_scrapyd_dispatch.py` — `schedule()` sends basic auth + args → jobid; missing/wrong creds → 401, no schedule; `SET NX` guard: second dispatch of the same key is a no-op (fake Redis).

**Checkpoint**: Authenticated, idempotent dispatch works. US4 is independently testable.

---

## Phase 7: User Story 5 - Reactor-safe, batched persistence (Priority: P3)

**Goal**: Guarantee that DB writes never block the reactor and that observations/attempts/current-price writes flush in small batches (by size or time) with a final flush at close — the persistence design property enforced from the start.

**Independent Test**: Over N fixture matches, all N observations persist with observed commit count ≪ N (batched flushing), the final partial batch is flushed at close, and every DB call is dispatched off the reactor thread via the `deferToThread` seam.

### Implementation for User Story 5

- [ ] T041 [US5] Harden `libs/scrape-core/scrape_core/pipelines.py` — confirm the time-based `LoopingCall` flush, the size-based flush, and the `close_spider` final flush all route through `scrape_core.db.run_in_thread` (no synchronous commit / `time.sleep` / blocking Redis on the reactor thread), and thresholds are read from config. Depends on T021.

### Tests for User Story 5

- [ ] T042 [P] [US5] Create `tests/unit/test_persistence_batching.py` — flush at N items, at T seconds, and final flush at close; N items → ≪ N flushes; buffer emptied; DB routed through the (mocked) `deferToThread` seam.
- [ ] T043 [P] [US5] Create `tests/unit/test_reactor_safe_db.py` — `run_in_thread` returns a Deferred / offloads (no blocking call on the calling thread); `workspace_txn` sets workspace context.

**Checkpoint**: Reactor-safety and batched-flush guarantees are verified. US5 is independently testable.

---

## Phase 8: Integration (live-stack, authored + skip-marked)

**Purpose**: End-to-end scenarios against a real Postgres/Redis/Scrapyd stack. Authored now and **skip cleanly** where those services are unreachable (no container engine in this build env — SPEC-02→06 deferred-verification pattern). Zero real-competitor network calls; fixtures only (FR-021/SC-007).

- [ ] T044 [P] Create `tests/integration/test_spider_jsonld_fixture_live.py` (US1/SC-001) — seed ws/product/variant/competitor/match/profile; serve JSON-LD fixture (loopback server, resolver allowlisted); run spider; assert 1 success observation + `match_current_prices` upsert + 1 `request_attempt`; workspace-scoped.
- [ ] T045 [P] Create `tests/integration/test_spider_ssrf_live.py` (US2/SC-002) — match URL resolving to a private IP and a public→internal 302: refused pre-body; no `success=true` observation; failure recorded `BLOCKED`.
- [ ] T046 [P] Create `tests/integration/test_spider_strategies_live.py` (US3/SC-003) — CSS-only and regex-only fixtures → expected method + confidence; discount/"save X"-only fixture rejected.
- [ ] T047 [P] Create `tests/integration/test_dispatch_scrapyd_live.py` (US4/SC-005) — authenticated `schedule.json` returns a jobid; unauthenticated rejected; retried dispatch of the same `(scrape_job_id, batch_index)` does not double-run.
- [ ] T048 [P] Create `tests/integration/test_spider_batch_live.py` (US5/SC-006) — N fixture matches → all N observations persist with commit count ≪ N; DB off the reactor thread.
- [ ] T049 [P] Create `tests/integration/test_observations_isolation_live.py` (Isolation/Principle II) — cross-workspace read/write blocked (app scoping + RLS); no workspace context → 0 rows.

---

## Phase 9: Polish & Cross-Cutting Concerns

**Purpose**: Boundary enforcement and final validation across stories.

- [ ] T050 [P] Extend `tests/unit/test_import_boundaries.py` — cover the new `scrape_core.*` modules; assert `app_shared.models.observations` and `app_shared.scrapyd.client` import **no** scrapy/twisted/fastapi (one-way `apps → libs`; `scrape_core` may import `app_shared`, never the reverse).
- [ ] T051 Run the unit suite and offline migration render per quickstart.md — `uv run pytest tests/unit -q` and `SPECIFY_FEATURE_DIRECTORY=specs/007-scrapyd-http-spider uv run alembic upgrade head --sql` (expect the two `PARTITION BY RANGE` parents, current+next partitions, `unique(workspace_id, match_id)`, and RLS on all three; single head).
- [ ] T052 Confirm `apps/scrapers/price_monitor/settings.py` `SPIDER_MODULES`/packaging makes `generic_price_spider` discoverable under Scrapyd (deployment sanity; no live run required here).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately.
- **Foundational (Phase 2)**: Depends on Setup — **BLOCKS all user stories**.
- **User Stories (Phases 3–7)**: All depend on Foundational. US1 (P1) is the MVP. US2 (P1) can proceed in parallel with US1. US3/US4/US5 (P2/P2/P3) depend only on Foundational (US3 extends US1's extraction/validation files; US5 hardens US1's pipeline — see notes).
- **Integration (Phase 8)**: Depends on the corresponding user-story implementations; authored anytime, skip-marked.
- **Polish (Phase 9)**: Depends on all implemented modules existing.

### Story-level dependencies & shared-file notes

- **US1**: independent after Foundational. Creates `settings.py`, `extraction/pipeline.py`, `validation.py`.
- **US2**: independent after Foundational; **extends `settings.py`** (T029 after T023) — sequential edit to the same file.
- **US3**: extends US1's `extraction/pipeline.py` (T034 after T019) and `validation.py` (T035 after T020), and US1's extraction test file (T036 after T024) — sequential edits to shared files; the new `css.py`/`regex.py` are independent.
- **US4**: fully independent after Foundational (new files only).
- **US5**: hardens US1's `pipelines.py` (T041 after T021); tests are independent.

### Within Each Story

- Models/candidates/dataclasses before orchestrators before spider/pipeline wiring.
- Settings wiring after the middleware/pipeline it registers exists.

---

## Parallel Opportunities

- **Setup**: T001, T002, T003 all [P].
- **Foundational**: T004, T005, T006 [P]; T011, T012 [P] (after enums for T012); tests T013–T016 [P] once their targets exist.
- **US1**: T017, T018 [P]; test T024 [P].
- **US2**: T025, T026 [P]; tests T030, T031 [P].
- **US3**: T032, T033 [P]; test T037 [P].
- **US4**: T038 [P]; test T040 [P].
- **US5**: tests T042, T043 [P].
- **Integration**: T044–T049 all [P] (distinct files).
- **Cross-story**: with Foundational done, US1, US2, US4 can be built in parallel by different developers; US3 and US5 slot in once US1's extraction/pipeline files exist.

### Parallel Example: Foundational

```bash
# Enums, config, error vocabulary in parallel:
Task: "Extend enums in libs/shared/app_shared/enums.py"
Task: "Extend config in libs/shared/app_shared/config.py"
Task: "Create scrape_core/errors.py"

# Reactor-safe seam and transport item in parallel:
Task: "Create libs/scrape-core/scrape_core/db.py"
Task: "Create libs/scrape-core/scrape_core/items.py"
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Complete Phase 1 (Setup) + Phase 2 (Foundational).
2. Complete Phase 3 (US1) — JSON-LD extract → validate → persist end-to-end at fixture scale.
3. **STOP and VALIDATE**: run US1 unit tests; the live JSON-LD scenario is authored (skip-marked here).

### Incremental Delivery

1. Setup + Foundational → foundation ready.
2. US1 (P1, MVP) → the spider path works.
3. US2 (P1) → SSRF + robots safety guards ship with the first spider (Principle VI).
4. US3 (P2) → all three strategies + full validation guardrails.
5. US4 (P2) → authenticated idempotent dispatch.
6. US5 (P3) → reactor-safe + batched-flush guarantees verified.
7. Integration (skip-marked) + Polish (boundaries, quickstart validation).

### Parallel Team Strategy

After Foundational: Developer A → US1, Developer B → US2, Developer C → US4; then US3 (extends US1 extraction/validation) and US5 (hardens US1 pipeline) once US1's shared files land.

---

## Scope Guardrails (do not exceed)

- `DIRECT_HTTP` only — no proxies, browser spider, access-policy dispatch, rate limiter, in-flight dedup, or domain-strategy optimizer.
- Spider **stops at persistence** — no alerts, `variant_price_states`, alert events, webhooks, or `price_analysis` emission (FR-020).
- **Exactly three** new tables (`price_observations` + `request_attempts` partitioned monthly from birth; `match_current_prices`) — **no** `scrape_jobs`/`scrape_job_targets`.
- **FR-015 is a deferred seam**: record each match's terminal outcome via `request_attempts.success` + `price_observations.success` (carrying the nullable `scrape_job_id`); do **not** write a `scrape_job_targets` state row (SPEC-08).
- RLS enabled + forced (fail-closed) on all three tables.
- Reactor-safe DB decided **once** in `scrape-core` (sync SQLAlchemy in `deferToThread`); batched persistence (50 items / 2 s + final flush); fetch-time SSRF with injectable resolver/allowlist + per-redirect-hop revalidation; per-request robots middleware; authenticated + Redis `SET NX` idempotent dispatch; fixtures-only tests.

---

## Notes

- [P] = different files, no dependency on an incomplete task.
- [Story] label maps a task to a user story for traceability; Setup/Foundational/Integration/Polish carry none.
- Live-stack tests (Phase 8) are authored and skip-marked — no Postgres/Redis/Scrapyd in this build env.
- Commit after each task or logical group.
