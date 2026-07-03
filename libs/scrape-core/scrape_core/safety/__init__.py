"""Fetch-time SSRF safety (`contracts/fetch-url-safety.md`, FR-005, Principle VI).

Extends the save-time `app_shared.url_safety.validate_competitor_url`
(scheme allow-list, userinfo rejection, IP-literal deny) with
resolved-IP-at-connection-time validation + per-redirect-hop
re-validation:

- `fetch.py` — `validate_resolved_target()`, the pure/injectable
  resolver+allowlist seam (unit-testable off-reactor).
- `resolver.py` — `SafeResolver`, the Twisted resolver wrapper installed
  via the `DNS_RESOLVER` setting (connect-time defense against DNS
  rebinding).
- `middleware.py` — `SsrfGuardMiddleware`, the Scrapy downloader
  middleware that pre-checks scheme/userinfo on every request
  (including each redirect hop).
"""

from __future__ import annotations

from scrape_core.safety.fetch import validate_resolved_target

__all__ = ["validate_resolved_target"]
