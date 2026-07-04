# Contract: Spider integration (extend `price_monitor` — do NOT rebuild)

Extends the existing `apps/scrapers/price_monitor/spiders/generic_price_spider.py` at its
current seams so scrapes carry out the resolved access strategy and every attempt is logged
with the real transport. The persistence pipeline (`BatchedPersistencePipeline`) is
**unchanged** — it already writes `RequestAttempt` with `access_method`/`proxy_provider_id`/
`proxy_country` (research D1).

## Changes

1. **Target load** (`load_targets`, off-reactor `run_in_thread`): in addition to the SPEC-06
   profile, resolve the **effective access policy** per `(competitor, domain, url_pattern)`
   group using the same `access_resolution_cache_key` cache the API orchestrator populates
   (duplicated bounded-load shape, `apps -> libs` only — SPEC-07 precedent). Attach the
   resolved policy + assigned proxy plan to each `SpiderTarget`.

2. **First request** (`_request_for`): compute `next_attempt(strategy, attempt_number=1, ...)`
   (pure engine). For a proxied plan, `assign_proxy(...)`, set `request.meta["proxy"] =
   f"http://{host}:{port}"` and a `Proxy-Authorization` header built from
   `username` + `decrypt_secret(password_encrypted, password_key_version)` — decrypted
   **inside `run_in_thread`**, never on the reactor thread, never logged. Stash
   `access_method`/`proxy_provider_id`/`proxy_country`/`attempt_number` in `request.meta`.
   Before **every** dispatch (direct or proxied), `check_rate_ceilings` (policy per-min/hour/day)
   + `check_domain_cooldown` (domain-rule `cooldown_seconds`), off-reactor; if not allowed, defer/
   skip the attempt and stamp `RATE_LIMITED` (no dispatch). Before a proxied dispatch,
   additionally `incr_and_check_monthly_budget`; if exhausted, recompute
   `next_attempt(proxy_budget_exhausted=True)`. Per-domain `max_concurrent_requests` is not
   enforced here (deferred to SPEC-11).

3. **Retry** (`errback` / a lightweight retry on failure): consult `next_attempt(...,
   attempt_number=n+1)`. If it returns an `AttemptPlan`, yield a follow-up `scrapy.Request`
   with the new method/proxy (switching to `PROXY_HTTP` on retry when the policy says so). If
   it returns `STOP`, emit the terminal failure `ScrapeResult`. `PLAYWRIGHT_PROXY` plans are
   recorded as intent and terminate here (SPEC-14 executes browser rendering).

4. **Result build** (`_build_result`): stamp `access_method` from the actual attempt's method
   (not hardcoded `DIRECT_HTTP`), plus `proxy_provider_id`/`proxy_country` (None for direct)
   and the real `attempt_number`, `status_code`, `response_time_ms`. Failure classification
   maps to the existing `ScrapeErrorCode` (`PROXY_FAILED`/`RATE_LIMITED`/`HTTP_429`/`HTTP_403`/
   `TIMEOUT`/`DNS_ERROR`/`BLOCKED`/`LIMIT_REACHED`) via `scrape_core.errors.classify_exception`
   (extended for proxy failures).

5. **Settings** (`apps/scrapers/price_monitor/settings.py`): enable Scrapy's built-in
   `HttpProxyMiddleware` (reads `request.meta["proxy"]`) — the existing SSRF `SafeResolver` +
   `SsrfGuardMiddleware` stay in place so the *target* URL is still DNS-re-resolved and every
   redirect hop re-validated (FR-005 fetch-time).

## Non-goals (scope guardrails)

- No new spider, no browser rendering (SPEC-14), no cluster-wide distributed limiter / fencing
  in-flight lock (SPEC-11), no persistence-pipeline change.

## Acceptance (skip-clean integration + unit)

- `DIRECT_THEN_PROXY`, `max_retries≥1`: a failed direct first attempt yields a **second**
  `ScrapeResult` with `access_method=PROXY_HTTP` and the proxy provider/country set — two
  `RequestAttempt` rows with `attempt_number` 1 and 2 (SC-002, Scenario US3-1).
- `DIRECT_ONLY`: no `ScrapeResult`/attempt ever carries a proxy (SC-001).
- A disabled/missing referenced provider degrades (falls back per strategy or records
  `PROXY_FAILED`) — never crashes the run (Edge Case).
- Every fetch attempt produces exactly one `RequestAttempt` row (SC-002); writes stay batched
  and off the reactor thread (unchanged pipeline; SC-006).
- Decrypted proxy password never appears in logs (log-capture assertion).
