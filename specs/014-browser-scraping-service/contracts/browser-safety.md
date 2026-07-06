# Contract — Browser safety: SSRF, robots, proxy, reactor (US4)

## SSRF (FR-008, US4 AS2) — `scrape_core/browser/ssrf.py`

Two reused-logic layers (R6):

1. **Pre-fetch scheme/userinfo** — `SsrfGuardMiddleware` in the browser project `DOWNLOADER_MIDDLEWARES`
   (priority 100). `process_request` runs `validate_competitor_url(request.url)` before the
   scrapy-playwright handler; bad scheme/userinfo → `SsrfRejectedError` (`BLOCKED`). Unchanged from HTTP.

2. **Per-navigation-hop resolved-IP** — `PLAYWRIGHT_ABORT_REQUEST = scrape_core.browser.ssrf.
   abort_unsafe_request`:

   ```python
   async def abort_unsafe_request(request: PlaywrightRequest) -> bool:
       """True → scrapy-playwright aborts the request before its body loads.
       Applies only to navigation/document requests (request.is_navigation_request()
       or resource_type == 'document'); other sub-resources pass. For a navigation URL,
       re-run the reused validate_resolved_target(url, resolver=<system>) OFF-REACTOR
       (loop.getaddrinfo / run_in_thread) — resolves the host and rejects a
       private/loopback/link-local/reserved/multicast/unspecified IP. Unsafe → True (abort).
       The blocking resolve NEVER runs on the reactor thread."""
   ```

   Invoked by scrapy-playwright for **every** Playwright request including each redirect hop
   (handler `route.abort()` on truthy return). An aborted navigation surfaces to the spider `errback`
   → classified `BLOCKED`. This is REQUIRED because scrapy-playwright does **not** use Scrapy's
   `DNS_RESOLVER`, so `SafeResolver` alone never runs for browser navigations, and Chromium follows
   redirects internally (bypassing `RedirectMiddleware`).

`DNS_RESOLVER = scrape_core.safety.resolver.SafeResolver` is still set (defense-in-depth for any
non-Playwright request e.g. robots.txt fetch), reusing `validate_resolved_target`/`_reject_ip` — no
re-implemented SSRF logic anywhere.

**Guarantee:** a browser target whose host resolves to (or 302s to) a private/internal IP is refused
before any page body is processed and recorded as a `BLOCKED` attempt (SC-005, US4 AS2).

## Robots (FR-009) — reused `RobotsPolicyMiddleware`

Registered in the browser project `DOWNLOADER_MIDDLEWARES` (priority 110); `ROBOTSTXT_OBEY = False`
(per-request policy, not Scrapy's global switch). `request.meta["robots_policy"]` is stamped from the
resolved `Competitor.robots_policy` exactly as HTTP. `RESPECT` disallow → `RobotsBlockedError`
(`BLOCKED`); the robots.txt fetch (cache miss) runs off-reactor via `run_in_thread`. Unchanged.

## Proxy (FR-011, US4 AS3) — `PLAYWRIGHT_PROXY` context (R7)

When `_prepare_dispatch` yields a proxied plan (access policy assigns a proxy), the request carries:

```python
meta["playwright_context"] = f"proxy:{proxy_assignment.provider_id}"
meta["playwright_context_kwargs"] = {"proxy": {
    "server": f"http://{host}:{port}",           # from provider.base_url
    "username": provider.username,
    "password": <decrypted off-reactor in load_targets>,
}}
```

The attempt records `access_method = PLAYWRIGHT_PROXY`, `proxy_provider_id`, `proxy_country` (reused
SPEC-10 audit). Unproxied target → default context, no proxy kwargs, still `PLAYWRIGHT_PROXY` with null
proxy fields (R5). **Password never logged, never in a URL.** Context-creation-with-proxy failure →
`PROXY_FAILED` failed attempt, never a silent direct fetch (edge case).

## Concurrency & reactor (FR-007/010/014, US4 AS1/AS4/AS5)

- Node-level: `scrapyd.conf max_proc = 1`. Process-level: `CONCURRENT_REQUESTS = BROWSER_CONCURRENT_REQUESTS`,
  `PLAYWRIGHT_MAX_CONTEXTS = BROWSER_MAX_CONTEXTS` (from `Settings`, low). Simultaneous browser
  sessions never exceed the configured bound (SC-004).
- Every navigation/wait bounded by the effective timeout (R10); page/context released on completion or
  failure (`playwright_include_page: False`) — no leaked Chromium, slot reclaimed (FR-018).
- Persistence batched + off-reactor via the reused `BatchedPersistencePipeline` — far fewer than N
  commits, no DB call on the reactor thread (SC-007, FR-010).
- In-flight match lock + distributed domain rate limiting reused (SPEC-11) — a match already being
  scraped is not concurrently re-scraped (US4 AS5); lock TTL is `MATCH_LOCK_BROWSER_TTL_SECONDS`.
