# Contract — Shared spider machinery extraction (Principle I, R1)

Move the transport-agnostic machinery out of
`apps/scrapers/price_monitor/spiders/generic_price_spider.py` into `libs/scrape-core` so both Scrapy
projects import it (a browser project may NOT import `apps/scrapers`). **Behavior-preserving**:
guarded by the existing HTTP-spider suite.

## What moves → `scrape_core/targets.py`

- `SpiderTarget` (extended with the browser-relevant already-loaded fields: resolved
  `variant_selector_config` + its resolved `value_from` values, `wait_for_selector`,
  `browser_timeout_ms` — all read from the resolved profile/match; defaults keep old constructors valid)
- `_LoadedTargets`, `load_targets` (adds the off-reactor `resolve_variant_values` call when a profile
  carries `variant_selector_config`; otherwise unchanged bounded loads)
- `_DispatchDecision`, `_prepare_dispatch`
- `VisibleProviders`, the Redis resolution-cache get/set helpers (`_cache_*_group_result`,
  `_cache_*_access_result`)
- `_parse_match_ids`, `_parse_host_port`, `_attempt_kwargs_from_meta`, `_elapsed_ms`, `_RequeueState`

## What moves → `scrape_core/result_builder.py`

- `build_scrape_result(...)` (formerly the spider method `_build_result`) as a free function taking the
  target + attempt fields; identical `ScrapeResult` construction.

## What stays transport-specific (in each spider)

- HTTP spider: `_request_for` (Scrapy request over the HTTP download handler, `Proxy-Authorization`
  header), `parse`/`errback`/`_dispatch`/`_acquire_fetch_permission`/`_overflow_to_dispatch` retain the
  full access-method ladder (multi-attempt). **Refactored only to import the moved helpers** — no
  behavior change.
- Browser spider: `_browser_request_for` (Playwright meta), single-attempt `parse`/`errback`, no ladder
  (R4). Reuses the moved `_acquire_fetch_permission`/`_overflow_to_dispatch` logic — extract those into
  a small shared mixin/helper in `scrape_core.targets` too, or keep them shared via composition, so the
  browser spider gets identical admission behavior without duplication.

> Note: `_acquire_fetch_permission`/`_overflow_to_dispatch`/`_dispatch` are currently spider methods.
> Extract their bodies into `scrape_core.targets` functions taking the spider's
> `workspace_id`/`scrape_job_id`/`_requeue_state_by_match_id` (or a small `AdmissionContext`), so both
> spiders call the same admission code. The HTTP spider's methods become thin wrappers.

## Behavior-preservation contract

- `tests/unit/test_generic_price_spider.py` and `tests/integration/test_spider_*` MUST pass unchanged
  (imports may be repointed if a test reaches into a moved private symbol; assertions unchanged).
- `import_boundaries` test stays green: `app_shared` gains no Scrapy/Twisted/Playwright import;
  `scrape_core.targets`/`result_builder` import only `app_shared.*` + `scrape_core.*` (they already do);
  `scrape_core.browser.*` may import Scrapy/scrapy-playwright.
- No public behavior of the HTTP spider changes; the extraction is pure relocation + parameterization.
