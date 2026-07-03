# Implementation Plan: Scrapyd HTTP Spider MVP

**Branch**: `007-scrapyd-http-spider` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/007-scrapyd-http-spider/spec.md`

## Summary

Deliver the first working vertical slice of the scraping runtime on top of the SPEC-02→06 foundation: a single, generic, DB-configurable HTTP spider (`generic_price_spider`) that runs under Scrapyd, safely fetches a competitor product page, extracts a price via DB-configured strategies (JSON-LD → CSS → regex), validates it, and persists a `PriceObservation` + a `RequestAttempt` audit row + an upserted `MatchCurrentPrice`. The slice is proven end-to-end against **local fixture HTML pages** only — zero real-competitor network calls in tests. The spider **stops at persistence** (no alerts / variant states / webhooks / `price_analysis` emission).

Concretely this feature adds:

- **`libs/scrape-core`** (built out from its skeleton — this is where all reusable scraping-side code lives, imported by the Scrapy project, one-way over `app_shared`):
  - `db.py` — the **single, decided-once reactor-safe DB seam**: synchronous SQLAlchemy (reusing `app_shared.database.get_session` + `set_workspace_context`) wrapped in Twisted `deferToThread`, through PgBouncer with the existing small per-process pool. No DB call ever runs on the reactor thread.
  - `extraction/` — **pure** (parsel/stdlib only, no Twisted) extractors: `jsonld.py`, `css.py`, `regex.py`, an ordered `pipeline.py` orchestrator returning an `ExtractionCandidate`, and `result.py` dataclasses. Fully unit-testable off-reactor.
  - `validation.py` — **pure** candidate-price validation + confidence gate, reusing `app_shared.money.parse_money` (Decimal/finite/scale/positive) and `app_shared.profiles.confidence.resolve_confidence_rules` + `validation_rules` semantics.
  - `safety/` — fetch-time SSRF: `fetch.py` (`validate_resolved_target()` extending `app_shared.url_safety` with an **injectable resolver + allowlist seam**), `resolver.py` (a Twisted resolver wrapper that refuses to hand back an unsafe IP), and `middleware.py` (a downloader middleware that pre-checks scheme/userinfo and re-validates **every redirect hop**).
  - `robots.py` — a custom **per-request** robots downloader middleware resolving `robots_policy` per competitor from loaded config (never Scrapy's process-global `ROBOTSTXT_OBEY`).
  - `pipelines.py` — the **batched persistence** item pipeline: buffer, flush every N items or T seconds (default 50 / 2 s, config-tunable) + final flush at `close_spider`, each flush a single `deferToThread` transaction (bulk-insert observations + attempts, upsert current prices).
  - `items.py` — the transport dataclass/Item carrying the observation + attempt fields.
  - `errors.py` — the §34 error-code vocabulary the pipeline/middlewares/validators emit.
- **`libs/shared/app_shared`**:
  - `models/observations.py` — three ORM models: `PriceObservation` and `RequestAttempt` (**monthly-partitioned from birth**, composite PK including the partition key), and `MatchCurrentPrice` (current-state, `unique(workspace_id, match_id)`). All three workspace-owned + RLS.
  - `enums.py` (extend): `AccessMethod`, `StockStatus`, `ExtractionMethod`, `ScrapeErrorCode` (§34).
  - `repository.py` (extend): register the three new models in `WORKSPACE_OWNED_MODELS`; `models/__init__.py` re-exports them.
  - `scrapyd/client.py` — a **requests-based** authenticated Scrapyd dispatch client (basic auth, `schedule.json`) with **idempotency** via Redis `SET NX` on a stable dispatch key. Framework-agnostic (no scrapy/twisted), consistent with `app_shared` already owning `SCRAPYD_*` settings + `redis_client`.
  - `config.py` (extend): `SCRAPE_FLUSH_MAX_ITEMS=50`, `SCRAPE_FLUSH_INTERVAL_SECONDS=2.0`.
- **`apps/scrapers`** (the Scrapyd HTTP node — packages the Scrapy project):
  - `price_monitor/spiders/generic_price_spider.py` — the spider: parse args (`workspace_id`, `scrape_job_id`, `match_ids`, `mode`), load matches scoped to `workspace_id`, consume the **cached resolved** scrape profile, issue `DIRECT_HTTP` requests, run extraction+validation, yield items.
  - `price_monitor/settings.py` (extend): `ROBOTSTXT_OBEY=False`, install the SSRF resolver + SSRF/robots downloader middlewares and the batched persistence pipeline, small pool, flush knobs.
- **`apps/workers`**: a thin Celery dispatch task wrapper over `app_shared.scrapyd.client` (the full scheduler/orchestration is a later spec — US4 is exercised via the client + tests).
- **repo root**: one Alembic migration creating the three tables — the two partitioned parents (`PARTITION BY RANGE`, composite PK, current + next month partitions) + `match_current_prices` — and `emit_rls_policy` on all three, chained onto the current head `a4f205e8d7de`.

Everything DB/reactor/network-independent is fully unit-tested **here** (extraction corpus, price validation + confidence gate, fetch-time SSRF accept/deny incl. redirect hops via the injected resolver, batching flush-boundary logic, dispatch client auth + idempotency guard, model/partition/PK/RLS DDL render, Scrapyd client request shape). Live-stack items (real spider run under Scrapyd, actual partition routing, RLS row denial, end-to-end fixture scrape) are authored and **skip cleanly** where no Postgres/Redis/Scrapyd is reachable — matching the SPEC-02→06 deferred-verification pattern (no container engine in this build env).

## Technical Context

**Language/Version**: Python 3.13 (`requires-python = ">=3.13,<3.14"`; uv workspace).

**Primary Dependencies**:
- Existing: SQLAlchemy 2.0 (sync) + PostgreSQL dialect (`insert(...).on_conflict_do_update` for the `match_current_prices` upsert; `postgresql_partition_by` table option); psycopg 3; Alembic; `redis`; `requests` (Scrapyd dispatch client — already an indirect dep, pin explicitly in `app_shared`).
- Scraping-side (in `scrape-core`, runtime-loaded by the Scrapy project, never by `app_shared`): Scrapy 2.13 + `parsel` (pure HTML/JSON-LD/CSS parsing — safe to import off-reactor for unit tests), Twisted (`deferToThread`, resolver). `parsel` is added to `scrape-core`'s deps so extraction is unit-testable without booting Scrapy/Twisted.
- Import boundary (enforced by `tests/unit/test_import_boundaries.py`): `app_shared` MUST NOT import scrapy/twisted/playwright/fastapi; `scrape_core` MAY import `app_shared` + scrapy/twisted, never the reverse; `apps → libs` only.

**Storage**: PostgreSQL 17 via PgBouncer (transaction pooling) with a small per-process spider pool (`DB_POOL_SIZE`, existing). Two new **monthly-partitioned** append tables (`price_observations` by `scraped_at`, `request_attempts` by `created_at`) created partitioned from birth with the partition key in the PK (§22 rule) + current + next month partitions; one current-state table (`match_current_prices`). RLS enabled+forced on all three in the creating migration; workspace context set per-transaction (`set_config('app.workspace_id', :wsid, true)`) on every spider DB transaction. Redis for the dispatch idempotency guard + (already-built) profile-resolution cache the spider consumes.

**Testing**: pytest. Reactor/DB/network-independent logic unit-tested here (pure extraction, validation, SSRF w/ injected resolver, batching boundaries, dispatch client, DDL render offline `alembic upgrade head --sql`). Live-stack tests authored + skip-marked (no Postgres/Redis/Scrapyd in this env).

**Target Platform**: Linux containers. Only `apps/api` is publicly exposed; Scrapyd nodes are internal-only and basic-auth protected.

**Project Type**: Backend monorepo (uv workspace). Spans `libs/scrape-core` (all scraping-side code), `libs/shared/app_shared` (models, enums, dispatch client, config), `apps/scrapers` (Scrapy project + spider + settings), `apps/workers` (thin dispatch task), plus repo-root Alembic.

**Performance Goals**: Persistence is **batched** — flush every 50 items or 2 s (config-tunable) + final flush at close, each flush a single bulk transaction through PgBouncer (never one commit per item — at 10k–20k targets/job per-item commits serialize the pooler). Extraction + validation + SSRF checks are O(1)-per-page pure CPU. The spider consumes the **cached** resolved profile (SPEC-06 Redis cache), never re-walking the resolution chain per match. Every DB/Redis call off the reactor thread (`deferToThread` / non-blocking).

**Constraints**: Reactor safety is mandatory — no synchronous commit, `time.sleep`, or blocking Redis round-trip on the reactor thread; the DB seam is decided **once** in `scrape-core` (sync SQLAlchemy + `deferToThread`). Fetch-time SSRF validates the **resolved IP at connection time** with **per-redirect-hop** re-validation, via an injectable resolver/allowlist seam (tests drive both deny and, for local fixtures, an allowed public IP; prod validates the real resolved IP with **no** allowlist). Money is `Decimal`/`NUMERIC(18,4)`; floats forbidden; NaN/Infinity/over-scale/non-positive rejected (not rounded). Currency mismatch → `comparable=false` + `CURRENCY_MISMATCH`, excluded from comparison (no FX). Confidence < 0.75 (tunable) → rejected. Transaction-pooling-safe only (`SET LOCAL`/`set_config(...,true)`; no session advisory locks; `prepare_threshold=None`). Partitioned-from-birth; retention is partition-drop (later spec). No proxies / rate limiter / in-flight dedup / domain-strategy optimizer / browser spider (later specs); `DIRECT_HTTP` only.

**Scale/Scope**: Foundation for 10k–20k matches/workspace (§39). This spec adds **exactly three** tables + the spider + extraction/validation/SSRF/robots/persistence machinery in `scrape-core` + an authenticated idempotent dispatch client. **No** alert/variant-state/webhook/`price_analysis` computation, **no** `variant_price_states` population, **no** `scrape_jobs`/`scrape_job_targets` tables (owned by the later orchestration spec — see Complexity Tracking on FR-015).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Relevance | How this plan satisfies it |
|-----------|-----------|----------------------------|
| **I. API-First / Service boundaries** | Core: first build-out of `scrape-core` + the Scrapy project | All reusable scraping code (DB seam, extraction, validation, SSRF, robots, batched pipeline, items, errors) lives in `libs/scrape-core` and is imported by the `apps/scrapers` Scrapy project — never the reverse. `scrape_core` imports `app_shared` (money, url_safety, database, enums, models) one-way; `app_shared` stays scrapy/twisted/playwright/fastapi-free (import-boundary test **extended** to cover the new `scrape_core.*` modules and to assert `app_shared.observations`/`scrapyd.client` import no scrapy/twisted). The Scrapyd HTTP node stays internal-only; only `apps/api` is public. **PASS** |
| **II. Workspace Isolation (NON-NEGOTIABLE)** | Spider reads + writes workspace-owned rows | The spider receives `workspace_id` and scopes **every** query to it via `scoped_select`/`scoped_get`; the three new models are added to `WORKSPACE_OWNED_MODELS` (CI guard `scripts/check_workspace_scoping.py` covers them). Each spider DB transaction calls `set_workspace_context` before any read/write, so DB-level RLS (`emit_rls_policy` ENABLE+FORCE+fail-closed `NULLIF(current_setting('app.workspace_id',true),'')::uuid`, applied to all three tables — RLS on a partitioned parent propagates to partitions) is the second, fail-closed layer. Cross-workspace read/write + no-context (0 rows) tests authored (live-DB). A match not found for the workspace is skipped, never cross-read. **PASS** |
| **III. Variant-Level Pricing & Explicit Matching** | Observations are variant-level | Every `price_observation` / `match_current_price` carries `product_variant_id` (+ `product_id`, `match_id`); the spider prices the exact matched variant. No automatic matching. `match_current_prices` is `unique(workspace_id, match_id)`. **PASS** |
| **IV. Database-driven config** | Extraction/validation/confidence all DB-driven | The spider consumes the **cached resolved** `scrape_profiles` config (SPEC-06) for selectors/regex/`validation_rules`/`confidence_rules`/mode — no hardcoded per-domain behavior. Confidence defaults + min-accepted (0.75) come through `resolve_confidence_rules` (DB-tunable), not literals. Flush thresholds are config (`SCRAPE_FLUSH_*`). Robots policy is resolved per competitor from config. **PASS** |
| **V. Disciplined Scraping Runtime (NON-NEGOTIABLE)** | The heart of this spec | Spider runs under Scrapyd (never started in Celery). **Spiders persist only** — stops at observations/attempts/current-price; no alerts/variant-state/webhook/`price_analysis` (deferred, FR-020). **Reactor safety**: the one DB seam is sync SQLAlchemy in `deferToThread`; no blocking commit/`sleep`/Redis on the reactor; small per-process pool through PgBouncer. **Idempotent dispatch**: `schedule.json` guarded by Redis `SET NX` on a stable dispatch key (+ persisted jobid), so a retried dispatch never double-runs a batch. Browser spider explicitly out of scope. **PASS** |
| **VI. Internal-Only & Legally Compliant Access (NON-NEGOTIABLE)** | Fetch-time SSRF + auth + no-archive | Fetch-time SSRF re-resolves the host and validates the **connected IP** (deny private/loopback/link-local/unique-local/reserved/metadata) with **per-redirect-hop** re-validation, extending the save-time `validate_competitor_url` — via a custom resolver that refuses unsafe IPs (defeats DNS rebinding) + a middleware for scheme/userinfo/hop checks. `DIRECT_HTTP` internal method only; no external unlocker APIs. Scrapyd requires basic auth (unauth `schedule.json` rejected). Only extracted observations/attempts/errors are persisted — **no raw HTML or screenshots** stored (§30/§38). Competitor `legal_status`/`robots_policy` honored (robots middleware). **PASS** |
| **VII. Monetary & Extraction Correctness (NON-NEGOTIABLE)** | Prices, confidence, currency | Money is `Decimal`/`NUMERIC(18,4)` end-to-end via the existing `Money` type + `parse_money` boundary (rejects float/NaN/Infinity/over-scale/non-positive — not rounded). Confidence on every extraction (JSON-LD 0.95 / CSS 0.85 / regex 0.75 / single-number 0.40); `< min_accepted` (0.75, tunable) → `success=false` + `LOW_CONFIDENCE_PRICE`, current price **not** overwritten. `validation_rules` (min/max, `reject_if_text_contains`, old/installment/discount/shipping) enforced before accept. Currency mismatch → `comparable=false` + `CURRENCY_MISMATCH`, excluded (no FX). A bare number is not a price. **PASS** |
| **VIII. Scale-Safe Data & Concurrency (NON-NEGOTIABLE)** | Partitioning + batching + hot reads | `price_observations`/`request_attempts` **born monthly-partitioned** (partition key in PK) with initial partitions; retention = partition-drop (later). Persistence **batched** (≪ N commits per N items), never per-item; single bulk transaction per flush. Current-state reads/writes go to `match_current_prices` (hot table), never by scanning history; `observation_id`/`current_price_id` are **soft** references (no FK) tolerant of dropped partitions. All traffic through PgBouncer; `SET LOCAL`/xact-scoped only; small pool. **Out of scope but not violated**: distributed rate limiter + in-flight dedup are later specs (this MVP fixture-scale slice does not introduce hot-row contention). **PASS** |

**Technology & Security Constraints (§33/§34)**: Stack lock-in honored (Scrapy+Scrapyd, SQLAlchemy+Alembic, PostgreSQL, Redis, `scrapy-playwright` untouched). UUIDv7 PKs (§21). Scrapyd basic auth from `SCRAPYD_USERNAME`/`SCRAPYD_PASSWORD` (§33); Redis idempotency guard. Structured **error codes** from the §34 vocabulary via `scrape_core.errors` / `ScrapeErrorCode` (`HTTP_403/404/429`, `TIMEOUT`, `DNS_ERROR`, `PRICE_NOT_FOUND`, `LOW_CONFIDENCE_PRICE`, `CURRENCY_MISMATCH`, `INVALID_PRICE_FORMAT`, `BLOCKED`, `UNKNOWN_ERROR`, plus the SSRF rejection surfaced as `BLOCKED`/unsafe-URL).

**Gate result**: PASS — one **scoped deviation** documented below (FR-015 job-target-state, whose backing table is out of this slice). No principle is violated. Re-checked post-Phase-1 (end of plan): still PASS.

## Project Structure

### Documentation (this feature)

```text
specs/007-scrapyd-http-spider/
├── plan.md              # This file (/speckit-plan output)
├── research.md          # Phase 0 — 9 decisions: reactor-safe DB seam, fetch-time SSRF resolver/allowlist
│                        #   seam + redirect hops, partitioned-table convention (first in repo), extraction
│                        #   order+parsel, validation+confidence reuse, batched pipeline, robots middleware,
│                        #   Scrapyd dispatch client+idempotency, FR-015 scrape_job_targets deferral
├── data-model.md        # Phase 1 — 3 tables (exact §22 shapes), partitioning+composite PK, enums, unique
│                        #   key, soft references, RLS/isolation, ExtractionCandidate/Item transport shapes
├── quickstart.md        # Phase 1 — how to validate (unit here; live fixture scrape/partition/RLS on a full stack)
├── contracts/           # Phase 1 — interfaces this feature exposes
│   ├── spider-args.md               # generic_price_spider arguments + lifecycle + persist-only boundary
│   ├── reactor-safe-db.md           # the scrape_core.db deferToThread seam contract
│   ├── extraction.md                # ordered extractors + ExtractionCandidate + default confidences
│   ├── price-validation.md          # candidate validation rules + confidence gate + error codes
│   ├── fetch-url-safety.md          # fetch-time SSRF: resolver/allowlist seam + per-redirect-hop revalidation
│   ├── robots-middleware.md         # per-request robots_policy middleware
│   ├── persistence-pipeline.md      # batched flush (N items / T seconds) + final flush + bulk txn
│   ├── models-observations.md       # PriceObservation/RequestAttempt (partitioned) + MatchCurrentPrice
│   ├── scrapyd-dispatch.md          # authenticated schedule.json client + Redis SET NX idempotency
│   ├── errors.md                    # §34 error-code vocabulary used by this slice
│   └── migration-observations.md    # partitioned DDL + initial partitions + RLS on all three, single head
└── tasks.md             # Phase 2 (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
libs/scrape-core/scrape_core/
├── __init__.py           # (exists) — importable marker
├── db.py                 # NEW: the DECIDED-ONCE reactor-safe DB seam. run_in_thread(fn) -> Deferred via
│                         #   twisted.internet.threads.deferToThread; workspace_txn(workspace_id) context
│                         #   reusing app_shared.database.get_session + set_workspace_context. No sync
│                         #   commit on the reactor; small per-process pool through PgBouncer.
├── items.py              # NEW: ScrapeResult dataclass/Item carrying observation + attempt fields.
├── errors.py             # NEW: ScrapeErrorCode usage constants (§34) + helpers to classify fetch failures.
├── validation.py         # NEW (pure): validate_candidate(candidate, rules, confidence_cfg) -> Accepted |
│                         #   Rejected(error_code). Reuses app_shared.money.parse_money + resolve_confidence_rules.
├── extraction/
│   ├── __init__.py       # NEW
│   ├── result.py         # NEW: ExtractionCandidate dataclass (raw_price_text, currency, method, confidence,
│   │                     #   selector_used, raw_title, stock, matched_text).
│   ├── jsonld.py         # NEW (pure): parse <script type=application/ld+json> Product/Offer -> candidate (0.95).
│   ├── css.py            # NEW (pure): parsel CSS price/old_price/currency/stock/title selectors (0.85).
│   ├── regex.py          # NEW (pure): DB regex rules -> candidate (0.75); single-number heuristic (0.40).
│   └── pipeline.py       # NEW (pure): extract(html, profile) tries jsonld -> css -> regex in order, first hit
│                         #   wins, else PRICE_NOT_FOUND.
├── safety/
│   ├── __init__.py       # NEW
│   ├── fetch.py          # NEW: validate_resolved_target(url, *, resolver, allowlist=None) — runs the save-time
│   │                     #   scheme/userinfo/literal checks (app_shared.url_safety) then resolves + rejects any
│   │                     #   unsafe resolved IP (reuse _reject_ip). Injectable resolver + allowlist seam.
│   ├── resolver.py       # NEW (twisted): SafeResolver wrapping the reactor resolver — resolves then refuses to
│   │                     #   return an unsafe IP (defeats DNS rebinding at connect time). Installed via settings.
│   └── middleware.py     # NEW (scrapy): SsrfGuardMiddleware.process_request re-validates scheme/userinfo and
│                         #   process_response/redirect re-validates EVERY hop; rejects -> flagged failure.
├── robots.py             # NEW (scrapy): RobotsPolicyMiddleware — per-request robots_policy from loaded config
│                         #   (RESPECT / REVIEW_REQUIRED / IGNORE_AFTER_APPROVAL); NOT ROBOTSTXT_OBEY.
└── pipelines.py          # NEW (scrapy+twisted): BatchedPersistencePipeline — buffer; flush every N items or T
                          #   seconds (LoopingCall) + final flush at close_spider; each flush one deferToThread
                          #   bulk txn (insert observations+attempts, upsert match_current_prices).

libs/shared/app_shared/
├── enums.py              # EXTEND: AccessMethod (DIRECT_HTTP/DIRECT_HTTP_RETRY/PROXY_HTTP/PLAYWRIGHT_PROXY),
│                         #   StockStatus (IN_STOCK/OUT_OF_STOCK/UNKNOWN), ExtractionMethod (JSON_LD/CSS/REGEX/
│                         #   SINGLE_NUMBER…), ScrapeErrorCode (§34 vocabulary). All StrEnum -> VARCHAR.
├── config.py             # EXTEND: SCRAPE_FLUSH_MAX_ITEMS=50, SCRAPE_FLUSH_INTERVAL_SECONDS=2.0.
├── repository.py         # EXTEND: add PriceObservation, RequestAttempt, MatchCurrentPrice to
│                         #   WORKSPACE_OWNED_MODELS (ModelT bound already Base).
├── models/
│   ├── __init__.py       # EXTEND: re-export the three new models (Base.metadata visibility for Alembic).
│   └── observations.py   # NEW: PriceObservation + RequestAttempt (partitioned, composite PK incl. partition
│                         #   key, __table_args__ postgresql_partition_by) + MatchCurrentPrice
│                         #   (unique(workspace_id, match_id)). All WorkspaceScopedBase; money via Money type;
│                         #   confidence NUMERIC(5,4); currency CHAR(3); soft observation_id (no FK).
└── scrapyd/
    ├── __init__.py       # NEW
    └── client.py         # NEW (requests): ScrapydDispatchClient.schedule(project, spider, args) -> jobid with
                          #   HTTP basic auth (SCRAPYD_USERNAME/PASSWORD, SCRAPYD_HTTP_URLS); dispatch_key(job,
                          #   batch) + Redis SET NX guard so a retried schedule never double-runs. No scrapy/twisted.

apps/scrapers/price_monitor/
├── settings.py           # EXTEND: ROBOTSTXT_OBEY=False; DNS_RESOLVER=scrape_core.safety.resolver.SafeResolver;
│                         #   DOWNLOADER_MIDDLEWARES += SsrfGuardMiddleware + RobotsPolicyMiddleware;
│                         #   ITEM_PIPELINES += BatchedPersistencePipeline; small pool; flush knobs from Settings.
└── spiders/
    └── generic_price_spider.py  # NEW: parse args; load matches scoped to workspace_id; consume cached resolved
                          #   profile; yield DIRECT_HTTP requests; extraction+validation in parse; yield ScrapeResult
                          #   items (success + failure); persist-only (no price_analysis emission).

apps/workers/app/workers/
└── tasks_dispatch.py     # NEW: thin Celery task dispatch_generic_price_spider(workspace_id, scrape_job_id,
                          #   match_ids, mode, batch_index) delegating to app_shared.scrapyd.client (auth+idempotent).

alembic/versions/
└── <rev>_observations_current_prices_tables.py  # NEW: create price_observations + request_attempts as
                          #   PARTITION BY RANGE (partition key in PK) + current+next month partitions; create
                          #   match_current_prices (unique(workspace_id, match_id)); emit_rls_policy on all three
                          #   (parent partitioned tables — propagates to partitions); downgrade drops partitions
                          #   then parents then current-prices; down_revision = a4f205e8d7de (current head).

tests/unit/
├── test_import_boundaries.py         # EXTEND: cover scrape_core.* ; assert app_shared.models.observations +
│                                     #   app_shared.scrapyd.client import NO scrapy/twisted/fastapi.
├── test_observations_models.py       # NEW: table/column shapes, composite PK incl. partition key, partition_by
│                                     #   option, unique(workspace_id, match_id), Money/NUMERIC(5,4)/CHAR(3), enums.
├── test_rls_observations.py          # NEW: emit_rls_policy render for all three (fail-closed DDL).
├── test_migration_offline_observations.py  # NEW: `alembic upgrade head --sql` renders PARTITION BY + initial
│                                     #   partitions + RLS on all three; single head.
├── test_extraction_jsonld_css_regex.py     # NEW: fixture-HTML corpus — each method extracts w/ expected default
│                                     #   confidence; fallback order jsonld->css->regex; PRICE_NOT_FOUND; single-number 0.40.
├── test_price_validation.py          # NEW: Decimal/>0/finite/scale; currency match vs CURRENCY_MISMATCH;
│                                     #   min/max; reject_if_text_contains (save/discount/installment/shipping);
│                                     #   confidence < 0.75 -> LOW_CONFIDENCE_PRICE; never rounds.
├── test_fetch_url_safety.py          # NEW: validate_resolved_target — injected public IP accepted; private/
│                                     #   loopback/link-local/unique-local/metadata resolved IP rejected; each
│                                     #   redirect hop re-validated; scheme/userinfo rejected pre-fetch; prod path
│                                     #   (no allowlist) uses real resolver seam.
├── test_persistence_batching.py      # NEW: flush at N items and at T seconds and final flush at close; N items ->
│                                     #   ≪ N flushes; buffer emptied; DB call routed through the deferToThread seam (mocked).
├── test_reactor_safe_db.py           # NEW: db.run_in_thread returns a Deferred / offloads (no blocking call on
│                                     #   the calling thread); workspace_txn sets context.
├── test_robots_middleware.py         # NEW: RESPECT skips a disallowed path; IGNORE_AFTER_APPROVAL fetches;
│                                     #   policy read per-request from config (not global).
├── test_scrapyd_dispatch.py          # NEW: schedule() sends basic auth + args -> jobid; missing/wrong creds 401 ->
│                                     #   no schedule; SET NX guard: second dispatch of same key is a no-op.
└── test_observations_scoping_guard.py# NEW: CI guard flags a planted unscoped select on the three new models.

tests/integration/  (authored, live-stack-marked — skipped without Postgres/Redis/Scrapyd)
├── test_spider_jsonld_fixture_live.py     # seed ws/product/variant/competitor/match/profile; serve JSON-LD
│                                          #   fixture; run spider; assert 1 success observation + match_current_prices
│                                          #   upsert + 1 request_attempt; workspace-scoped.
├── test_spider_ssrf_live.py               # match URL resolving to private IP / 302->internal: refused pre-body;
│                                          #   no success observation; failure recorded.
├── test_spider_strategies_live.py         # CSS-only and regex-only fixtures -> expected method+confidence;
│                                          #   discount/"save X"-only fixture rejected.
├── test_spider_batch_live.py              # N fixture matches -> all N observations, commit count ≪ N.
├── test_observations_isolation_live.py    # cross-workspace blocked (app + RLS); no-context -> 0 rows.
└── test_dispatch_scrapyd_live.py          # authenticated schedule.json returns jobid; unauth rejected; retried
                                           #   dispatch does not double-run.
```

**Structure Decision**: Backend monorepo (uv workspace), matching SPEC-02→06. This is the first substantive build-out of `libs/scrape-core`: **all** reusable scraping-side code (reactor-safe DB seam, extraction, validation, fetch-time SSRF, robots, batched persistence, items, errors) lands there so every DB/reactor/network-independent piece is unit-testable with `parsel`/stdlib only, and the `apps/scrapers` Scrapy project is a thin packaging + spider + settings layer over it. The three ORM models + the authenticated Scrapyd dispatch client live in `app_shared` (which already owns the money type, url_safety, database seam, redis client, and `SCRAPYD_*`/config), keeping `app_shared` scrapy/twisted-free. The three tables + RLS land in one repo-root Alembic migration chained onto head `a4f205e8d7de`.

## Complexity Tracking

> One scoped deviation (a MUST whose backing table is out of this slice), justified below. No Constitution Check violation.

| Item | Why / Decision | Simpler / stricter alternative rejected because |
|------|----------------|-------------------------------------------------|
| **FR-015 "update scrape job target state"** treated as a **deferred seam**, not implemented against a table | `scrape_jobs`/`scrape_job_targets` (§22) are owned by the later **job-orchestration** spec; the spec's own Assumptions defer "job orchestration (dispatch of jobs, batching into Scrapyd calls)", and the master doc's resolved decision #5 enumerates the new tables created here as exactly `price_observations`, `request_attempts`, `match_current_prices` — `scrape_job_targets` is not among them and is not partitioned. `scrape_job_id` is a passed-in correlation UUID stored as a **nullable soft reference** on observations/attempts (matching §22's nullable `scrape_job_id`). The spider records each match's terminal outcome (via `request_attempts.success` + `price_observations.success`), which is exactly the data a job-target updater consumes; wiring the actual `scrape_job_targets` row write activates when that table exists. | Creating `scrape_jobs`/`scrape_job_targets` here would pull the whole job lifecycle/state machine + `unique(scrape_job_id, match_id)` + parent-counter aggregation (§21/§26) into a slice explicitly scoped to "prove the spider path", contradicting resolved decision #5's table enumeration and the spec Assumptions. Silently dropping the MUST was rejected — it is documented here and surfaced as a named seam. |
| **First partitioned tables in the repo** (`postgresql_partition_by` + composite PK + raw-DDL partitions in the migration) | §22/§29 mandate append-heavy tables born monthly-partitioned with the partition key in the PK; no prior spec created one, so this spec establishes the convention (parent `PARTITION BY RANGE`, `PRIMARY KEY (id, <partition_col>)`, initial current+next month `PARTITION OF` tables, RLS on the parent). | A non-partitioned table "for now" was rejected: §29 requires partitioned **from the first real-data load**, and this is that load; retrofitting partitioning later requires a table rewrite. |
