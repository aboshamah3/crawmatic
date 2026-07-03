# Implementation Plan: Current Prices & Alert Logic

**Branch**: `009-current-prices-alerts` (not on a git branch; feature dir is the anchor) | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/009-current-prices-alerts/spec.md`

## Summary

Turn per-match `match_current_prices` observations into a **variant-level** price
comparison and a **deterministic** alert state. Three new workspace-owned tables
(`variant_price_states`, `variant_alert_states` — current-state, regular;
`price_alert_events` — append-only, **monthly-partitioned from birth**), a **pure,
DB/framework-free alert engine** implementing the ordered §23 decision tree (Decimal
quantized to 4dp `ROUND_HALF_UP` before every boundary compare, fixed severity map,
currency filter, and the event-transition rule), a **`price_analysis` Celery task on its
own queue** that reads the variant + its comparable competitor prices, runs the engine,
and idempotently upserts the three tables (writing an event **only** on a type/severity
change), recompute wiring from three triggers (scrape completion — deduplicated per
variant per job; client price/currency change; match archived/paused), and four
workspace-scoped, scope-gated read endpoints.

The engine is the acceptance core and is exhaustively unit-testable, including every
0% / 1% / 5% boundary; DB/Redis/Celery paths use skip-clean integration tests
(SPEC-05..08 convention — no live infra in this build environment).

## Technical Context

**Language/Version**: Python 3.12 (repo-wide, `uv` workspace)

**Primary Dependencies**: SQLAlchemy 2.x + Alembic (models/migration), Celery + Redis
(`price_analysis` task/queue + dedup), FastAPI (read endpoints) — all already locked
(Constitution: Locked stack). The pure engine depends on **stdlib `decimal` only**.

**Storage**: PostgreSQL via PgBouncer (transaction pooling); `NUMERIC(18,4)` money,
uuidv7 PKs, RLS (`SET LOCAL app.workspace_id`). Redis for per-variant-per-job dedup.

**Testing**: pytest. Pure engine → exhaustive unit tests (every §23 branch + boundary +
transition-rule table); DB/Redis/Celery/API → integration tests that **skip cleanly**
when infra is absent (SPEC-01..08 precedent).

**Target Platform**: Linux multi-service deployment (api-service, worker-service, …).

**Project Type**: Backend monorepo (`uv` workspace) — `libs/shared` (`app_shared`),
`libs/scrape-core` (`scrape_core`), `apps/api`, `apps/workers`, `apps/scrapers`.

**Performance Goals**: 2,000 products & 10k–20k matches per workspace. One recompute per
variant **per job** (never one write per completed match) — protects the hot
`variant_price_states` / `variant_alert_states` rows (§26, Principle VIII).

**Constraints**: No blocking Redis/DB on the Scrapy reactor thread; API MUST NOT import
`apps/workers` (enqueue by name via `app_shared.messaging`); single-head migration; Decimal
determinism (no float ever touches a boundary compare).

**Scale/Scope**: 3 tables, 4 enums, 1 pure engine module, 1 Celery task + queue, 3 trigger
wirings, 4 read endpoints. Out of scope (deferred): daily rollups / retention / partition
maintenance (SPEC-15), webhook emission (SPEC-16), product-level comparison &
`matches/{id}/current-price` & `observations` list & alert-acknowledge PATCH (deferred).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Status | How this plan complies |
|-----------|--------|------------------------|
| **I. API-First, Service-Oriented** | PASS | Engine + models + task-name constant live in `app_shared` (scraping-free). API enqueues `price_analysis` **by name** via `app_shared.messaging.enqueue` — never imports `apps/workers`. `scrape-core` pipeline emits by name too. The Celery task lives in `apps/workers`. |
| **II. Workspace Isolation (NON-NEGOTIABLE)** | PASS | All 3 tables carry `workspace_id` (FK + index), added to `WORKSPACE_OWNED_MODELS`, `emit_rls_policy` (ENABLE+FORCE+fail-closed) in the creating migration. Every read via `scoped_select`/`scoped_get`; the task calls `set_workspace_context`. No-context reads → zero rows. Cross-workspace tests required. |
| **III. Variant-Level Pricing & Explicit Matching** | PASS | State, alert, and events are all keyed at the variant (`unique(workspace_id, product_variant_id)` on both current-state tables). Competitor prices read from `match_current_prices` (one competitor URL ↔ one variant). |
| **IV. Database-Driven Configuration** | PASS (n/a) | §23 thresholds (0/1/5 %, severity map) are the fixed, spec-pinned decision tree — deliberately code-constant, not DB-tunable, so determinism is guaranteed (Principle VII "one ordered deterministic computation"). No per-match config walk. |
| **V. Disciplined Scraping Runtime (NON-NEGOTIABLE)** | PASS | `price_analysis` is a **separate Celery task on its own queue**, never run inside the spider/reactor (§8, §26). Spider persists only; emission is a fire-and-forget enqueue-by-name after the persistence transaction commits. |
| **VI. Internal-Only & Legally Compliant Access (NON-NEGOTIABLE)** | PASS (n/a) | No fetching/scraping introduced — pure analysis over already-persisted rows. |
| **VII. Monetary & Extraction Correctness** | PASS | `Decimal`/`NUMERIC(18,4)` throughout (`Money()`); `discount_vs_average` quantized 4dp `ROUND_HALF_UP` **before** any compare; NaN/Infinity rejected at boundary. Currency mismatch → excluded, `comparable=false`, `CURRENCY_MISMATCH` stored, no FX. Decision tree is one ordered deterministic function. |
| **VIII. Scale-Safe Data & Concurrency** | PASS | One recompute per variant per job (Redis `SET NX` dedup on emission); no per-match state write. Hot reads from current-state tables. `price_alert_events` monthly-partitioned from birth (PK includes partition key). All traffic through PgBouncer; `SET LOCAL` only. |

**Gate result: PASS** — no violations; Complexity Tracking table left empty.

## Project Structure

### Documentation (this feature)

```text
specs/009-current-prices-alerts/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── models-alerts.md
│   ├── migration-alerts.md
│   ├── alert-engine.md
│   ├── price-analysis-task.md
│   ├── recompute-triggers.md
│   └── api-alerts.md
├── autospec-decisions.md
└── tasks.md             # /speckit-tasks output (NOT created here)
```

### Source Code (repository root)

```text
libs/shared/app_shared/
├── enums.py                         # +AlertType, +AlertSeverity, +AlertStatus, +AlertEventType
├── alerts/                          # NEW — the PURE engine package (stdlib-decimal only)
│   ├── __init__.py                  #   re-exports engine API + constants
│   └── engine.py                    #   §23 tree, quantization, severity map, currency
│                                    #   filter, transition rule — no sqlalchemy/celery/fastapi
├── models/
│   ├── alerts.py                    # NEW — VariantPriceState, VariantAlertState, PriceAlertEvent
│   └── __init__.py                  # register the 3 models (metadata + re-export)
├── repository.py                    # add the 3 models to WORKSPACE_OWNED_MODELS
└── task_names.py                    # +PRICE_ANALYSIS_RECOMPUTE constant

alembic/versions/
└── <newrev>_alerts_price_states_tables.py   # NEW — chains onto a6b0234cd4ad (current head)

libs/scrape-core/scrape_core/
└── pipelines.py                     # emit price_analysis per affected variant (dedup/job) after commit

apps/workers/app/workers/
├── celery_app.py                    # +"price_analysis" queue + route
└── tasks_analysis.py                # NEW — the price_analysis.recompute_variant task

apps/api/app/
├── routers/
│   ├── alerts.py                    # NEW — GET /v1/alerts/current(+/{variant_id}), /v1/alert-events
│   └── variants.py                  # +GET /v1/variants/{variant_id}/price-comparison; PATCH/bulk enqueue
├── schemas/alerts.py                # NEW — response envelopes
└── main.py                          # include the new alerts router

tests/  (per-package, mirroring SPEC-08 layout)
├── unit/    — exhaustive engine tests (boundaries, severity, currency, transitions)
└── integration/ — skip-clean: migration, task upsert/idempotency/dedup, endpoints, RLS
```

**Structure Decision**: Reuse the established monorepo layout exactly. The **pure engine**
goes in `app_shared/alerts/` (mirrors how SPEC-08 kept batching/lifecycle/counter logic pure
in `app_shared/jobs/`), the **ORM** in `app_shared/models/alerts.py` (mirrors
`models/observations.py`), the **migration** chains onto the single head `a6b0234cd4ad`, the
**task** in `apps/workers` (mirrors `tasks_jobs.py`), the **emission** in the existing
`scrape_core/pipelines.py` `_flush_batch` seam (already enqueues `SCRAPE_FINALIZE_JOBS` by
name after commit), and the **endpoints** follow the `routers/jobs.py` scope-gated,
`scoped_select`, cursor-paginated conventions.

## Complexity Tracking

> No Constitution Check violations — table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |
