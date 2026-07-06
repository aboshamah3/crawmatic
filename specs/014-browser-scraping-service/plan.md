# Implementation Plan: Browser Scraping Service

**Branch**: `014-browser-scraping-service` | **Date**: 2026-07-06 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/014-browser-scraping-service/spec.md`

## Summary

Deliver the internal JavaScript-rendering scrape path: a second, already-scaffolded Scrapyd service
(`price_monitor_browser`, `apps/scrapers-browser`, Chromium baked in, `max_proc=1`, low
`CONCURRENT_REQUESTS`/`PLAYWRIGHT_MAX_CONTEXTS`) running a new `generic_browser_price_spider` that
loads a competitor product page in a real browser via `scrapy-playwright`, waits for the profile's
`wait_for_selector` (bounded by `browser_timeout_ms` or a safe default), optionally performs an
allowlisted `variant_selector_config` interaction, optionally routes the browser context through the
internal proxy pool when the resolved access policy assigns `PLAYWRIGHT_PROXY`, then extracts,
validates and persists exactly as the HTTP spider does — reusing SPEC-07 extraction/validation/
persistence, SPEC-10 proxy assignment + attempt logging, SPEC-11 locks + rate limiting, and the
SPEC-09 `price_analysis` handoff, all through the **same** `BatchedPersistencePipeline`.

The central structural move is **extracting the transport-agnostic spider machinery**
(`SpiderTarget`, bounded `load_targets`, `_prepare_dispatch`, permission/lock acquisition, cache
helpers, `_build_result`) out of `apps/scrapers/price_monitor/spiders/generic_price_spider.py` into
`libs/scrape-core` so both Scrapy projects import it — mandated by Constitution Principle I
(`apps` may not import another `apps`; shared Scrapy code lives in `scrape-core`). The HTTP spider is
refactored to consume the extracted module with **behavior preserved** (guarded by its existing
unit + integration suite). The browser spider adds only its transport-specific surface: Playwright
request meta (`playwright`, `playwright_page_methods`, per-target proxied `playwright_context`), the
wait/variant interaction, a browser error classifier, and a `PLAYWRIGHT_ABORT_REQUEST` SSRF guard
that re-validates every navigation hop's resolved IP (because `scrapy-playwright` does **not** use
Scrapy's `DNS_RESOLVER`, so the existing `SafeResolver` alone does not cover browser navigations).

Finally, dispatch is corrected (FR-015/016): `apps/workers/app/workers/tasks_jobs.py` already routes
BROWSER batches to `SCRAPYD_BROWSER_URLS` but schedules the HTTP project/spider for **every** batch —
a surgical change selects `(price_monitor_browser, generic_browser_price_spider)` for BROWSER batches
and `(price_monitor, generic_price_spider)` otherwise, in both `dispatch_job` and
`recover_stalled_batches`, leaving deterministic node selection and the `dispatched:{job}:{batch}`
idempotency guard untouched.

No new persistent schema: the browser fields (`mode`, `wait_for_selector`, `browser_timeout_ms`,
`variant_selector_config`) already exist from SPEC-06, and `access_method` is a plain
app-validated `VARCHAR`, so browser attempts record the existing `PLAYWRIGHT_PROXY` method (with
`proxy_provider_id`/`proxy_country` populated only when actually proxied) — no migration, no new
error codes, no new `AccessMethod` member.

Full rationale in [research.md](./research.md); the three plan-mandated design decisions
(`variant_selector_config` shape, browser error-code names, browser retry semantics) plus the SSRF
and shared-code decisions are resolved there; entity/field detail in [data-model.md](./data-model.md);
behavior in [contracts/](./contracts/); validation in [quickstart.md](./quickstart.md).

## Technical Context

**Language/Version**: Python 3.13 (uv workspace, `requires-python >=3.13,<3.14`).

**Primary Dependencies**: Scrapy 2.13 + Scrapyd 1.5, `scrapy-playwright >=0.0.43` + `playwright >=1.48`
(browser project only), Twisted `AsyncioSelectorReactor`, SQLAlchemy 2.0 (sync `Session` off-reactor),
Redis (locks, rate limits, resolution caches, dispatch idempotency), Celery (dispatch +
`price_analysis` handoff). `app_shared` stays scraping-free (no Scrapy/Twisted/Playwright import);
`scrape-core` may depend on `app_shared`, never the reverse.

**Storage**: PostgreSQL via PgBouncer (transaction pooling), small per-process pool. Reuses
`price_observations`, `request_attempts`, `match_current_prices`, `scrape_job_targets`,
`scrape_jobs`, `scrape_profiles`, `competitor_product_matches`, `domain_strategy_profiles`. **No new
table, no migration** (FR-017).

**Testing**: pytest. Unit tests (no reactor, no DB): the extracted `scrape-core` loader/decision
module (behavior-parity with today's HTTP spider), the browser error classifier, the
`variant_selector_config` parser → `PageMethod` translation (allowlist + `value_from` resolution),
the dispatch project/spider selector. Reactor/live tests (`*_live.py`, `skipif` DB/Chromium probe):
a JS-fixture page rendering its price only after `wait_for_selector`; a variant-selection fixture;
an SSRF fixture (private host + 302→internal) aborted before body; a proxied-context attempt logging
`PLAYWRIGHT_PROXY`. The existing HTTP spider suite (`tests/unit/test_generic_price_spider.py`,
`tests/integration/test_spider_*`) is the **regression guard** for the extraction refactor and MUST
stay green.

**Target Platform**: Linux, multi-service deploy (Railway or similar). New spider runs in
`scrapyd-browser-service` (`apps/scrapers-browser`, own image with Chromium); dispatch fix runs in
`worker-service`; no `api-service`/frontend change.

**Project Type**: Backend monorepo (uv workspace: `apps/*` + `libs/*`). No frontend (v1 backend-only).

**Performance Goals**: 2k products / 10k–20k matches per workspace (Principle VIII). Browser is the
*selective* path (only `mode=BROWSER` targets), deliberately low concurrency (`max_proc=1`,
`CONCURRENT_REQUESTS`/`PLAYWRIGHT_MAX_CONTEXTS` small). Persistence stays batched + off-reactor
(reused pipeline); dispatch batches by `(domain, mode)` unchanged.

**Constraints**: reactor safety (no sync DB/Redis on the reactor thread — every blocking call via
`run_in_thread`/async); bounded every navigation + wait (`browser_timeout_ms` or safe default) so no
page holds a scarce slot indefinitely; SSRF re-validated on every navigation hop against the resolved
IP; per-competitor robots policy; batched off-reactor persistence; idempotent dispatch; internal
access methods only (no external unlocker/stealth/anti-bot); proxy password never logged; DB-driven
config (browser timeout default, browser concurrency knobs read from `Settings`, not hardcoded).

**Scale/Scope**: 1 new spider (`generic_browser_price_spider`); 1 shared `scrape-core` extraction
(new `scrape_core/targets.py` + `scrape_core/browser/` for the browser-specific SSRF/variant/error
helpers) + a behavior-preserving HTTP-spider refactor; browser project `settings.py` completion
(pipeline, middlewares, Playwright wiring, config-driven concurrency + nav timeout); 1 surgical
dispatch-routing fix (2 call sites); ~2–3 new `Settings` knobs; **0 migrations, 0 new error codes,
0 new enum members**.

## Constitution Check

*GATE: evaluated against `.specify/memory/constitution.md` v1.0.1.*

| Principle | Assessment |
|---|---|
| I. API-First / Service-Oriented / shared Scrapy code in `scrape-core` | **PASS (embodies it).** The browser service is its own deployment (`apps/scrapers-browser`, `price_monitor_browser`, own image + node pool). Shared spider machinery is **extracted into `libs/scrape-core`** so the HTTP and browser projects differ only in download handler + spider entrypoint (§5) — resolving the `apps→apps` import barrier. `app_shared` gains no Scrapy/Twisted/Playwright import; `scrape-core` gains the Playwright-facing code (it may depend on Scrapy/Playwright). Import-boundary tests stay green. |
| II. Workspace Isolation (NON-NEGOTIABLE) | **PASS.** Browser spider loads matches via the reused `scoped_select(CompetitorProductMatch, workspace_id)`; a match outside the workspace is simply absent (FR-002). No new query path; the extracted `load_targets` keeps every `workspace_id` predicate + `set_workspace_context`. |
| III. Variant-Level Pricing & Explicit Matching | **PASS.** Persists at variant level via the reused pipeline; `variant_selector_config` selects the **match's** variant (keyed off `competitor_variant_options`/`_identifier`/`_sku`) so the observation is the correct variant's price. |
| IV. Database-Driven Configuration | **PASS.** Wait/variant/timeout all come from the resolved `scrape_profiles` row (Redis-cached chain, reused). Browser concurrency + default nav timeout are `Settings` knobs read at settings-build time, never hardcoded literals in spider logic. |
| V. Disciplined Scraping Runtime (NON-NEGOTIABLE) | **PASS.** Runs under Scrapyd, never in a Celery process. Spider persists then stops; alerts/variant-state/webhooks stay in the reused `price_analysis` task (one per variant per job, deduped in the pipeline). Reactor-safe (all DB/Redis off-reactor); every browser nav/wait is bounded; dispatch stays idempotent (`SET NX` guard unchanged). Browser is the selective fallback, concurrency low (§8/§14). |
| VI. Internal-Only & Legally Compliant Access (NON-NEGOTIABLE) | **PASS.** Only the four locked access methods; browser uses `PLAYWRIGHT_PROXY` (proxied) or an unproxied browser fetch still recorded under `PLAYWRIGHT_PROXY` (no new method). No external unlocker, no stealth/anti-bot, no login/CAPTCHA/paywall bypass (FR-019). Public product pages only; no raw HTML/screenshot stored. SSRF + robots re-enforced. |
| VII. Monetary & Extraction Correctness | **PASS.** Extraction/validation/confidence/currency-mismatch all reused verbatim against the rendered DOM — `Decimal` money, no floats, confidence bar honored. |
| VIII. Scale-Safe Data & Concurrency | **PASS.** Distributed rate limits + in-flight match locks reused (mode-sized `MATCH_LOCK_BROWSER_TTL_SECONDS` already exists). Batched off-reactor persistence via the shared pipeline (far fewer than N commits). Deterministic node selection + no hot-row counters unchanged. |
| Tech & Security constraints | **PASS.** Structured error codes reused (§34 vocabulary — **no new codes**). Proxy password decrypted once off-reactor, never logged, never embedded in `request.meta`/context logs. `access_method` is `VARCHAR` (no native-enum migration). |
| Workflow / scope discipline | **PASS.** Correctly sequenced (browser service after MVP + jobs + access + locks). No forbidden v1 scope (no frontend/billing/auto-match/external API/stealth). Browser is NOT the default path — only `mode=BROWSER` routes here. |

**Gate result: PASS** — no violations, no Complexity Tracking entries required. The one notable move
(extracting shared spider code into `scrape-core` and refactoring the HTTP spider) *satisfies*
Principle I rather than straining it; it is behavior-preserving and regression-guarded. Re-checked
after Phase 1 design — unchanged.

## Project Structure

### Documentation (this feature)

```text
specs/014-browser-scraping-service/
├── plan.md              # This file
├── research.md          # Phase 0 — decisions R1–R12 (resolves the 3 open items + SSRF/shared-code/proxy)
├── data-model.md        # Phase 1 — no new tables; variant_selector_config shape; access_method semantics
├── quickstart.md        # Phase 1 — validation scenarios (JS render, variant, SSRF, proxy, dispatch)
├── contracts/
│   ├── browser-spider.md      # US1/US4 — generic_browser_price_spider lifecycle, wait, persistence, errors
│   ├── variant-selection.md   # US3 — variant_selector_config JSON shape + allowlisted PageMethod translation
│   ├── browser-safety.md      # US4 — SSRF (PLAYWRIGHT_ABORT_REQUEST per-hop), robots, proxy context, reactor
│   ├── dispatch-routing.md    # US2 — BROWSER→(price_monitor_browser, generic_browser_price_spider) fix
│   └── shared-extraction.md   # scrape-core extraction: what moves, behavior-preservation contract
├── spec.md
├── autospec-decisions.md
└── tasks.md             # (later, /speckit-tasks)
```

### Source Code (repository root)

```text
libs/scrape-core/scrape_core/
├── targets.py                 # NEW — extracted transport-agnostic loader/decision machinery:
│                              #   SpiderTarget, _LoadedTargets, load_targets, _prepare_dispatch,
│                              #   _DispatchDecision, cache get/set helpers, VisibleProviders,
│                              #   _parse_match_ids, _parse_host_port, _attempt_kwargs_from_meta,
│                              #   _elapsed_ms, _RequeueState  (moved from the HTTP spider module)
├── result_builder.py          # NEW — extracted build_scrape_result(...) (was `_build_result`), shared
├── errors.py                  # EDIT — add classify_playwright_exception (Playwright TimeoutError→TIMEOUT,
│                              #   other Playwright errors→PLAYWRIGHT_FAILED); reuse existing codes only
├── browser/
│   ├── __init__.py            # NEW
│   ├── ssrf.py                # NEW — PLAYWRIGHT_ABORT_REQUEST guard: per-navigation-hop resolved-IP
│   │                          #   re-validation via reused scrape_core.safety.fetch.validate_resolved_target
│   ├── variant.py             # NEW — parse variant_selector_config → ordered PageMethod list (allowlist,
│   │                          #   value_from resolution); raises a typed VariantConfigError on bad shape
│   └── page.py                # NEW — build the Playwright meta (wait_for_selector PageMethod, proxied
│                              #   context kwargs, nav timeout) for one target
└── pipelines.py / items.py / limiter.py / robots.py / safety/* / extraction/* / validation.py
                               # UNCHANGED — reused as-is (persistence, locks, SSRF core, robots, extraction)

apps/scrapers/price_monitor/spiders/
└── generic_price_spider.py    # EDIT (refactor) — import the extracted scrape_core.targets/result_builder;
                               #   keep only HTTP-transport specifics; BEHAVIOR PRESERVED (suite green)

apps/scrapers-browser/price_monitor_browser/
├── settings.py                # EDIT — add ITEM_PIPELINES (BatchedPersistencePipeline), SsrfGuard +
│                              #   RobotsPolicy DOWNLOADER_MIDDLEWARES, DNS_RESOLVER=SafeResolver,
│                              #   ROBOTSTXT_OBEY=False (per-request robots instead), PLAYWRIGHT_ABORT_REQUEST,
│                              #   PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT + concurrency from Settings,
│                              #   SCRAPE_FLUSH_* from Settings (parity with HTTP settings.py)
└── spiders/
    └── generic_browser_price_spider.py   # NEW — GenericBrowserPriceSpider(scrapy.Spider):
                               #   start()/parse()/errback() reusing scrape_core.targets + result_builder;
                               #   yields Playwright requests (wait + variant PageMethods); single browser
                               #   attempt per target; classify_playwright_exception on failure

libs/shared/app_shared/
├── config.py                  # EDIT — add BROWSER_CONCURRENT_REQUESTS, BROWSER_MAX_CONTEXTS,
│                              #   SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS (env/DB-tunable, Principle IV)
└── (models/enums UNCHANGED — no new fields, no new AccessMethod/ScrapeErrorCode members)

apps/workers/app/workers/
└── tasks_jobs.py              # EDIT — mode-driven (project, spider) selection in dispatch_job +
                               #   recover_stalled_batches (FR-015/016); node selection + idempotency unchanged
```

**Structure Decision**: Backend monorepo. The browser deployment scaffold (`apps/scrapers-browser`
image, `scrapyd.conf` `max_proc=1`, entrypoint, `SCRAPYD_BROWSER_URLS`) already exists from SPEC-01;
this feature completes its `settings.py`, adds the one spider, extracts the shared machinery into
`libs/scrape-core` (Principle I), and corrects the dispatch routing in `apps/workers`.

## Complexity Tracking

> No Constitution Check violations — this section is intentionally empty. The shared-code extraction
> is not a deviation; it is the Principle-I-mandated placement of code shared by both Scrapy projects,
> performed behavior-preservingly under the existing HTTP-spider test suite.
