# Quickstart / Validation — SPEC-14 Browser Scraping Service

Validation guide (not implementation). Proves the browser path end to end against JS-rendered fixture
pages, plus the dispatch-routing fix. Live browser/DB scenarios use `*_live.py` with a `skipif` probe
(Chromium + Postgres); this build env has neither Docker daemon nor Chromium/DB, so those skip cleanly
— unit scenarios run everywhere.

## Prerequisites

```bash
uv sync --all-packages            # NEVER plain `uv sync` (wipes workspace member deps)
# live only: playwright install --with-deps chromium ; Postgres+PgBouncer+Redis up
```

## Unit scenarios (no reactor, no DB, no browser — run everywhere)

1. **Shared extraction is behavior-preserving** — the full existing HTTP-spider suite stays green:
   ```bash
   uv run pytest tests/unit/test_generic_price_spider.py tests/integration/test_spider_access.py -q
   ```
   (imports repointed to `scrape_core.targets`/`result_builder`; assertions unchanged.)

2. **`variant_selector_config` parsing** (`scrape_core/browser/variant.py`):
   - a valid config with `select_option value_from options.size` + `click` + `wait_for_selector` →
     ordered `PageMethod` list; `value_from` resolved from a fake match's `competitor_variant_options`.
   - `config is None` → `[]`.
   - `{"type": "evaluate", ...}` or any non-allowlisted type → `VariantConfigError`.
   - unresolved `value_from` (missing option key) → `VariantConfigError`.

3. **Browser error classifier** (`scrape_core/errors.classify_playwright_exception`): Playwright
   `TimeoutError` → `TIMEOUT`; other Playwright error → `PLAYWRIGHT_FAILED`.

4. **Dispatch routing selector** (`apps/workers` unit): a mixed set of `Batch(mode=BROWSER)` /
   `Batch(mode=HTTP)` → BROWSER picks `(price_monitor_browser, generic_browser_price_spider)` +
   `SCRAPYD_BROWSER_URLS`; HTTP picks `(price_monitor, generic_price_spider)` + `SCRAPYD_HTTP_URLS`
   (assert the args passed to a fake `ScrapydDispatchClient.schedule`). Covers US2 AS1/AS3, SC-002.

## Live scenarios (`*_live.py`, skip without Chromium+DB)

5. **US1 — JS page yields a price** (SC-001): serve a fixture whose price is injected by JS after load,
   profile with `price_selector` + `wait_for_selector`; schedule `generic_browser_price_spider` against a
   browser-mode match. Assert: exactly one `PriceObservation` (valid Decimal), one `MatchCurrentPrice`,
   one `RequestAttempt` (`access_method=PLAYWRIGHT_PROXY`), and one `PRICE_ANALYSIS_RECOMPUTE` enqueued
   for the variant.

6. **US1 AS2 — wait times out**: same page, `wait_for_selector` that never appears, small
   `browser_timeout_ms`. Assert one **failed** `RequestAttempt` `error_code=TIMEOUT`, no observation with
   a price, bounded (no hang), page/context released.

7. **US1 AS4 — server-HTML price**: browser mode against a static-price fixture → still extracts +
   persists (browser is a superset of static extraction).

8. **US3 — variant selection** (SC-003): fixture whose price changes only after selecting a variant, with
   `variant_selector_config`. Assert the persisted price is the post-selection price; the same page with
   **no** config yields the default price; a config whose target is missing → `VARIANT_NOT_FOUND`, no
   price persisted.

9. **US4 AS2 — SSRF** (SC-005): a fixture whose host resolves to a private IP, and one that 302s to an
   internal address. Assert the navigation is aborted before body processing (`PLAYWRIGHT_ABORT_REQUEST`),
   one `BLOCKED` attempt, no observation.

10. **US4 AS3 — proxied context** (SC-006): access policy assigns a proxy; assert the browser routes
    through it, the attempt records `PLAYWRIGHT_PROXY` + `proxy_provider_id`/`proxy_country`, and the
    proxy password appears in **no** log line.

11. **US4 AS1/AS4/AS5** (SC-004/007): under many browser targets, simultaneous sessions never exceed the
    configured bound; persisting N results does far fewer than N commits, no DB call on the reactor; a
    held match lock → `LOCKED_ALREADY_RUNNING` SKIPPED, no duplicate scrape.

12. **US2 AS2 / SC-008 — idempotent browser dispatch**: dispatch a browser batch twice (same
    `scrape_job_id`/`batch_index`); assert one Scrapyd run, same node/spider, no double-run.

## Success criteria coverage

| SC | Scenario |
|---|---|
| SC-001 | 5 |
| SC-002 | 4 |
| SC-003 | 8 |
| SC-004 | 11 |
| SC-005 | 9 |
| SC-006 | 10 |
| SC-007 | 11 |
| SC-008 | 12 |

Details: [browser-spider](./contracts/browser-spider.md), [variant-selection](./contracts/variant-selection.md),
[browser-safety](./contracts/browser-safety.md), [dispatch-routing](./contracts/dispatch-routing.md),
[shared-extraction](./contracts/shared-extraction.md); shapes in [data-model.md](./data-model.md);
decisions in [research.md](./research.md).
