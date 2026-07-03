"""Fetch-failure classification helpers (§34 error-code vocabulary).

Per ``contracts/errors.md``: ``ScrapeErrorCode`` (``app_shared.enums``) is
the single structured error-code vocabulary shared by
``price_observations``/``request_attempts``/``match_current_prices``,
this module's classification helpers, and (later) the strategy
optimizer/access-policy tuning/client reporting. This module holds only
the *usage* constants + pure classification helpers a spider/middleware
consults when turning an HTTP status or a raised exception into a
persisted ``error_code`` — it declares no new codes (those live in
``app_shared.enums.ScrapeErrorCode``).

Pure stdlib — no Scrapy/Twisted imports, so it is unit-testable
off-reactor and safe to import from both the extraction/validation
pipeline and the downloader middlewares.
"""

from __future__ import annotations

from app_shared.enums import ScrapeErrorCode

__all__ = [
    "HTTP_403",
    "HTTP_404",
    "HTTP_429",
    "TIMEOUT",
    "DNS_ERROR",
    "PRICE_NOT_FOUND",
    "LOW_CONFIDENCE_PRICE",
    "CURRENCY_MISMATCH",
    "INVALID_PRICE_FORMAT",
    "BLOCKED",
    "UNKNOWN_ERROR",
    "SSRF_REJECTED_ERROR_CODE",
    "ROBOTS_BLOCKED_ERROR_CODE",
    "classify_http_status",
    "classify_exception",
]

# --- §34 codes this slice emits, re-exported as module-level constants for
# convenient `from scrape_core.errors import HTTP_403, ...` call sites
# (contracts/errors.md "Codes used by this slice"). ---
HTTP_403 = ScrapeErrorCode.HTTP_403
HTTP_404 = ScrapeErrorCode.HTTP_404
HTTP_429 = ScrapeErrorCode.HTTP_429
TIMEOUT = ScrapeErrorCode.TIMEOUT
DNS_ERROR = ScrapeErrorCode.DNS_ERROR
PRICE_NOT_FOUND = ScrapeErrorCode.PRICE_NOT_FOUND
LOW_CONFIDENCE_PRICE = ScrapeErrorCode.LOW_CONFIDENCE_PRICE
CURRENCY_MISMATCH = ScrapeErrorCode.CURRENCY_MISMATCH
INVALID_PRICE_FORMAT = ScrapeErrorCode.INVALID_PRICE_FORMAT
BLOCKED = ScrapeErrorCode.BLOCKED
UNKNOWN_ERROR = ScrapeErrorCode.UNKNOWN_ERROR

# An SSRF/unsafe-target rejection (no body download) and a robots-policy
# skip both surface as BLOCKED — there is no dedicated SSRF code in §34
# (contracts/errors.md "Note"). Named aliases document the call site's
# intent without introducing a new code.
SSRF_REJECTED_ERROR_CODE = ScrapeErrorCode.BLOCKED
ROBOTS_BLOCKED_ERROR_CODE = ScrapeErrorCode.BLOCKED

# HTTP status codes with a dedicated §34 member.
_STATUS_CODE_ERRORS: dict[int, ScrapeErrorCode] = {
    403: ScrapeErrorCode.HTTP_403,
    404: ScrapeErrorCode.HTTP_404,
    429: ScrapeErrorCode.HTTP_429,
}


def classify_http_status(status_code: int) -> ScrapeErrorCode | None:
    """Classify a fetch ``status_code`` into a §34 error code.

    Returns ``None`` for a 2xx/3xx status (not a failure — callers only
    invoke this once a response has already been judged a failure).
    403/404/429 map to their dedicated codes; any other 4xx/5xx maps to
    ``UNKNOWN_ERROR`` (no dedicated §34 code exists for it in this
    slice).
    """
    if 200 <= status_code < 400:
        return None
    return _STATUS_CODE_ERRORS.get(status_code, ScrapeErrorCode.UNKNOWN_ERROR)


def classify_exception(exc: BaseException) -> ScrapeErrorCode:
    """Classify a fetch-time exception into a §34 error code.

    Recognizes timeout and DNS-resolution failures by exception class
    name (duck-typed/string-based deliberately, so this module never
    needs to import Twisted/Scrapy exception types — pure stdlib, safe
    to unit-test off-reactor with plain ``Exception`` subclasses).
    Anything unrecognized maps to ``UNKNOWN_ERROR``.
    """
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return ScrapeErrorCode.TIMEOUT
    if "dns" in name or "nameresolution" in name or "domainerror" in name:
        return ScrapeErrorCode.DNS_ERROR
    return ScrapeErrorCode.UNKNOWN_ERROR
