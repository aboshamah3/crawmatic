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
pipeline and the downloader middlewares. (`classify_exception`'s
optional `hostname` argument reads `scrape_core.safety.
rejection_registry`, which is itself pure stdlib for the same reason —
see that module's docstring.)

SPEC-10 US3 (T033, ``contracts/spider-integration.md`` §4) extends this
module's vocabulary coverage for the access-policy/proxy failure paths
the spider's request-side (SPEC-10 US2, T026) now produces, reusing the
existing ``ScrapeErrorCode`` members only (no enum change):

* A proxy CONNECT/tunnel failure (Scrapy's own
  ``scrapy.core.downloader.handlers.http11.TunnelError``, or any
  similarly-named proxy-connect/auth exception) is recognized by class
  name — duck-typed, same convention as the existing timeout/DNS
  checks — and classified ``PROXY_FAILED``.
* A ``407 Proxy Authentication Required`` response status is classified
  ``PROXY_FAILED`` via :func:`classify_http_status`.
* ``RATE_LIMITED``/``LIMIT_REACHED`` (the rate-ceiling/cooldown-defer and
  proxy-budget-exhaustion outcomes) are decided directly by
  ``generic_price_spider._prepare_dispatch`` — a pure Redis-gating
  decision, not a raised exception — so the spider stamps those two
  codes straight from ``app_shared.enums.ScrapeErrorCode`` without ever
  calling :func:`classify_exception`. The generic ``error_code``-attribute
  chain walk below still recognizes either code if some future caller
  *does* raise an exception carrying one (e.g. ``exc.error_code =
  ScrapeErrorCode.LIMIT_REACHED``) — no special-casing needed for that.
"""

from __future__ import annotations

from app_shared.enums import ScrapeErrorCode

from scrape_core.safety.rejection_registry import was_recently_rejected

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
    "PROXY_FAILED",
    "RATE_LIMITED",
    "LIMIT_REACHED",
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

# --- SPEC-10 US3 (T033) additions: proxy/access failure vocabulary, reusing
# the ScrapeErrorCode members already declared forward-compat by SPEC-07
# (see module docstring "no enum change"). ---
PROXY_FAILED = ScrapeErrorCode.PROXY_FAILED
RATE_LIMITED = ScrapeErrorCode.RATE_LIMITED
LIMIT_REACHED = ScrapeErrorCode.LIMIT_REACHED

# An SSRF/unsafe-target rejection (no body download) and a robots-policy
# skip both surface as BLOCKED — there is no dedicated SSRF code in §34
# (contracts/errors.md "Note"). Named aliases document the call site's
# intent without introducing a new code.
SSRF_REJECTED_ERROR_CODE = ScrapeErrorCode.BLOCKED
ROBOTS_BLOCKED_ERROR_CODE = ScrapeErrorCode.BLOCKED

# HTTP status codes with a dedicated §34 member. 407 (Proxy Authentication
# Required) is a proxy-specific failure (SPEC-10 US3) -- distinct from the
# target's own 403/404/429.
_STATUS_CODE_ERRORS: dict[int, ScrapeErrorCode] = {
    403: ScrapeErrorCode.HTTP_403,
    404: ScrapeErrorCode.HTTP_404,
    407: ScrapeErrorCode.PROXY_FAILED,
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


def classify_exception(
    exc: BaseException, *, hostname: str | None = None
) -> ScrapeErrorCode:
    """Classify a fetch-time exception into a §34 error code.

    Checks for an explicit ``error_code`` attribute first — the SSRF
    guard (``scrape_core.safety.middleware.SsrfRejectedError``) and the
    robots middleware (``scrape_core.robots.RobotsBlockedError``) both
    set one so their rejection surfaces as ``BLOCKED`` without relying
    on class-name sniffing, and walks the wrapped exception's
    ``__cause__``/``__context__`` chain to find that attribute (or a
    by-name ``UnsafeResolvedAddressError``) if a wrapper layer re-raised
    it via ``raise ... from``.

    That chain walk covers callers that *do* preserve the original
    exception. It does **not** cover the connect-time
    ``scrape_core.safety.resolver.SafeResolver`` rejection in its actual
    runtime path: Twisted's ``HostnameEndpoint``/
    ``SimpleResolverComplexifier`` machinery unconditionally discards
    whatever ``getHostByName()`` raised — class, ``error_code``, and any
    cause chain — before a spider's ``errback`` ever sees it, replacing
    it with a generic "0 addresses resolved" ``DNSLookupError`` /
    ``CannotResolveHostError`` indistinguishable from a genuine DNS miss
    (verified empirically; SPEC-07 tasks.md T053). For that path, pass
    the failed request's ``hostname`` — ``SafeResolver`` records a
    rejected hostname in ``scrape_core.safety.rejection_registry``, and
    a recent match there is the only way to recognize the rejection.

    Otherwise recognizes timeout, DNS-resolution, and (SPEC-10 US3)
    proxy connect/tunnel failures by exception class name (duck-typed/
    string-based deliberately, so this module never needs to import
    Twisted/Scrapy exception types for *this* part — pure stdlib, safe
    to unit-test off-reactor with plain ``Exception`` subclasses).
    Anything unrecognized maps to ``UNKNOWN_ERROR``.
    """
    error_code = _chained_error_code(exc)
    if error_code is not None:
        return error_code

    if hostname and was_recently_rejected(hostname):
        return ScrapeErrorCode.BLOCKED

    name = type(exc).__name__.lower()
    if "timeout" in name:
        return ScrapeErrorCode.TIMEOUT
    if (
        "dns" in name
        or "nameresolution" in name
        or "domainerror" in name
        or "resolvehost" in name
    ):
        return ScrapeErrorCode.DNS_ERROR
    # SPEC-10 US3: a proxy CONNECT/tunnel failure -- Scrapy's own
    # `scrapy.core.downloader.handlers.http11.TunnelError` (raised when
    # the configured HTTP proxy refuses/can't complete a CONNECT, incl.
    # a proxy-auth rejection) or any similarly-named proxy-connect
    # exception. Checked after timeout/DNS so a class name that happens
    # to combine both (unlikely, but keeps this additive) still prefers
    # the more specific proxy classification only when neither matched.
    if "tunnel" in name or "proxy" in name:
        return ScrapeErrorCode.PROXY_FAILED
    return ScrapeErrorCode.UNKNOWN_ERROR


def _chained_error_code(exc: BaseException) -> ScrapeErrorCode | None:
    """Walk ``exc``'s ``__cause__``/``__context__`` chain for a §34 code.

    Recognizes an explicit ``error_code`` attribute at any depth, or an
    ``UnsafeResolvedAddressError`` by class name (so a caller that
    doesn't import ``scrape_core.safety.resolver`` — keeping this module
    free of a hard Twisted dependency — still recognizes it if it's ever
    found intact in the chain).
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        error_code = getattr(current, "error_code", None)
        if isinstance(error_code, ScrapeErrorCode):
            return error_code
        if type(current).__name__ == "UnsafeResolvedAddressError":
            return ScrapeErrorCode.BLOCKED
        current = current.__cause__ or current.__context__
    return None
