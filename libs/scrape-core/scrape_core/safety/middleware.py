"""`SsrfGuardMiddleware` — pre-fetch SSRF guard, re-applied to every redirect hop.

Per `contracts/fetch-url-safety.md`: `process_request` re-checks
scheme/userinfo before fetch (fast reject, no network) by reusing the
save-time `app_shared.url_safety.validate_competitor_url`. Because
Scrapy's downloader invokes `process_request` for **every** request it
handles — including each new request `RedirectMiddleware` re-emits for
a `Location` header — this same check re-runs on every redirect hop by
construction, without this middleware needing any redirect-specific
code. The other half of "fetch-time SSRF" (the resolved-IP check) is
enforced independently, per request/hop, by `safety.resolver.SafeResolver`
at actual DNS-resolution/connect time.

A rejection raises `SsrfRejectedError` (an `IgnoreRequest`), which
short-circuits the request before any body download; the spider's
`errback` (via `scrape_core.errors.classify_exception`, which recognizes
this exception's `error_code`) turns it into a `success=false`
`ScrapeResult` with error code `BLOCKED` — no observation is ever
marked `success=true` for a rejected target (US2 scenario 4).
"""

from __future__ import annotations

import logging
from typing import Any

from scrapy.exceptions import IgnoreRequest

from app_shared.url_safety import UnsafeUrlError, validate_competitor_url

from scrape_core.errors import SSRF_REJECTED_ERROR_CODE

__all__ = ["SsrfGuardMiddleware", "SsrfRejectedError"]

logger = logging.getLogger(__name__)


class SsrfRejectedError(IgnoreRequest):
    """Raised when a request (or a redirect hop) fails the pre-fetch SSRF check.

    Carries `error_code` (`ScrapeErrorCode.BLOCKED`) so
    `scrape_core.errors.classify_exception` can map it to the correct
    §34 code without name-sniffing.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = SSRF_REJECTED_ERROR_CODE


class SsrfGuardMiddleware:
    """Scrapy downloader middleware: pre-fetch scheme/userinfo SSRF guard."""

    @classmethod
    def from_crawler(cls, crawler: Any) -> "SsrfGuardMiddleware":
        return cls()

    def process_request(self, request: Any, spider: Any) -> None:
        try:
            validate_competitor_url(request.url)
        except UnsafeUrlError as exc:
            logger.warning(
                "SsrfGuardMiddleware: rejected %r (%s)", request.url, exc.reason
            )
            raise SsrfRejectedError(
                f"unsafe URL rejected pre-fetch: {request.url} ({exc.reason})"
            ) from exc
        return None
