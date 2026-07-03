# Contract: fetch-time URL safety / SSRF (`scrape_core.safety`)

Extends the save-time `app_shared.url_safety.validate_competitor_url` with resolved-IP-at-connection-time validation + per-redirect-hop re-validation (FR-005, §8/§11, Principle VI, research D2). NON-NEGOTIABLE.

## `validate_resolved_target(url, *, resolver, allowlist=None) -> None`

Raises on any unsafe target; returns `None` when safe.

1. Run `validate_competitor_url(url)` — reuses the scheme allow-list (`http`/`https` only), userinfo rejection, and IP-literal deny ranges (no re-implementation).
2. Resolve the host via the **injected** `resolver` callable (`host -> list[ip_str]`).
3. Reject each resolved IP with the existing `app_shared.url_safety._reject_ip` predicate (not `is_global`, or loopback/private/link-local/reserved/multicast/unspecified) — **unless** the IP is in the explicit `allowlist`.

**Injectable seam** (spec Clarification #3):
- Happy-path fixture tests inject a `resolver` returning a **public** IP, or pass an `allowlist` for the loopback fixture server — so loopback-served fixtures don't trip the deny rule.
- Deny-path tests inject a resolver returning private/loopback/link-local/unique-local/metadata IPs, and a redirect-to-internal case.
- **Production**: real system resolver, `allowlist=None`. Prod always validates the real resolved IP with no allowlist.

## Enforcement points in the Scrapy project

- **`SafeResolver`** (`resolver.py`, Twisted) — installed via the `DNS_RESOLVER` setting; wraps the reactor resolver and **refuses to return an unsafe IP** (raises), so the connection cannot proceed to an internal address. This is the connect-time defense against DNS rebinding (the address the socket connects to is the validated one).
- **`SsrfGuardMiddleware`** (`middleware.py`, Scrapy downloader middleware):
  - `process_request`: pre-fetch scheme/userinfo re-check (fast reject, no network).
  - redirect handling: **every** redirect hop is re-validated — Scrapy's `RedirectMiddleware` re-emits each `Location` as a new request that passes back through `process_request` (and re-resolves through `SafeResolver`), so a public→internal 302 is refused at the hop.
  - A rejection short-circuits to a `success=false` `ScrapeResult` (error code `BLOCKED`) **before body download** — no observation is ever marked `success=true` for that target (US2 scenario 4).

## Acceptance (US2 / SC-002)

- Host resolving to private/loopback/link-local/unique-local/metadata IP → refused before body download; failure recorded.
- Public URL redirecting to an internal IP → the redirect hop refused.
- Disallowed scheme or embedded userinfo → rejected without any network fetch.
- 100% of internal-target fetches (direct or via redirect) refused; none produce a `success=true` observation.
