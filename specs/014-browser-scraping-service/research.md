# Phase 0 Research — SPEC-14 Browser Scraping Service

All unknowns resolved from the master doc, the constitution, and a survey of the existing code
(`generic_price_spider.py`, `scrape_core/*`, `tasks_jobs.py`, `batching.py`,
`scrape_profiles.py`, `competitors_matches.py`, the `scrapy_playwright` package). No
NEEDS CLARIFICATION remains. Format: **Decision / Rationale / Alternatives**.

---

## R1 — Where does the reusable spider machinery live? (shared-code extraction)

**Decision.** Extract the transport-agnostic machinery currently in
`apps/scrapers/price_monitor/spiders/generic_price_spider.py` into `libs/scrape-core`:
`scrape_core/targets.py` (`SpiderTarget`, `_LoadedTargets`, `load_targets`, `_prepare_dispatch`,
`_DispatchDecision`, `VisibleProviders`, the Redis resolution-cache get/set helpers,
`_parse_match_ids`, `_parse_host_port`, `_attempt_kwargs_from_meta`, `_elapsed_ms`, `_RequeueState`)
and `scrape_core/result_builder.py` (`build_scrape_result`, formerly the spider's `_build_result`).
Refactor the HTTP spider to import them, **preserving behavior**. Both spiders then share one loader,
one dispatch-decision engine, one result builder; each keeps only its transport-specific request
construction and callbacks.

**Rationale.** Constitution Principle I: code shared by both Scrapy projects MUST live in
`libs/scrape-core`; `apps/scrapers-browser` may depend on `scrape-core` + `app_shared` only, **never**
on `apps/scrapers`. The machinery is already `app_shared`-only in its imports (verified — it imports
`app_shared.*` + `scrape_core.*`, no `apps.*`), so relocation is mechanical. The existing HTTP-spider
suite (`tests/unit/test_generic_price_spider.py`, `tests/integration/test_spider_*`) is the
regression guard.

**Alternatives.** (a) Duplicate the loader into the browser spider — rejected: ~600 lines of
subtle rate-limit/proxy/strategy logic drifting across two files, forbidden reuse. (b) Put it in
`app_shared` — rejected: it uses `scrape_core.db.run_in_thread`/`workspace_txn` and is conceptually
scrape-runtime; `app_shared` must stay Scrapy/Twisted-free.

---

## R2 — `variant_selector_config` JSON shape (open item #1)

**Decision.** An ordered, **allowlisted** interaction script on the JSONB field:

```jsonc
{
  "version": 1,                       // shape version for forward-compat
  "actions": [                        // executed in order, before extraction
    {"type": "select_option", "selector": "select#size", "value_from": "options.size"},
    {"type": "click",         "selector": "button.add-to-cart"},
    {"type": "wait_for_selector", "selector": ".price[data-ready]", "state": "visible"}
  ],
  "settle": {"wait_for_selector": ".price-final", "load_state": "networkidle"}  // optional post-interaction wait
}
```

Allowlisted `type` values only — `click`, `select_option`, `fill`, `wait_for_selector`,
`wait_for_timeout`, `wait_for_load_state`. Each maps 1:1 to a `scrapy_playwright.page.PageMethod`
(`click`, `select_option`, `fill`, `wait_for_selector`, `wait_for_timeout`, `wait_for_load_state`).
A step supplies its value as either a literal `value` **or** a `value_from` reference into a
**whitelisted** per-match source, resolved off-reactor in `load_targets` from the already-loaded
match row:

| `value_from`     | resolves to                                                 |
|------------------|-------------------------------------------------------------|
| `options.<key>`  | `competitor_product_matches.competitor_variant_options[<key>]` (JSONB) |
| `identifier`     | `competitor_product_matches.competitor_variant_identifier`  |
| `sku`            | `competitor_product_matches.competitor_variant_sku`         |

**Rationale.** A scrape profile is resolved per `(competitor, url_pattern)` group and shared across
the variants that map to it, so the per-variant selection value CANNOT be a literal on the profile —
it must key off the specific match. The match already carries exactly the right per-variant columns
(`competitor_variant_options`/`_identifier`/`_sku`), so the profile holds the *interaction template*
and the match supplies the *value*. The allowlist + `PageMethod` translation is directly supported by
`scrapy-playwright` (`request.meta["playwright_page_methods"]`). Post-interaction the spider re-applies
the `settle` wait (or the profile's `wait_for_selector`) before reading the DOM (US3 AS1).

**Security (decisive).** `evaluate` / arbitrary-JS / arbitrary-callable actions are **forbidden** —
`variant_selector_config` is tenant-editable DB config, and running arbitrary JS from it would be a
code-execution hole. Any unknown `type`, a malformed action, or a `value_from` that resolves to
nothing is a **config error** → the attempt fails cleanly (`SELECTOR_BROKEN`), never executes, never
persists a price (US3 AS3, edge case "Variant target missing/uninteractable").

**Alternatives.** (a) A single `{selector, value}` pair — rejected: too weak for pages needing a
click-then-wait sequence. (b) Free-form `PageMethod` list serialized in JSON — rejected: lets a
tenant call any page method (`evaluate`, `pdf`, `route`), unsafe. (c) A new per-match column for the
value — rejected: violates FR-017 (no new schema) and the columns already exist.

---

## R3 — Browser failure error-code names (open item #2)

**Decision.** **Reuse the existing `ScrapeErrorCode` vocabulary — introduce no new codes** (§34 is a
locked shared language):

| Situation | Error code |
|---|---|
| `wait_for_selector` not present within `browser_timeout_ms`; page-load/navigation timeout | `TIMEOUT` |
| Browser crash / page error / context-launch failure / non-timeout Playwright error | `PLAYWRIGHT_FAILED` |
| `variant_selector_config` target element missing / uninteractable | `VARIANT_NOT_FOUND` |
| Malformed / unsupported `variant_selector_config` (bad action type, unresolved `value_from`) | `SELECTOR_BROKEN` |
| Proxy assigned but the browser context cannot use it | `PROXY_FAILED` |
| Host resolves to (or 302s to) private/internal IP; scheme/userinfo rejected | `BLOCKED` |
| robots policy disallows the path | `BLOCKED` |
| No price extracted from rendered DOM | `PRICE_NOT_FOUND` |
| Confidence below bar / currency mismatch / invalid format | `LOW_CONFIDENCE_PRICE` / `CURRENCY_MISMATCH` (comparable=false) / `INVALID_PRICE_FORMAT` |
| Match lock already held; rate/budget skip | `LOCKED_ALREADY_RUNNING` / `RATE_LIMITED` / `LIMIT_REACHED` |

Implemented by a new `scrape_core.errors.classify_playwright_exception(exc)` mapping
`playwright...TimeoutError` → `TIMEOUT` and other Playwright errors → `PLAYWRIGHT_FAILED`, plus the
spider raising/catching `VariantConfigError`→`SELECTOR_BROKEN` and a missing-element condition
→`VARIANT_NOT_FOUND`. HTTP-status/SSRF/robots/extraction/validation codes come from the reused
`classify_http_status`, `SsrfRejectedError`/`SafeResolver` (`BLOCKED`), `RobotsBlockedError`
(`BLOCKED`), and `validate_candidate` exactly as HTTP does.

**Rationale.** Constitution Tech-constraints §34: failures MUST use the defined vocabulary so the
strategy optimizer, reporting, and debugging share one language. Every situation the spec raises maps
onto an existing member (`TIMEOUT`, `PLAYWRIGHT_FAILED`, `VARIANT_NOT_FOUND`, `SELECTOR_BROKEN`,
`PROXY_FAILED`, `BLOCKED`, `PRICE_NOT_FOUND`, …) already present in `ScrapeErrorCode`. `PLAYWRIGHT_FAILED`
and `VARIANT_NOT_FOUND`/`SELECTOR_BROKEN` were reserved as forward-compat members in SPEC-07 for
exactly this feature.

**Alternatives.** New codes like `WAIT_TIMEOUT`/`VARIANT_INTERACTION_FAILED` — rejected: expands the
locked §34 vocabulary for no analytic gain; `TIMEOUT`/`VARIANT_NOT_FOUND` already carry the meaning.

---

## R4 — Browser retry semantics (open item #3)

**Decision.** **One browser fetch attempt per target per run.** The browser spider does NOT run the
HTTP access-method ladder (`DIRECT_HTTP → DIRECT_HTTP_RETRY → PROXY_HTTP`) inside the process — that
ladder's earlier steps are HTTP transports the browser node cannot perform, and a full page render is
expensive under `max_proc=1`. Each target is fetched once with the resolved browser transport
(`PLAYWRIGHT_PROXY`, proxied when the access policy assigns a proxy, else an unproxied browser fetch
still recorded as `PLAYWRIGHT_PROXY`), producing exactly **one** `RequestAttempt` row. A transient
browser failure is a terminal failed attempt for this run; re-attempting is delegated to the
**job level** — SPEC-08 stall recovery, SPEC-13 scheduled re-scrape, and the next refresh — never an
in-run re-render.

The SPEC-11 **admission** loop is still reused: rate-limit token + concurrency slot acquisition
(`_acquire_fetch_permission`, with `deferred_delay` backoff and the requeue-cap overflow to
`DEFERRED`) and the in-flight match lock (`acquire_lock(mode=PLAYWRIGHT_PROXY)`,
`MATCH_LOCK_BROWSER_TTL_SECONDS`) run exactly once before the single fetch. That backoff is admission
control, not a scrape retry, so it is consistent with "one attempt."

**Rationale.** Spec Assumption "No in-process HTTP→browser escalation" (separate processes) and
constitution §14 "keep browser concurrency low." Multiple renders per target would multiply the cost
of the scarcest resource. The `next_attempt`/`_prepare_dispatch` ladder is reused only to make the
**single** transport decision (proxied vs direct) and run the ceiling/cooldown/budget gate — not to
loop attempts. `errback` records the one failed attempt and stops (no `_dispatch` of a next attempt).

**Alternatives.** (a) Mirror HTTP's full ladder — rejected: no HTTP transport exists on the browser
node; wasteful. (b) A bounded 1 extra retry on `TIMEOUT` — considered, deferred: adds slot pressure
for marginal gain; job-level re-scrape already covers transient failures. Left as a future tuning knob,
not built now.

---

## R5 — What `access_method` does a browser attempt record?

**Decision.** Always `AccessMethod.PLAYWRIGHT_PROXY` (the fourth, browser transport in the locked §11
vocabulary). `proxy_provider_id`/`proxy_country` are populated **only** when the access policy actually
assigned a proxy; for an unproxied browser fetch they are `NULL` (that null-proxy `PLAYWRIGHT_PROXY`
row is what "direct-browser" means in the Key Entities note). No new `AccessMethod` member.

**Rationale.** Constitution VI locks access methods to exactly four; `PLAYWRIGHT_PROXY` is the browser
one. `request_attempts.access_method` is a plain app-validated `VARCHAR` (`enum_column` →
`_AppValidatedEnumString`), so even if a new member were wanted it would need no migration — but adding
one would breach the locked vocabulary, so we don't. FR-011 ("records `PLAYWRIGHT_PROXY` with proxy
metadata") is satisfied; the unproxied case is the same method with null proxy fields.

**Alternatives.** A `DIRECT_BROWSER` member — rejected: expands the constitution's locked set. Recording
`DIRECT_HTTP` for an unproxied browser fetch — rejected: misrepresents the transport (it is a browser
render, not an HTTP GET).

---

## R6 — SSRF for Playwright navigations (critical, non-obvious)

**Decision.** Enforce SSRF for the browser via **two** reused-logic layers:

1. **Pre-fetch scheme/userinfo guard** — `SsrfGuardMiddleware` (registered in the browser project's
   `DOWNLOADER_MIDDLEWARES` exactly as HTTP) runs `process_request` for the scrapy-playwright request
   before the download handler, rejecting bad scheme/userinfo. This covers the top-level request.
2. **Per-navigation-hop resolved-IP guard** — a new `PLAYWRIGHT_ABORT_REQUEST` callable
   (`scrape_core.browser.ssrf.abort_unsafe_request`). `scrapy-playwright` invokes it for **every**
   Playwright request the page issues — including main-frame navigations and each redirect hop
   (verified: handler lines ~784–787 `route.abort()` on truthy return). For each main-frame
   navigation/redirect it re-runs the reused `scrape_core.safety.fetch.validate_resolved_target(url,
   resolver=...)` (which resolves the host and applies `app_shared.url_safety._reject_ip`), aborting
   the request before the body loads; an aborted navigation surfaces to `errback` and is classified
   `BLOCKED`.

Why layer 2 is required: **`scrapy-playwright` does NOT use Scrapy's `DNS_RESOLVER`** — Chromium does
its own DNS internally — so the existing `SafeResolver` (which defeats DNS rebinding for the HTTP
downloader) does **not** cover browser navigations, and Chromium follows redirects internally without
Scrapy's `RedirectMiddleware`. The abort hook is the only place to re-validate every hop against the
resolved IP for the browser path (FR-008, US4 AS2, edge case "Redirect to internal host").

Reactor safety: the abort callable is async; its blocking DNS resolution runs off the reactor thread
(`run_in_thread` / `asyncio` `getaddrinfo`), never a sync resolve on the reactor.

`DNS_RESOLVER = SafeResolver` is still set in the browser project settings as defense-in-depth for any
non-Playwright request (e.g. the robots.txt fetch path), but the browser SSRF guarantee rests on the
abort hook.

**Rationale.** Grounded in the actual `scrapy_playwright` handler behavior. Reuses
`validate_resolved_target`/`_reject_ip` verbatim (no re-implemented SSRF logic), satisfying "reuse the
existing SSRF machinery."

**Alternatives.** (a) Rely on `SafeResolver` alone — rejected: it never runs for Playwright navigations
(would silently fail the SSRF requirement). (b) Chromium `--host-resolver-rules` — rejected: static,
can't express the private-range deny set, no per-hop re-validation. (c) Pre-resolve once before
navigation only — rejected: misses in-browser redirects (DNS-rebind/302-to-internal), which is exactly
US4 AS2.

---

## R7 — Proxying the browser context (`PLAYWRIGHT_PROXY` realization)

**Decision.** When `_prepare_dispatch` (reused) yields a proxied plan, the browser spider sets
`request.meta["playwright_context_kwargs"] = {"proxy": {"server": "http://{host}:{port}",
"username": provider.username, "password": <decrypted>}}` and a **distinct per-provider**
`request.meta["playwright_context"]` name (e.g. `proxy:{provider_id}`) so a proxied context is never
shared with a direct one. The decrypted password comes from the reused off-reactor
`load_targets` decryption (`provider_passwords`), is placed only into the Playwright proxy dict (never
logged, never in a `request.meta["proxy"]` URL). An unproxied target uses the default context, no proxy
kwargs. If context creation with the proxy fails, the attempt is a `PROXY_FAILED` failed attempt (edge
case "Proxy assigned but browser cannot use it"), not a silent direct fetch.

**Rationale.** Playwright takes proxy credentials as a structured `proxy=` on the context/browser
(unlike HTTP's `Proxy-Authorization` header), so the HTTP spider's header approach does not transfer;
the context-kwargs form is the scrapy-playwright-supported path. Per-provider context names keep the
low `PLAYWRIGHT_MAX_CONTEXTS` bound meaningful and prevent cross-contaminating a direct session with a
proxied one. The SPEC-10 assignment/decryption/attempt-logging path is reused unchanged (same
`assign_proxy`, same `provider_passwords`, same `proxy_provider_id`/`proxy_country` onto the attempt).

**Alternatives.** Browser-level `PLAYWRIGHT_LAUNCH_OPTIONS` proxy — rejected: process-global, can't vary
per target; the node scrapes many domains/policies. Embedding creds in a proxy URL — rejected: leaks the
secret into logs/meta.

---

## R8 — Dispatch routing fix (FR-015/016)

**Decision.** In `apps/workers/app/workers/tasks_jobs.py`, add module constants
`_SCRAPYD_BROWSER_PROJECT = "price_monitor_browser"`, `_GENERIC_BROWSER_SPIDER =
"generic_browser_price_spider"`, and in **both** `dispatch_job` and `recover_stalled_batches` select
`(project, spider)` by `batch.mode`: `BROWSER` → browser project/spider, else the existing
`(price_monitor, generic_price_spider)`. Node selection (`SCRAPYD_BROWSER_URLS` vs `SCRAPYD_HTTP_URLS`,
already mode-branched) and the `dispatched:{scrape_job_id}:{batch_index}` idempotency guard,
`select_node`, and `plan_batches` are **unchanged**.

**Rationale.** The batching layer already produces mode-pure batches (`plan_batches` groups by
`(domain, mode)`; a `Batch` always carries one mode) and already routes BROWSER batches to the browser
node pool — the only defect is the hardcoded HTTP project/spider constants passed to `client.schedule`.
This is the smallest correct change; determinism and idempotency are preserved because the same
`batch_index` and node selection are reused, only the payload's project/spider string changes with the
(already-determined) mode (FR-016, SC-008, US2 AS1/AS2).

**Alternatives.** A new dispatch task for browser — rejected: duplicates orchestration; the existing
task already branches on mode for the node pool.

---

## R9 — Browser project `settings.py` completion

**Decision.** Bring `apps/scrapers-browser/price_monitor_browser/settings.py` to parity with the HTTP
project for the shared runtime, keeping Playwright + low concurrency:
`ITEM_PIPELINES = {BatchedPersistencePipeline: 300}`; `ROBOTSTXT_OBEY = False` (replaced by the
per-request `RobotsPolicyMiddleware`); `DNS_RESOLVER = SafeResolver`;
`DOWNLOADER_MIDDLEWARES = {SsrfGuardMiddleware:100, RobotsPolicyMiddleware:110}` (no `HttpProxyMiddleware`
— browser proxying is via context kwargs, R7); keep the scrapy-playwright `DOWNLOAD_HANDLERS` +
`AsyncioSelectorReactor`; add `PLAYWRIGHT_ABORT_REQUEST = scrape_core.browser.ssrf.abort_unsafe_request`;
add `PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT` and `CONCURRENT_REQUESTS`/`PLAYWRIGHT_MAX_CONTEXTS` read from
`get_settings()` (`BROWSER_CONCURRENT_REQUESTS`, `BROWSER_MAX_CONTEXTS`,
`SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS`) instead of the current hardcoded `2`/`1`; read
`SCRAPE_FLUSH_MAX_ITEMS`/`SCRAPE_FLUSH_INTERVAL_SECONDS` from settings exactly like the HTTP project.

**Rationale.** The browser path must reuse the SAME persistence/SSRF/robots wiring (FR-006/008/009/010);
the current skeleton only wires the Playwright handlers. Concurrency/timeout become DB/env-tunable
(Principle IV) rather than literals. Note `ROBOTSTXT_OBEY` flips from the skeleton's `True` to `False`
because per-request `RobotsPolicyMiddleware` (not Scrapy's global switch) is the sanctioned mechanism.

**Alternatives.** Keep hardcoded concurrency — rejected: Principle IV; the spec explicitly says this
feature "owns their correctness." Keep `ROBOTSTXT_OBEY=True` — rejected: violates FR-009 (per-request
robots, not the global switch) and would double-handle robots.

---

## R10 — Bounding every navigation/wait (FR-003/018)

**Decision.** The effective per-target browser timeout is `profile.browser_timeout_ms` when set, else
`Settings.SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS` (default 30000). It bounds page navigation (via
`PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT` and/or a per-request page-goto timeout) and every
`wait_for_selector`/settle `PageMethod` (passed as the method's `timeout`). A wait/nav that exceeds it
raises Playwright `TimeoutError` → classified `TIMEOUT`; the page/context is released so the scarce slot
is reclaimed (edge case "Browser crash / page error"; FR-018).

**Rationale.** No page may hold a `max_proc=1` slot indefinitely (SC-004). The timeout is per-target
(from the resolved profile), with a safe global default when unset.

**Alternatives.** A single global timeout only — rejected: `browser_timeout_ms` is a per-profile column
meant to tune slow pages.

---

## R11 — `price_analysis` handoff parity (FR-006)

**Decision.** No new code — reusing `BatchedPersistencePipeline` gives the handoff for free: the
pipeline already enqueues one `PRICE_ANALYSIS_RECOMPUTE` per distinct `(workspace_id, scrape_job_id,
product_variant_id)` (Redis `SET NX` dedup) after the batch commits, and stops there. The browser
spider computes no alert/variant-state/webhook itself (US1 AS3).

**Rationale.** The pipeline is the single persistence + handoff seam for both spiders; the browser
spider only needs to emit `ScrapeResult` items with the same fields (via the shared
`build_scrape_result`), which it does.

**Alternatives.** A browser-specific handoff — rejected: duplicates SPEC-09, breaks the "one task per
variant per job" dedup shared across modes.

---

## R12 — Deployment scaffold reuse (FR-013)

**Decision.** Reuse the existing `apps/scrapers-browser` image untouched structurally: Chromium is
already installed at build (`playwright install --with-deps chromium`), the `price_monitor_browser`
project is baked in (`COPY . .`, no runtime egg upload), `scrapyd.conf` has `max_proc=1` + basic auth
rendered from `SCRAPYD_USERNAME`/`SCRAPYD_PASSWORD` at entrypoint, and `SCRAPYD_BROWSER_URLS` is an
existing config pool. The dispatch client already authenticates every `schedule.json` (same basic-auth
path as HTTP). This feature adds only the spider + settings; no Dockerfile/scrapyd.conf change is
required (the spider is discovered via `SPIDER_MODULES` already pointing at
`price_monitor_browser.spiders`).

**Rationale.** SPEC-01 built the scaffold precisely so SPEC-14 only adds the spider. FR-013's
"browser project baked at build time, basic auth, dispatch authenticates" is already satisfied.

**Alternatives.** Runtime `scrapyd-deploy` egg upload — rejected: the image bakes the project at build,
matching the HTTP node and FR-013 "no runtime code-upload dependency."

---

### Resolved unknowns summary

| Spec open item | Resolved by |
|---|---|
| `variant_selector_config` JSON shape | R2 |
| Browser-failure error-code names | R3 |
| Browser retry semantics | R4 |
| (surfaced) `access_method` for browser | R5 |
| (surfaced) SSRF for Playwright navigations | R6 |
| (surfaced) proxying the browser context | R7 |
| (surfaced) shared-code placement (Principle I) | R1 |
