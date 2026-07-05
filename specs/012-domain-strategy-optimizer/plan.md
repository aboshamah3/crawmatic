# Implementation Plan: Domain Strategy Optimizer

**Branch**: `012-domain-strategy-optimizer` (not on a git branch; feature dir is the anchor) | **Date**: 2026-07-05 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/012-domain-strategy-optimizer/spec.md`

## Summary

Learn the best **access method + extraction method** per `workspace + competitor + domain + url_pattern`,
so future scrapes start from the proven combination instead of walking the full escalation ladder — and
keep those learned strategies healthy over time. This layers **learning** on top of the existing
SPEC-05..11 fetch/extract pipeline; it does **not** rebuild fetch/extract.

Delivered as three new **workspace-owned, non-partitioned** Postgres tables (`domain_strategy_profiles`,
`strategy_attempt_stats`, `strategy_discovery_runs` — §22 columns verbatim) plus pure-logic services and
thin integration seams:

1. **URL pattern grouping (foundation, FR-001..FR-006).** *Reuse* the already-shipped
   `app_shared.url_pattern.derive_url_pattern` + `URL_PATTERN_ALGORITHM_VERSION` (=1) — no new derivation
   code; profiles stamp `url_pattern_version`; manual override precedence honored; a version-bump backfill
   is a `maintenance` task.
2. **Learn & promote (US1, FR-007..FR-012).** Per-method attempt stats keyed uniquely by
   `(profile_id, method_type, method_name)`; a pure `evaluate_promotion` promotes a method after **3
   qualifying successes across ≥3 distinct URLs** (confidence ≥ 0.85, valid numeric price, valid
   currency-when-required), setting the profile's preferred access/extraction methods and moving it to
   `ACTIVE`. Access and extraction learned **separately**.
3. **Consume the learned start (US2, FR-013..FR-015).** A pure, **version-guarded, workspace-scoped**
   `resolve_strategy_start` returns the preferred `(access, extraction)` for `ACTIVE`/`LEARNING` profiles
   (never `DISABLED`, never a mismatched pattern version); the spider seeds the first attempt from it and
   falls back to the default ladder otherwise.
4. **Discover for a new domain (US3, FR-016..FR-019).** A Celery task on the existing
   `strategy_discovery` queue probes candidate access then extraction methods over a 3–10 URL sample
   (the one path allowed to try multiple methods), records a `strategy_discovery_runs` row, picks the
   winner, and seeds the profile. Triggerable **both** automatically (new key → `DISCOVERY_REQUIRED` →
   enqueue) and by explicit operator API — both converge on one task + seed code path.
5. **Rediscovery (US4, FR-020/FR-021).** A pure `evaluate_rediscovery` marks the profile `DEGRADED` and
   enqueues discovery on any FR-020 condition (3 consecutive preferred-method failures via
   `recent_failure_count`; cumulative `success_rate < 0.80` from persisted + pending deltas; repeated
   empty selector / low confidence / 403-429 / currency-gone / unrealistic price / template change).
   Driven both inline at flush and by a periodic **light re-check**.
6. **Atomic buffered stats (US5, FR-022..FR-025, the scale-safety guarantee).** Per-method counters are
   buffered in **Redis** (atomic `HINCRBY`/`SADD`, keyed by profile id), **recorded off-reactor inside
   the existing SPEC-07 `_flush_batch`**, and flushed to Postgres by a Celery task + at job finalization
   with a **single atomic `count = count + delta` per key** — no per-attempt hot-row write, no
   read-modify-write in Python, and promotion/rediscovery read persisted counts **plus** pending deltas.

**Isolation** (non-negotiable): all three tables are RLS-protected. `domain_strategy_profiles` and
`strategy_discovery_runs` carry `workspace_id` (standard `emit_rls_policy`); `strategy_attempt_stats` has
no `workspace_id` (§22) and is isolated **transitively via its profile FK** by a new
`emit_fk_transitive_rls_policy` EXISTS-subquery policy. All ids are UUIDv7.

See `research.md` (D1–D11) for decisions, `data-model.md` for the exact schema/enums/Redis keys, and
`contracts/` for each behavioral surface.

## Technical Context

**Language/Version**: Python 3.13 (repo-wide `uv` workspace; `requires-python >=3.13,<3.14`).

**Primary Dependencies**: SQLAlchemy 2.x + Alembic (three new tables + one hand-authored migration);
Redis (`redis` **sync** client — `HINCRBY`/`SADD`/`SCARD` + a Lua drain `EVAL`/`register_script`, the
store SPEC-10/11 already use); Celery (discovery on `strategy_discovery`; stats-flush / light-recheck /
pattern-backfill on `maintenance`; enqueued via the existing `app_shared.messaging.enqueue` producer
seam); Scrapy + Twisted (extend the existing spider `load_targets` + persistence `_flush_batch` — no new
spider); FastAPI (thin operator router). **No new third-party dependency** — reuses `app_shared.url_pattern`,
`app_shared.access.engine`, `scrape_core.extraction.pipeline`, `app_shared.money`.

**Storage**: PostgreSQL — three new **non-partitioned** workspace-owned tables (the learned/rolled-up
layer, §22/§29). Redis (`noeviction`) — three TTL-bounded buffered-stats key families. Existing tables
are only *read* (`competitor_product_matches` for sample URLs / pattern; `request_attempts` supplies the
raw attempt signal) and unchanged.

**Testing**: pytest. Pure logic (pattern reuse, promotion/rediscovery/consumption evaluators, Redis
buffer math against a fake/real Redis, RLS-DDL string rendering, single-head) → exhaustive **unit** tests
with no infra. Live behaviors (RLS zero-rows under real Postgres, discovery driving a real Scrapyd
sample, end-to-end flush→promote) → **integration** tests that **skip cleanly** without infra (SPEC-05..11
precedent). The static reactor-safety AST grep test (`tests/unit/test_reactor_safety_grep.py`) continues
to guarantee FR-025/SC-007 unchanged (the new recorder sits inside the already-safe `_flush_batch`).

**Target Platform**: Linux multi-service deployment (`api-service`, `scheduler-service`,
`worker-service`, `scrapyd-http-service`, `scrapyd-browser-service`, `pgbouncer`, `postgres`, `redis`).

**Project Type**: Backend monorepo (`uv` workspace) — `libs/shared` (`app_shared`), `libs/scrape-core`
(`scrape_core`), `apps/scrapers`, `apps/workers`, `apps/scheduler`, `apps/api`.

**Performance Goals**: 2,000 products & 10k–20k matches per workspace. Stats recording is O(1) atomic
Redis `HINCRBY` (no scan, no DB write per attempt); ≤1 stats-row UPDATE per (profile, method) key per
flush interval independent of attempt volume (SC-003). Profile/consumption resolution reuses the existing
**per-group** (never per-match) resolution seam (Principle IV, no N+1).

**Constraints**: Non-blocking reactor — stats recorded inside the existing off-reactor `_flush_batch`;
promotion/rediscovery/discovery/flush all in Celery; no `time.sleep`/sync-Redis/DB on the reactor thread
(FR-025, SC-007). No hot-row RMW — Redis buffer + single atomic `count = count + delta` UPDATE (FR-023).
Money is `Decimal`/`NUMERIC`; a qualifying success requires valid numeric price + valid currency-when-
required (Constitution VII). Workspace-namespaced everything; no-context query → 0 rows on all three
tables (FR-026). `app_shared` MUST NOT import Scrapy/Twisted/FastAPI/`apps/*`; `scrape_core` MAY import
`app_shared`, never the reverse.

**Scale/Scope**: 3 new tables + 1 migration + 1 new RLS emitter; new `app_shared/strategy/` package
(stats buffer, flush, promotion, rediscovery, consumption resolver, seed, repository — all pure /
redis-client / SQLAlchemy, no Scrapy/Twisted); new enums + `Settings` knobs + task-name constants; spider
`load_targets` + pipeline `_flush_batch` extensions (profile get-or-create, learned start, stats record);
new worker tasks (discovery, stats-flush, light-recheck, pattern-backfill); a thin `apps/api` operator
router; scheduler entries for the periodic tasks. **Out of scope (deferred):** actual Playwright browser
execution (SPEC-14 — `PLAYWRIGHT_PROXY`/`PLAYWRIGHT` are reserved vocabulary only); per-workspace threshold
overrides (global `Settings` defaults suffice); partitioning the three tables (rolled-up layer, not
append-heavy).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Status | How this plan complies |
|-----------|--------|------------------------|
| **I. API-First, Service-Oriented** | PASS | Pure learning logic lives in `libs/shared/app_shared/strategy/` (scraping-free — SQLAlchemy/redis-client/stdlib, no twisted/scrapy/fastapi). The consumption resolver is pure `app_shared`; the spider seam only wires it. Discovery/flush/rediscovery are Celery tasks enqueued via the existing `app_shared.messaging.enqueue` → task-name-constant seam, so the spider never imports `apps/workers`. Operator endpoints live in `apps/api` under `/v1`. No responsibility bleed. |
| **II. Workspace Isolation (NON-NEGOTIABLE)** | PASS | All three tables RLS-protected in the creating migration: `domain_strategy_profiles`/`strategy_discovery_runs` via standard `emit_rls_policy` (own `workspace_id` + real FK + composite workspace-local FK to `competitors`); `strategy_attempt_stats` (no `workspace_id` per §22) via the new **`emit_fk_transitive_rls_policy`** EXISTS-subquery on its parent profile — fail-closed (no context → 0 rows, SC-005). Redis keys namespaced (`stratdirty:{workspace_id}`; profile-id keys belong to one workspace). Profiles/runs added to `WORKSPACE_OWNED_MODELS` (scoped queries + CI guard); stats excluded (no `workspace_id`) and queried only joined to its scoped parent (SPEC-10 dual-scope exclusion precedent). Cross-workspace read/write tests required (FR-026). |
| **III. Variant-Level Pricing & Explicit Matching** | PASS (n/a) | No new pricing/matching logic. Discovery samples existing `competitor_product_matches` URLs; learning keys on the match-derived `url_pattern`. Prices are validated (Decimal/currency) only to gate a *qualifying* learning success, never to alter matching. |
| **IV. Database-Driven Configuration** | PASS | Behavior is config-driven: promotion/rediscovery/discovery thresholds are env-tunable `Settings` with the documented defaults (0.85 / 3 / 3 / 0.80 / 0.75 / 3 / 3–10) — not hardcoded. Profile + learned-start resolution reuses the existing **per-`(competitor_id, url_pattern)`-group**, Redis-cached resolution seam — never a per-match DB walk. Learned strategy itself is the DB-driven `domain_strategy_profiles` the spec exists to populate. |
| **V. Disciplined Scraping Runtime (NON-NEGOTIABLE)** | PASS | **Central to this spec.** Spiders still only persist: stats recording is added **inside the existing off-reactor `_flush_batch`** (`run_in_thread`), so no new reactor hop and no blocking Redis/DB on the reactor (FR-025). Promotion, rediscovery, discovery, and stats-flush all run in **Celery** (off-reactor). The static `test_reactor_safety_grep.py` proof stays green (the recorder is in the already-"safe" transitive set). Discovery reuses idempotent dispatch + in-flight match locks (SPEC-08/11). Browser stays a reserved fallback (`PLAYWRIGHT_*` vocabulary only; SPEC-14 executes). |
| **VI. Internal-Only & Legally Compliant Access (NON-NEGOTIABLE)** | PASS | Discovery probes only the internal ladder (`DIRECT_HTTP`/`DIRECT_HTTP_RETRY`/`PROXY_HTTP`/`PLAYWRIGHT_PROXY`); no external unlocker API, no CAPTCHA/login/paywall. Sample URLs are `validate_competitor_url`'d (SSRF guard). No raw HTML/screenshots stored — only learned strategy + rolled-up stats (§30/§38). |
| **VII. Monetary & Extraction Correctness** | PASS | A learning success counts **only** with a valid numeric `Decimal` price and valid currency-when-required, at confidence ≥ 0.85 — reusing SPEC-06/07 money/currency validation (`app_shared.money`, `Numeric(18,4)`; confidences are `Numeric(5,4)`, never `Money`/float). Currency-mismatch/absent successes don't count and are a rediscovery signal. Promotion/rediscovery evaluators are deterministic (boundary-tested). |
| **VIII. Scale-Safe Data & Concurrency** | PASS | **Delivers the §14 atomic-stats guarantee.** No hot-row write: per-method counters buffer in Redis (atomic `HINCRBY`), flushed with ≤1 `count = count + delta` UPDATE per key per interval (SC-003) — never N per-attempt writes. The three tables are the rolled-up layer, **not partitioned** (correctly — they aren't append-heavy). Concurrent promotion protected by the unique `(profile_id, method_type, method_name)` + a guarded single-statement `UPDATE`. UUIDv7 ids. Learned start reuses hot current-state resolution, no historical scan. |

**Gate result: PASS** — no violations; Complexity Tracking table intentionally empty. The one genuinely
new primitive, `emit_fk_transitive_rls_policy`, is not a deviation — it is the faithful implementation of
FR-026's mandated *transitive* isolation for a table §22 deliberately gives no `workspace_id`, and it
reuses the exact fail-closed `NULLIF(current_setting(...))` guard of the existing emitter.

## Project Structure

### Documentation (this feature)

```text
specs/012-domain-strategy-optimizer/
├── plan.md              # This file
├── research.md          # Phase 0 — decisions D1–D11
├── data-model.md        # Phase 1 — 3 tables (verbatim §22 cols), enums, Redis keys, Settings, tasks
├── quickstart.md        # Phase 1 — validation scenarios (US1–US5 + isolation)
├── contracts/           # Phase 1
│   ├── stats-buffer.md            # Redis record + atomic drain/flush (FR-022..FR-025)
│   ├── promotion.md               # pure evaluator + guarded apply (FR-010/FR-011)
│   ├── rediscovery.md             # pure evaluator + inline/periodic triggers (FR-020/FR-021)
│   ├── discovery.md               # discovery run lifecycle + profile seeding (FR-016..FR-019)
│   ├── consumption.md             # version-guarded learned-start resolver + spider seam (FR-013..FR-015)
│   ├── rls-and-migration.md       # emit_fk_transitive_rls_policy + single Alembic migration (FR-026..FR-028)
│   └── api-and-observability.md   # operator /v1 endpoints + §31 strategy events
├── spec.md
└── tasks.md             # /speckit-tasks output (NOT created here)
```

### Source Code (repository root)

```text
libs/shared/app_shared/
├── enums.py                         # +StrategyStatus, +MethodType, +DiscoveryRunStatus (VARCHAR — no DB enum)
│                                    #  (AccessMethod / ExtractionMethod REUSED as method_name — D1)
├── config.py                        # +STRATEGY_* threshold / sample-bound / flush-interval / key-TTL knobs
├── task_names.py                    # +STRATEGY_DISCOVERY_RUN / STRATEGY_STATS_FLUSH /
│                                    #   STRATEGY_LIGHT_RECHECK / STRATEGY_PATTERN_BACKFILL
├── url_pattern.py                   # (unchanged) REUSE derive_url_pattern + URL_PATTERN_ALGORITHM_VERSION
├── repository.py                    # +DomainStrategyProfile, +StrategyDiscoveryRun in WORKSPACE_OWNED_MODELS
│                                    #   (StrategyAttemptStats deliberately EXCLUDED — no workspace_id)
├── models/
│   ├── strategy.py                  # NEW — 3 ORM models (verbatim §22 columns; UUIDv7 via Base)
│   └── rls.py                       # +emit_fk_transitive_rls_policy (EXISTS-subquery, fail-closed)
└── strategy/                        # NEW package — PURE learning logic (no scrapy/twisted/fastapi)
    ├── __init__.py
    ├── stats_buffer.py              #   record_attempt / read_pending / drain (redis-client param, stdlib)
    ├── flush.py                     #   flush_profile: drain → single count=count+delta UPSERT (SQLAlchemy)
    ├── promotion.py                 #   evaluate_promotion (pure) + guarded apply
    ├── rediscovery.py               #   evaluate_rediscovery (pure) + apply (DEGRADED + enqueue)
    ├── resolution.py                #   resolve_strategy_start (pure, version-guarded) + profile get-or-create
    ├── seed.py                      #   seed_from_discovery (shared by auto + operator paths)
    └── repository.py                #   scoped profile/run queries + stats-joined-to-profile helper

libs/scrape-core/scrape_core/
└── pipelines.py                     # EXTEND _flush_batch — after persisting each RequestAttempt/observation,
                                     #   app_shared.strategy.stats_buffer.record_attempt(...) for matches whose
                                     #   group resolved a profile_id (off-reactor, no new run_in_thread hop)

apps/scrapers/price_monitor/
└── spiders/generic_price_spider.py # EXTEND load_targets — per (competitor_id, url_pattern) group:
                                     #   resolve_or_create_strategy_profile (+ auto-enqueue discovery for a new
                                     #   key) and resolve_strategy_start; seed first AttemptPlan access + reorder
                                     #   extraction to preferred-first; thread profile_id onto each result

apps/workers/app/workers/
├── tasks_strategy.py                # NEW — STRATEGY_DISCOVERY_RUN (discovery.md) + STRATEGY_STATS_FLUSH
│                                    #   (stats-buffer.md flush + inline promotion/rediscovery)
└── tasks_maintenance? / tasks_jobs.py
                                     # EXTEND — STRATEGY_LIGHT_RECHECK + STRATEGY_PATTERN_BACKFILL;
                                     #   job finalization also triggers a stats flush for the job's profiles

apps/scheduler/app/scheduler/scheduler_app.py
                                     # EXTEND — enqueue periodic STRATEGY_STATS_FLUSH + STRATEGY_LIGHT_RECHECK

apps/api/app/
├── routers/strategy.py              # NEW — POST discovery-run (3–10 validate), GET profiles/runs (cursor),
│                                    #   PATCH profile (override / DISABLE) — workspace-scoped /v1
├── schemas/strategy.py              # NEW — request/response models (Decimal-as-string)
└── services/strategy.py             # NEW — thin service wiring router → app_shared.strategy + enqueue

alembic/versions/<rev>_domain_strategy_optimizer_tables.py
                                     # NEW — 3 tables, composite/real FKs, uniques, indexes, RLS
                                     #   (emit_rls_policy ×2 + emit_fk_transitive_rls_policy ×1);
                                     #   down_revision = 851220acab90 (single head)

tests/  (mirroring SPEC-10/11 layout)
├── unit/         — promotion/rediscovery/consumption evaluators (boundary values), Redis buffer
│                   record/drain/read math, RLS-DDL rendering (incl. transitive EXISTS), enum
│                   validation, url-pattern grouping reuse, single-head, reactor-safety grep (unchanged)
└── integration/  — skip-clean: RLS zero-rows on all 3 tables (incl. transitive), discovery run over a
                    sample → winner + seed, flush→promote end-to-end, cross-workspace denial, API 422 on
                    out-of-bounds sample size
```

**Structure Decision**: Reuse the established monorepo split exactly. Pure learning logic goes in a new
`libs/shared/app_shared/strategy/` package — the sibling of `app_shared/access/` and `app_shared/limiter/`
(SQLAlchemy / injected-redis-client / stdlib, no Scrapy/Twisted/FastAPI, exhaustively unit-testable). The
three ORM models live in `app_shared/models/strategy.py` with RLS emitted (including the new transitive
emitter added to `app_shared/models/rls.py`) in the single new Alembic migration whose `down_revision` is
the current head `851220acab90`. Recording integrates at the existing off-reactor persistence seam
(`scrape_core.pipelines._flush_batch`) and the existing per-group resolution seam
(`generic_price_spider.load_targets`) — **no new spider, no new reactor hop**. Orchestration
(discovery/flush/rediscovery/backfill) is Celery on the existing `strategy_discovery` + `maintenance`
queues via the existing enqueue-by-name producer seam; the thin operator API mirrors the versioned,
cursor-paginated `/v1` surface. `URL_PATTERN_ALGORITHM_VERSION` + `derive_url_pattern` are **reused
unchanged** (no new derivation code).

**Agent context**: The `after_plan` agent-context hook is disabled in `.specify/extensions.yml`
(project does not use GitHub Copilot; `.github/copilot-instructions.md` was removed — per user memory),
so no agent-context file is updated. This is the intended configuration, not a skipped step.

## Complexity Tracking

> No Constitution Check violations — table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |
