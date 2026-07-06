# Tasks: Browser Scraping Service

**Input**: Design documents from `/specs/014-browser-scraping-service/`

**Prerequisites**: plan.md, spec.md, research.md (R1–R12), data-model.md, contracts/ (browser-spider,
variant-selection, browser-safety, dispatch-routing, shared-extraction), quickstart.md,
autospec-decisions.md — all read and binding.

**Tests**: INCLUDED. The plan (Technical Context → Testing) and quickstart.md explicitly request them.
Two kinds: (a) **unit** tests that run everywhere in this env (no reactor/DB/Chromium) and MUST pass;
(b) **live** tests authored as `*_live.py` with a `skipif` probe (Postgres/Redis/Chromium) that skip
cleanly here — this build env has no container engine. The existing HTTP-spider suite
(`tests/unit/test_generic_price_spider.py`, `tests/integration/test_spider_*`) is the **regression
guard** for the extraction refactor and MUST stay green.

**Organization**: Tasks are grouped by user story. Setup + Foundational block all stories. The
shared-code extraction (Phase 2) is the load-bearing prerequisite — Constitution Principle I forbids
`apps/scrapers-browser` importing `apps/scrapers`, so both spiders must consume `libs/scrape-core`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1 / US2 / US3 / US4 (maps to spec.md user stories)
- All paths are repo-relative from `/srv/crawmatic/crawmatic/`

## Reuse posture (applies to every task)

Reuse SPEC-07 extraction/validation/persistence (`BatchedPersistencePipeline`), SPEC-10 proxy
assignment + attempt logging + `PLAYWRIGHT_PROXY`, SPEC-11 locks + rate limiting, SPEC-08
dispatch/batching/node-selection/idempotency, SPEC-09 `price_analysis` handoff — **as-is**. No new
migration, no new `AccessMethod`/`ScrapeErrorCode` member. Error codes come from the locked §34
vocabulary only (`TIMEOUT`, `PLAYWRIGHT_FAILED`, `VARIANT_NOT_FOUND`, `SELECTOR_BROKEN`, `BLOCKED`,
`PROXY_FAILED`, `PRICE_NOT_FOUND`, …). `app_shared` stays scraping-free (no Scrapy/Twisted/Playwright
import); `scrape_core` may depend on `app_shared`, never the reverse.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: DB/env-tunable configuration knobs (Principle IV) and workspace dependency sanity.

- [X] T001 [P] Add three env/DB-tunable knobs to `libs/shared/app_shared/config.py` on the `Settings`
  model: `SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS: int = 30000`, `BROWSER_CONCURRENT_REQUESTS: int = 2`,
  `BROWSER_MAX_CONTEXTS: int = 1` (data-model.md §4). Keep `app_shared` import-clean (no Scrapy/Twisted/
  Playwright). Do NOT touch existing knobs (`MATCH_LOCK_BROWSER_TTL_SECONDS`, `SCRAPE_FLUSH_*`,
  `SCRAPYD_BROWSER_URLS` already exist and are reused).
- [X] T002 [P] Add unit coverage for the three new knobs' defaults + env override in
  `tests/unit/test_config.py` (must pass in this env).
- [X] T003 Verify workspace deps resolve for the browser project: `uv sync --all-packages` (NEVER plain
  `uv sync`), and confirm `apps/scrapers-browser` already declares `scrapy-playwright`/`playwright`
  (SPEC-01 scaffold; add to its `pyproject.toml` only if genuinely absent).

**Checkpoint**: Config knobs exist and are read at settings-build time; workspace installs cleanly.

---

## Phase 2: Foundational (Blocking Prerequisites — shared-code extraction, R1)

**Purpose**: Extract the transport-agnostic spider machinery from the HTTP spider into
`libs/scrape-core` so BOTH Scrapy projects import it (Principle I). This is behavior-preserving and
guarded by the existing HTTP-spider suite. It BLOCKS every user story (both spiders depend on it).

**⚠️ CRITICAL**: No user story work can begin until this phase is complete and the regression guard
(T010) is green.

- [X] T004 Create `libs/scrape-core/scrape_core/targets.py` by **moving** (not copying) the
  transport-agnostic machinery out of `apps/scrapers/price_monitor/spiders/generic_price_spider.py`:
  `SpiderTarget`, `_LoadedTargets`, `load_targets`, `_DispatchDecision`, `_prepare_dispatch`,
  `VisibleProviders`, the Redis resolution-cache get/set helpers (`_cache_*_group_result`,
  `_cache_*_access_result`), `_parse_match_ids`, `_parse_host_port`, `_attempt_kwargs_from_meta`,
  `_elapsed_ms`, `_RequeueState` (shared-extraction.md). Import only `app_shared.*` + `scrape_core.*`.
- [X] T005 In `libs/scrape-core/scrape_core/targets.py`, extend `SpiderTarget` with the browser-relevant
  already-loaded fields — resolved `wait_for_selector`, `browser_timeout_ms`, `variant_selector_config`,
  and a slot for its resolved `value_from` values (e.g. `match_variant_values`) — all with defaults so
  existing HTTP constructors stay valid (shared-extraction.md "What moves"). `load_targets` reads these
  from the already-resolved profile/match rows; no new query.
- [X] T006 Extract the admission machinery bodies (`_acquire_fetch_permission`, `_overflow_to_dispatch`,
  and the reusable part of `_dispatch`) from the HTTP spider into
  `libs/scrape-core/scrape_core/targets.py` as functions taking a small `AdmissionContext`
  (workspace_id, scrape_job_id, `_requeue_state_by_match_id`), so both spiders share identical admission
  behavior without duplication (shared-extraction.md Note).
- [X] T007 [P] Create `libs/scrape-core/scrape_core/result_builder.py` with free function
  `build_scrape_result(...)` (formerly the spider's `_build_result`) constructing the identical
  `ScrapeResult` from target + attempt fields (shared-extraction.md).
- [X] T008 [P] Edit `libs/scrape-core/scrape_core/errors.py`: add
  `classify_playwright_exception(exc)` mapping Playwright `TimeoutError` → `TIMEOUT` and any other
  Playwright error → `PLAYWRIGHT_FAILED` (R3). Reuse existing `ScrapeErrorCode` members only — add none.
- [X] T009 [P] Create `libs/scrape-core/scrape_core/browser/__init__.py` (empty package marker) so
  `scrape_core.browser.*` exists for later modules.
- [X] T010 Refactor `apps/scrapers/price_monitor/spiders/generic_price_spider.py` to import the moved
  helpers from `scrape_core.targets` / `scrape_core.result_builder`; keep only HTTP-transport specifics
  (`_request_for` with `Proxy-Authorization`, the multi-attempt `parse`/`errback`/`_dispatch` ladder as
  thin wrappers over the shared admission functions). **Behavior preserved.**
- [X] T011 Run the regression guard and make it green: `uv run pytest
  tests/unit/test_generic_price_spider.py tests/integration/test_spider_access.py -q` plus the
  import-boundary test — repoint imports to moved symbols if a test reaches a moved private symbol;
  assertions unchanged (shared-extraction.md behavior-preservation contract).
- [X] T012 [P] Add a unit test for `classify_playwright_exception` (Playwright `TimeoutError` →
  `TIMEOUT`; generic Playwright error → `PLAYWRIGHT_FAILED`) in
  `tests/unit/test_browser_errors.py` (quickstart scenario 3; must pass in this env).

**Checkpoint**: Shared machinery lives in `scrape-core`, HTTP spider consumes it with its suite green,
browser package scaffolded, error classifier ready. User stories can now begin.

---

## Phase 3: User Story 1 - A JavaScript-rendered product page yields a price (Priority: P1) 🎯 MVP

**Goal**: A browser-mode match whose price is injected by JS is rendered in a real browser, waits for
the configured selector, then extracts + persists exactly one observation / current-price / attempt and
emits one `price_analysis` task — where the HTTP spider produced nothing.

**Independent Test**: Schedule `generic_browser_price_spider` against a browser-mode match whose fixture
page renders its price only after JS (with `wait_for_selector`); assert exactly one `PriceObservation`
(valid Decimal), one `MatchCurrentPrice`, one `RequestAttempt` (`access_method=PLAYWRIGHT_PROXY`), and
one `PRICE_ANALYSIS_RECOMPUTE` enqueued (quickstart scenarios 5–7).

- [X] T013 [US1] Create `libs/scrape-core/scrape_core/browser/page.py` with
  `effective_timeout(target, settings)` (= `target.browser_timeout_ms` if set else
  `Settings.SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS`, R10) and `build_page_methods(target)` returning the
  ordered `scrapy_playwright.page.PageMethod` list — for US1 the profile `wait_for_selector` (if set) as
  a `wait_for_selector` PageMethod carrying the effective timeout (browser-spider.md
  `_browser_request_for`; variant/settle steps are appended in US3). **When `wait_for_selector` is
  unset** (spec FR-003 / Edge Cases "no wait_for_selector"), express the "normal load/network settle"
  default explicitly — append a `wait_for_load_state("networkidle")` PageMethod (bounded by the effective
  timeout) rather than relying solely on the goto load event — so extraction always runs against a
  settled rendered DOM.
- [X] T014 [US1] Complete `apps/scrapers-browser/price_monitor_browser/settings.py` for shared-runtime
  parity (R9): `ITEM_PIPELINES = {BatchedPersistencePipeline: 300}`; keep the scrapy-playwright
  `DOWNLOAD_HANDLERS` + `AsyncioSelectorReactor`; set `PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT`,
  `CONCURRENT_REQUESTS = BROWSER_CONCURRENT_REQUESTS`, `PLAYWRIGHT_MAX_CONTEXTS = BROWSER_MAX_CONTEXTS`,
  and `SCRAPE_FLUSH_MAX_ITEMS`/`SCRAPE_FLUSH_INTERVAL_SECONDS` all read from `get_settings()` (no
  hardcoded literals); `ROBOTSTXT_OBEY = False` with `RobotsPolicyMiddleware` in
  `DOWNLOADER_MIDDLEWARES` (priority 110). (SSRF middleware + DNS_RESOLVER + PLAYWRIGHT_ABORT_REQUEST
  are added in T030/T031 — a **required MVP safety gate** per Constitution §VI, NON-NEGOTIABLE; the
  browser path MUST NOT be dispatched in production until T030/T031 land, even though they are
  authored in the US4 phase.)
- [X] T015 [US1] Create `apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py`
  → `GenericBrowserPriceSpider(scrapy.Spider)`, `name = "generic_browser_price_spider"`. Accept the same
  args as the HTTP spider (`workspace_id`, `scrape_job_id`, `match_ids`, `mode`) via shared
  `_parse_match_ids` (FR-002). Implement async `start()`: `run_in_thread(load_targets, ...)` off-reactor,
  then per target `run_in_thread(_prepare_dispatch, target, 1, ...)` for the single transport decision +
  ceiling/cooldown/budget gate (skip → emit the skip `ScrapeResult` exactly as HTTP), acquire shared
  admission + in-flight match lock (`acquire_lock(mode=PLAYWRIGHT_PROXY)`,
  `MATCH_LOCK_BROWSER_TTL_SECONDS`; held → `SKIPPED`/`LOCKED_ALREADY_RUNNING`), then `yield` **one**
  Playwright request (R4 — no `_dispatch` retry loop) (browser-spider.md `start()`).
- [X] T016 [US1] In the same spider, implement `_browser_request_for(target, plan, perm, lock)` building
  the Playwright `scrapy.Request`: `meta["playwright"]=True`, `meta["playwright_include_page"]=False`
  (handler auto-closes the page — no leak), `meta["playwright_page_methods"]=build_page_methods(target)`,
  the shared meta keys (`match_id`/`robots_policy`/`access_method=PLAYWRIGHT_PROXY`/`attempt_number=1`/
  proxy-audit/`match_lock_*`/`semaphore_*` stamped exactly as HTTP), goto bounded by the effective
  timeout, `callback=self.parse`, `errback=self.errback`, `dont_filter=True`. **Direct/default context
  only** in US1 (proxied context branch is added in US4/T025).
- [X] T017 [US1] Implement async `parse(response)` (browser-spider.md): release the concurrency slot
  (shared), `_attempt_kwargs_from_meta(response.meta)`, `classify_http_status(response.status)` →
  failed result; else reuse `extract(response.text, ...)` on the **rendered** DOM (`None` →
  `PRICE_NOT_FOUND`) then `validate_candidate(...)` (`Rejected` → its code; `Accepted` → success). Every
  yield via `build_scrape_result(..., access_method=PLAYWRIGHT_PROXY)` so the reused pipeline persists +
  terminalizes the target + enqueues one `price_analysis` per variant (FR-006, R11) — no
  alert/variant-state/webhook here.
- [X] T018 [US1] Implement async `errback(failure)` for the single-attempt, no-retry path (R4): release
  the slot, compute `error_code` via a `classify_browser_failure(failure, hostname)` helper — for US1
  wire `classify_playwright_exception` (`TimeoutError`→`TIMEOUT`, else `PLAYWRIGHT_FAILED`); yield one
  failed `build_scrape_result(success=False, error_code=...)` and **stop** (no next attempt). (SSRF/
  robots `BLOCKED` and variant `VARIANT_NOT_FOUND`/`SELECTOR_BROKEN` branches are completed in US3/US4.)
- [X] T019 [P] [US1] Live test `tests/integration/test_browser_spider_render_live.py` (`skipif`
  Chromium+DB): JS-injected-price fixture + `wait_for_selector` → assert one valid-Decimal
  `PriceObservation`, one `MatchCurrentPrice`, one `RequestAttempt` (`PLAYWRIGHT_PROXY`), one
  `PRICE_ANALYSIS_RECOMPUTE` (SC-001, US1 AS1/AS3); a static-server-HTML-price fixture still extracts
  (US1 AS4).
- [X] T020 [P] [US1] Live test `tests/integration/test_browser_spider_timeout_live.py` (`skipif`):
  `wait_for_selector` never appears within a small `browser_timeout_ms` → exactly one **failed**
  `RequestAttempt` `error_code=TIMEOUT`, no priced observation, bounded (no hang), page/context released
  (US1 AS2, edge cases).

**Checkpoint**: Browser MVP works end to end against a scheduled browser-mode match. Independently
testable (given a browser Scrapyd node); still needs US2 to be reachable through production dispatch.

---

## Phase 4: User Story 2 - Only browser-mode work reaches the separate browser service (Priority: P1)

**Goal**: Dispatch routes BROWSER batches to the browser project + `generic_browser_price_spider` on the
browser node pool, and HTTP batches to the HTTP spider on the HTTP pool — a batch carries exactly one
mode; retried dispatch never double-runs.

**Independent Test**: Dispatch a mixed HTTP/BROWSER job; assert BROWSER batches schedule
`(price_monitor_browser, generic_browser_price_spider)` on `SCRAPYD_BROWSER_URLS` and HTTP batches
`(price_monitor, generic_price_spider)` on `SCRAPYD_HTTP_URLS`, no batch mixes modes, and a re-dispatch
targets the same node/spider once (quickstart scenarios 4, 12).

- [ ] T021 [US2] Fix `apps/workers/app/workers/tasks_jobs.py` (dispatch-routing.md): add module
  constants `_SCRAPYD_BROWSER_PROJECT = "price_monitor_browser"` and `_GENERIC_BROWSER_SPIDER =
  "generic_browser_price_spider"`; in **both** `dispatch_job` and `recover_stalled_batches`, inside the
  `for batch in batches:` loop select **`(project, spider)`** by `batch.mode` — `BROWSER` →
  `(_SCRAPYD_BROWSER_PROJECT, _GENERIC_BROWSER_SPIDER)`, else `(_SCRAPYD_PROJECT, _GENERIC_PRICE_SPIDER)`
  — and pass the selected `project`/`spider` to `client.schedule(...)`. **Node routing
  (`SCRAPYD_BROWSER_URLS` vs `SCRAPYD_HTTP_URLS`) is ALREADY mode-branched in the code — leave it
  unchanged.** Only the hardcoded project/spider constants are the defect. Leave `plan_batches`,
  `select_node`, and the
  `dispatched:{scrape_job_id}:{batch_index}` idempotency guard (and its `:r{window}` recovery form)
  **unchanged** (FR-015/016, SC-008).
- [ ] T022 [P] [US2] Unit test the dispatch project/spider selector in
  `tests/unit/test_dispatch_routing.py` (must pass in this env): a mixed set of `Batch(mode=BROWSER)` /
  `Batch(mode=HTTP)` → assert the args a fake `ScrapydDispatchClient.schedule` receives —
  `(price_monitor_browser, generic_browser_price_spider, SCRAPYD_BROWSER_URLS)` vs
  `(price_monitor, generic_price_spider, SCRAPYD_HTTP_URLS)`; an all-HTTP job sends nothing to the
  browser pool (US2 AS1/AS3, SC-002).
- [ ] T023 [P] [US2] Live test `tests/integration/test_dispatch_browser_idempotent_live.py` (`skipif`
  Redis): dispatch a browser batch twice (same `scrape_job_id`/`batch_index`) → one Scrapyd run, same
  node/spider, no double-run (US2 AS2, SC-008). Also assert the dispatch client sends **basic-auth**
  credentials on the browser-node `schedule.json` call exactly as for the HTTP node (FR-013 dispatch-auth
  check; the browser deployment scaffold itself — image/`scrapyd.conf` basic-auth — is SPEC-01's, R12).

**Checkpoint**: Browser-mode work reaches the browser service in production; US1 is now reachable end to
end through dispatch. **The deployable MVP is US1 + US2 + the SSRF safety gate (T030/T031)** — per
Constitution §VI (NON-NEGOTIABLE), the browser path must enforce SSRF on every navigation hop before it
may run against real targets, so T030/T031 are pulled forward into the MVP definition even though they
live in the US4 phase. Do NOT ship browser dispatch to production with US1+US2 alone.

---

## Phase 5: User Story 3 - Variant selection before reading the price (Priority: P2)

**Goal**: When the profile carries a `variant_selector_config`, the browser performs the allowlisted
interaction (keyed off the match's variant) and waits for the page to settle before extraction, so the
persisted price is the selected variant's price.

**Independent Test**: Point the browser spider at a fixture whose price changes only after selecting a
variant, with `variant_selector_config`; assert the persisted price is the post-selection price, while
the same page with no config yields the default price, and a missing target → `VARIANT_NOT_FOUND`
(quickstart scenario 8).

- [ ] T024 [US3] Create `libs/scrape-core/scrape_core/browser/variant.py` (variant-selection.md,
  data-model.md §2): `VariantConfigError(ValueError)` carrying `error_code=SELECTOR_BROKEN`;
  `resolve_variant_values(config, match) -> dict` (off-reactor value_from resolution — `options.<key>` →
  `competitor_variant_options[key]`, `identifier`, `sku`; unresolved/missing → `VariantConfigError`);
  `parse_variant_config(config, resolved_values) -> list[PageMethod]` (pure translation: `None` → `[]`;
  `version` must be `1`; allowlist ONLY `click`/`select_option`/`fill`/`wait_for_selector`/
  `wait_for_timeout`/`wait_for_load_state`; any other type incl. `evaluate` → `VariantConfigError`;
  element actions require `selector`, `select_option`/`fill` require `value`|`value_from`; every `wait_*`
  carries the effective timeout; optional trailing `settle` → appended `wait_for_selector`/
  `wait_for_load_state`).
- [ ] T025 [US3] Wire variant resolution off-reactor into `load_targets`
  (`libs/scrape-core/scrape_core/targets.py`): when a resolved profile carries
  `variant_selector_config`, call `resolve_variant_values(config, match)` and store the result on the
  `SpiderTarget` (its `match_variant_values` slot from T005); a `VariantConfigError` at load time marks
  the target to emit a `SELECTOR_BROKEN` failed result (never fetched). Bounded loads otherwise
  unchanged (shared-extraction.md).
- [ ] T026 [US3] Extend `build_page_methods` in `scrape_core/browser/page.py` to append the variant
  `PageMethod`s in the contract order — profile `wait_for_selector` (if set) → variant `actions` (if
  config) → `settle` (if config) — via `parse_variant_config(target.variant_selector_config,
  target.match_variant_values)` (browser-spider.md `playwright_page_methods`).
- [ ] T027 [US3] Complete the variant failure branches in the browser spider `errback`/parse-time guard
  (`generic_browser_price_spider.py`): a `VariantConfigError` (parse/resolve time) → `SELECTOR_BROKEN`
  failed attempt before any fetch; a Playwright missing/uninteractable-element error at run time →
  `VARIANT_NOT_FOUND` — no partially-interacted state persisted as a valid price (US3 AS3, FR-005, R3).
- [ ] T028 [P] [US3] Unit tests for `variant.py` in `tests/unit/test_variant_config.py` (must pass in
  this env, quickstart scenario 2): valid `select_option value_from options.size` + `click` +
  `wait_for_selector` → ordered `PageMethod` list with resolved value from a fake match; `config is None`
  → `[]`; `{"type":"evaluate"}` / any non-allowlisted type → `VariantConfigError`; unresolved
  `value_from` (missing option key) → `VariantConfigError`; bad `version` → `VariantConfigError`;
  settle-only (`actions: []` + `settle`) valid.
- [ ] T029 [P] [US3] Live test `tests/integration/test_browser_variant_live.py` (`skipif`): variant
  fixture → persisted price is the post-selection price; same page with no config → default price; config
  whose target is missing → `VARIANT_NOT_FOUND`, no price persisted (US3 AS1/AS2/AS3, SC-003).

**Checkpoint**: Variant-level correctness on interactive pages; US1/US2 remain green (no config → no
interaction).

---

## Phase 6: User Story 4 - Bounded, guardrail-parity, optionally-proxied browsing (Priority: P2)

**Goal**: The browser path obeys every HTTP-path safety rule plus bounded browser resource use: per-hop
SSRF re-validation on the resolved IP, per-domain robots, `PLAYWRIGHT_PROXY` via a proxied context,
low bounded concurrency, batched off-reactor persistence, and reused match locks.

**Independent Test**: Run the browser spider and confirm concurrent sessions never exceed the low cap; a
private/redirect-to-internal host is refused before the body is read and recorded `BLOCKED`; an assigned
proxy is used and the attempt records `PLAYWRIGHT_PROXY` (password unlogged); N targets persist in far
fewer than N off-reactor commits (quickstart scenarios 9–12).

- [ ] T030 [US4] Create `libs/scrape-core/scrape_core/browser/ssrf.py` with async
  `abort_unsafe_request(request) -> bool` (browser-safety.md, R6): return `True` (abort) only for
  navigation/document requests (`request.is_navigation_request()` or `resource_type == "document"`)
  whose URL fails the reused `scrape_core.safety.fetch.validate_resolved_target(url, resolver=...)` —
  resolving the host and rejecting private/loopback/link-local/reserved/multicast/unspecified IPs via
  the reused `app_shared.url_safety._reject_ip`. The blocking resolve runs **off the reactor thread**
  (`loop.getaddrinfo`/`run_in_thread`); sub-resources pass. Re-runs on every redirect hop. No
  re-implemented SSRF logic.
- [ ] T031 [US4] Add the SSRF wiring to `apps/scrapers-browser/price_monitor_browser/settings.py`
  (browser-safety.md): `SsrfGuardMiddleware` in `DOWNLOADER_MIDDLEWARES` (priority 100) for the pre-fetch
  scheme/userinfo guard; `DNS_RESOLVER = scrape_core.safety.resolver.SafeResolver` (defense-in-depth for
  non-Playwright requests e.g. robots.txt); `PLAYWRIGHT_ABORT_REQUEST =
  scrape_core.browser.ssrf.abort_unsafe_request`. (Extends T014; same file, sequential after it.)
- [ ] T032 [US4] Add the proxied-context branch to `_browser_request_for` in
  `generic_browser_price_spider.py` (browser-safety.md, R7): when `_prepare_dispatch` yields a proxied
  plan, set `meta["playwright_context"] = f"proxy:{provider_id}"` and `meta["playwright_context_kwargs"]
  = {"proxy": {"server": f"http://{host}:{port}", "username": provider.username, "password":
  <decrypted off-reactor in load_targets>}}`; the attempt still records `access_method=PLAYWRIGHT_PROXY`
  plus `proxy_provider_id`/`proxy_country` (reused SPEC-10 audit). Password never logged, never in a
  `meta["proxy"]` URL. Context-creation-with-proxy failure → `PROXY_FAILED` failed attempt (never a
  silent direct fetch).
- [ ] T033 [US4] Complete `classify_browser_failure` in the spider `errback` (browser-spider.md): resolve
  SSRF/robots failures to `BLOCKED` via the reused `classify_exception` / rejection registry FIRST
  (`SsrfRejectedError`, `RobotsBlockedError`), then fall through to `classify_playwright_exception`
  (T018) and the variant codes (T027), then `PROXY_FAILED` for proxy-context failures (FR-008/009/011,
  US4 AS2/AS3).
- [ ] T034 [P] [US4] Live test `tests/integration/test_browser_ssrf_live.py` (`skipif`): a fixture whose
  host resolves to a private IP, and one that 302s to an internal address → navigation aborted before
  body processing (via `PLAYWRIGHT_ABORT_REQUEST`), one `BLOCKED` attempt, no observation (US4 AS2,
  SC-005).
- [ ] T035 [P] [US4] Live test `tests/integration/test_browser_proxy_live.py` (`skipif`): access policy
  assigns a proxy → browser routes through it, attempt records `PLAYWRIGHT_PROXY` +
  `proxy_provider_id`/`proxy_country`, and the proxy password appears in NO log line (US4 AS3, SC-006).
- [ ] T036 [P] [US4] Live test `tests/integration/test_browser_bounds_live.py` (`skipif`): under many
  browser targets, simultaneous sessions never exceed `BROWSER_CONCURRENT_REQUESTS`/`BROWSER_MAX_CONTEXTS`;
  persisting N results does far fewer than N commits with no DB call on the reactor thread; a held match
  lock → `LOCKED_ALREADY_RUNNING` SKIPPED, no duplicate scrape (US4 AS1/AS4/AS5, SC-004/007).

**Checkpoint**: The browser path is safe, isolated, honest about its transport, and bounded. All four
stories complete.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Verification, secret-safety audit, and boundary/lint hygiene across the whole feature.

- [ ] T037 [P] Confirm the import-boundary guard is still green and covers the new modules:
  `scrape_core.targets`/`result_builder` import only `app_shared.*` + `scrape_core.*`;
  `scrape_core.browser.*` may import Scrapy/scrapy-playwright; `app_shared` gains no Scrapy/Twisted/
  Playwright import (shared-extraction.md). Also add a lightweight **reactor-safety unit guard** (runs
  in this container-less env) asserting the browser spider's DB/Redis entry points route through
  `run_in_thread` — an AST/source check that `parse`/`start`/`errback` never call a sync `Session`
  commit or blocking Redis directly on the reactor thread (FR-007, Principle V; parity with the
  existing HTTP reactor-safety guard).
- [ ] T038 [P] Secret-safety audit: grep the browser spider + `ssrf.py`/`page.py`/`variant.py` and the
  proxy path to confirm the decrypted proxy password is placed ONLY in the Playwright `proxy` dict —
  never logged, never in `request.meta["proxy"]` or any log line (FR-011, SC-006, constitution §Tech).
- [ ] T039 Run the full unit suite that must pass in this env (`test_config.py`, `test_browser_errors.py`,
  `test_dispatch_routing.py`, `test_variant_config.py`, the HTTP-spider regression suite) with
  `uv run pytest -q`; confirm every `*_live.py` added here skips cleanly (no Chromium/DB) rather than
  erroring.
- [ ] T040 Walk quickstart.md scenarios 1–12 and confirm each maps to a passing unit test or a
  cleanly-skipped live test; record the SC-001..008 coverage table is satisfied.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately.
- **Foundational (Phase 2)**: Depends on Setup (needs the `Settings` knobs). **BLOCKS all user stories**
  — the shared extraction (T004–T007) + refactor (T010) + regression green (T011) must complete first.
- **US1 (Phase 3)**: Depends on Foundational. The MVP.
- **US2 (Phase 4)**: Depends on Foundational; conceptually independent of US1 (dispatch is a separate
  file), but US1 must exist for US2's route to run a real browser end to end. Can be built in parallel
  with US1 (different files).
- **US3 (Phase 5)**: Depends on Foundational + US1 (extends `page.py`, `load_targets`, and the spider's
  errback). Not required to prove US1.
- **US4 (Phase 6)**: Depends on Foundational + US1 (extends `settings.py`, `_browser_request_for`, and
  the errback). Hardens US1–US3.
- **Polish (Phase 7)**: Depends on all desired stories.

### Critical path

T001 → T004 → T005 → T006 → T010 → T011 → T013 → T014 → T015 → T016 → T017 → T018 (US1 MVP) →
US2/US3/US4 layer on. T007, T008, T009, T012 are parallel within Foundational.

### Within each user story

- Shared/model code before spider wiring before the live test.
- US1: `page.py` (T013) + `settings.py` (T014) before the spider (T015–T018); live tests (T019/T020) last.
- US3: `variant.py` (T024) before load/page wiring (T025/T026) before errback branch (T027) before tests.
- US4: `ssrf.py` (T030) before settings wiring (T031); proxy branch (T032) + classifier (T033) before tests.

### Parallel Opportunities

- Setup: T001, T002 parallel (T003 after, it installs).
- Foundational: T007, T008, T009, T012 marked [P] (different files); T004/T005/T006 are same-file
  sequential; T010 after the moves; T011 after T010.
- US1 live tests T019, T020 parallel. US2 T022, T023 parallel. US3 T028, T029 parallel. US4 T034, T035,
  T036 parallel. Polish T037, T038 parallel.
- With staffing: after Foundational, US1 and US2 can proceed concurrently (disjoint files:
  `apps/scrapers-browser/**` vs `apps/workers/**`).

---

## Parallel Example: Foundational Phase

```bash
# After the same-file moves T004–T006 land, run the independent [P] Foundational tasks together:
Task: "Create scrape_core/result_builder.py with build_scrape_result"        # T007
Task: "Add classify_playwright_exception to scrape_core/errors.py"           # T008
Task: "Create scrape_core/browser/__init__.py"                               # T009
Task: "Unit test classify_playwright_exception in tests/unit/test_browser_errors.py"  # T012
```

## Parallel Example: User Story 4 live tests

```bash
Task: "SSRF live test in tests/integration/test_browser_ssrf_live.py"        # T034
Task: "Proxy live test in tests/integration/test_browser_proxy_live.py"      # T035
Task: "Bounds/lock live test in tests/integration/test_browser_bounds_live.py"  # T036
```

---

## Implementation Strategy

### MVP First (US1 + US2 + SSRF gate T030/T031)

1. Phase 1 Setup → Phase 2 Foundational (shared extraction; regression green — CRITICAL gate).
2. Phase 3 US1 → a scheduled browser-mode match renders JS and persists a price.
3. Phase 4 US2 → production dispatch actually routes browser work to the browser service.
4. **SSRF safety gate (T030 + T031)** — pull these US4 tasks forward: per-hop
   `PLAYWRIGHT_ABORT_REQUEST` + pre-fetch `SsrfGuardMiddleware`/`DNS_RESOLVER` wiring. Per
   Constitution §VI (NON-NEGOTIABLE) the browser path must enforce SSRF on every navigation hop
   before it runs against real targets. The MVP is NOT production-deployable without this step.
5. **STOP and VALIDATE**: browser-mode matches get browser-scraped end to end through dispatch,
   with SSRF enforced on every hop (private/redirect-to-internal refused before body).

### Incremental Delivery

1. Setup + Foundational → shared machinery in `scrape-core`, HTTP suite still green.
2. US1 → browser MVP (test independently on a browser node).
3. US2 → routing correct (mixed-job dispatch test).
4. US3 → variant-level correctness.
5. US4 → safety/proxy/bounds parity.
6. Polish → boundary + secret audit + quickstart walk.

### Parallel Team Strategy

After Foundational: Dev A on US1 (`apps/scrapers-browser/**` + `scrape_core/browser/page.py`), Dev B on
US2 (`apps/workers/tasks_jobs.py` — fully disjoint). US3 and US4 then layer onto US1's spider/settings.

---

## Notes

- [P] = different files, no dependency on an incomplete task.
- The extraction (Phase 2) is pure relocation + parameterization — **behavior-preserving**, guarded by
  the existing HTTP-spider suite (T011). Never change HTTP-spider public behavior.
- No migration, no new `AccessMethod`/`ScrapeErrorCode` member, no new `ScrapeResult` field.
- Unit tests (T002, T012, T022, T028) MUST pass in this env. Live tests (`*_live.py`) MUST skip cleanly
  without Chromium/Postgres/Redis — never fake a live result.
- `app_shared` stays scraping-free; `scrape_core` may depend on it, never the reverse.
- Commit after each task or logical group.
