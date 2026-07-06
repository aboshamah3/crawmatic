# Contract — `generic_browser_price_spider` (US1, US4)

`apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py`

`GenericBrowserPriceSpider(scrapy.Spider)`, `name = "generic_browser_price_spider"`. Reuses the
extracted `scrape_core.targets` (loader + dispatch decision + permission/lock) and
`scrape_core.result_builder.build_scrape_result`; adds only Playwright transport, wait/variant
interaction, and the browser error classifier.

## Spider arguments (FR-002) — identical to the HTTP spider

`workspace_id` (required), `scrape_job_id`, `match_ids` (comma list or JSON list), `mode` (`"BROWSER"`).
Parsed via the shared `_parse_match_ids`. A match outside `workspace_id` is absent (no cross-read).

## `start()` (async) — per target

1. `loaded = await run_in_thread(load_targets, workspace_id, match_ids)` (shared, off-reactor):
   resolves profile + access policy + robots + strategy + decrypted proxy passwords + the resolved
   `variant_selector_config` `value_from` values. Bounded loads, unchanged.
2. For each target: init `_RequeueState`; `decision = await run_in_thread(_prepare_dispatch, target, 1,
   ...)` (shared) → the single transport decision (proxied `PLAYWRIGHT_PROXY` vs direct) + ceiling/
   cooldown/budget gate. `decision.plan is None` → emit the skip `ScrapeResult`
   (`RATE_LIMITED`/`PROXY_FAILED`/`LIMIT_REACHED`) or silently skip (`NONE_RESOLVED`), exactly as HTTP.
3. Acquire admission (shared `_acquire_fetch_permission` — rate token + concurrency slot, backoff via
   `deferred_delay`, requeue-cap overflow → `DEFERRED`) then the in-flight match lock
   (`acquire_lock(mode=AccessMethod.PLAYWRIGHT_PROXY)` → `MATCH_LOCK_BROWSER_TTL_SECONDS`). Lock held →
   emit `SKIPPED`/`LOCKED_ALREADY_RUNNING`, no fetch.
4. On grant: `yield` the Playwright request built by `_browser_request_for(target, plan, proxy, perm,
   lock)` (below). **One** attempt per target (R4) — there is no `_dispatch` retry loop.

## `_browser_request_for(...)` → `scrapy.Request`

`request.meta` carries (in addition to the shared `match_id`/`robots_policy`/`access_method`/
`attempt_number`/proxy audit fields/`dispatch_monotonic`/`semaphore_*`/`match_lock_*` keys stamped
exactly as HTTP):

- `"playwright": True`
- `"playwright_include_page": False` (page auto-closed by the handler after the response; no leaked page)
- `"playwright_page_methods"`: `[wait_for_selector PageMethod?] + variant PageMethods + settle PageMethod?`
  built by `scrape_core.browser.page.build_page_methods(target)` and
  `scrape_core.browser.variant.parse_variant_config(target.variant_config, target.match_variant_values)`.
  Order: profile `wait_for_selector` (if set) → variant `actions` (if config) → `settle` (if config).
  Each `wait_*` method carries the effective timeout (R10).
- proxied target only (R7): `"playwright_context": f"proxy:{provider_id}"` and
  `"playwright_context_kwargs": {"proxy": {"server": ..., "username": ..., "password": <decrypted>}}`.
  Unproxied: default context, no proxy kwargs. Password never logged, never in a `meta["proxy"]` URL.
- page-goto/navigation bounded by the effective timeout (via `PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT`
  and/or a per-request goto timeout).

`callback=self.parse`, `errback=self.errback`, `dont_filter=True`.

## `parse(response)` (async) — reuse HTTP result path

1. Release the concurrency slot (shared, from `response.meta`).
2. `attempt_kwargs = _attempt_kwargs_from_meta(response.meta)` (shared).
3. `classify_http_status(response.status)` → non-`None` ⇒ failed `ScrapeResult` with that code.
4. `extract(response.text, target.profile, preferred_method=...)` on the **rendered** HTML
   (`response.text` is the post-JS DOM from scrapy-playwright). `None` ⇒ `PRICE_NOT_FOUND`.
5. `validate_candidate(...)` (reused) → `Rejected` ⇒ its `error_code` (`LOW_CONFIDENCE_PRICE`/
   `CURRENCY_MISMATCH`/…); `Accepted` ⇒ success `ScrapeResult` (price, comparable).
6. Every yield is `build_scrape_result(...)` with `access_method=PLAYWRIGHT_PROXY` + proxy fields from
   meta. The reused pipeline persists + terminalizes target + enqueues `price_analysis` (FR-006).

A variant/settle wait that timed out surfaces as a download failure → `errback` (not `parse`).

## `errback(failure)` (async) — single attempt, no retry (R4)

1. Release the concurrency slot (shared).
2. `error_code = classify_browser_failure(failure, hostname)` — SSRF/robots (`BLOCKED`) via the reused
   `classify_exception` registry first; else `classify_playwright_exception(failure.value)`
   (`TimeoutError`→`TIMEOUT`, else `PLAYWRIGHT_FAILED`); a `VariantConfigError`→`SELECTOR_BROKEN`; a
   missing/uninteractable variant element→`VARIANT_NOT_FOUND`.
3. `yield build_scrape_result(..., success=False, error_code=...)` — one failed attempt. **Stop.** No
   next attempt is dispatched (the browser node has no HTTP ladder; job-level re-scrape handles retry).

## Guarantees

- One `PriceObservation` + one `RequestAttempt` per target; one `MatchCurrentPrice` upsert on success.
- No DB/Redis call on the reactor thread (all via `run_in_thread`); no `time.sleep`; backoff via
  `deferred_delay` (all inherited/shared).
- Every navigation/wait bounded (FR-018); page/context released on completion or failure (no leaked
  Chromium) via `playwright_include_page: False` + handler auto-close.
- No alert/variant-state/webhook computed here (FR-006).
