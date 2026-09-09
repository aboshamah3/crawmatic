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

import re

from app_shared.enums import ScrapeErrorCode

from scrape_core.safety.rejection_registry import was_recently_rejected

# SPEC-14 (R3): Playwright's own `TimeoutError` is imported defensively --
# the browser-only project (`apps/scrapers-browser`) always has `playwright`
# installed, but this module is also imported by `apps/scrapers` (the HTTP
# project) and by `app_shared`-adjacent test environments that may not have
# it, so a hard top-level `import playwright` here would make this whole
# module (and everything that imports it) fail to import wherever Playwright
# isn't present. `None` on `ImportError` keeps `classify_playwright_exception`
# working via its duck-typed-by-name fallback below.
try:
    from playwright.async_api import TimeoutError as _PlaywrightTimeoutError
except ImportError:  # pragma: no cover - exercised only where playwright is absent
    _PlaywrightTimeoutError = None

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
    "PLAYWRIGHT_FAILED",
    "NOT_LISTED",
    "IDENTITY_MISMATCH",
    "POLICY_BLOCKED",
    "CONNECTION_FAILED",
    "TLS_CONNECTION_FAILED",
    "TLS_VERIFICATION_FAILED",
    "PROTOCOL_FAILED",
    "SSRF_REJECTED_ERROR_CODE",
    "ROBOTS_BLOCKED_ERROR_CODE",
    "classify_http_status",
    "classify_exception",
    "classify_playwright_exception",
    # EPA C1/F08 (2026-09-07)
    "classify_extraction_outcome",
    "classify_timeout_phase",
    "page_carries_product_identity",
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

# SPEC-14 (R3): the browser spider's single-attempt failure classification
# reuses this same forward-compat member (declared by SPEC-07, never a new
# enum value) -- see classify_playwright_exception below.
PLAYWRIGHT_FAILED = ScrapeErrorCode.PLAYWRIGHT_FAILED
NOT_LISTED = ScrapeErrorCode.NOT_LISTED
IDENTITY_MISMATCH = ScrapeErrorCode.IDENTITY_MISMATCH
POLICY_BLOCKED = ScrapeErrorCode.POLICY_BLOCKED
CONNECTION_FAILED = ScrapeErrorCode.CONNECTION_FAILED
TLS_CONNECTION_FAILED = ScrapeErrorCode.TLS_CONNECTION_FAILED
TLS_VERIFICATION_FAILED = ScrapeErrorCode.TLS_VERIFICATION_FAILED
PROTOCOL_FAILED = ScrapeErrorCode.PROTOCOL_FAILED

# An SSRF/unsafe-target rejection (no body download) and a robots-policy
# skip both surface as BLOCKED — there is no dedicated SSRF code in §34
# (contracts/errors.md "Note"). Named aliases document the call site's
# intent without introducing a new code.
SSRF_REJECTED_ERROR_CODE = ScrapeErrorCode.BLOCKED
ROBOTS_BLOCKED_ERROR_CODE = ScrapeErrorCode.POLICY_BLOCKED

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

    # 2026-08-03: Scrapy's HttpErrorMiddleware filters every non-2xx to the
    # errback as `HttpError`, so a spider's `parse` never sees one and the
    # `classify_http_status` branch there is unreachable in practice. Left
    # unhandled these all landed as UNKNOWN_ERROR -- 717 S-Tech throttle
    # responses in the 2026-08-03 run were indistinguishable from genuine
    # crashes. Duck-typed on `.response.status` (never importing Scrapy
    # here, per this module's stdlib-only contract) so the real status
    # classifies exactly as it would have in `parse`.
    status = getattr(getattr(exc, "response", None), "status", None)
    if isinstance(status, int):
        status_code = classify_http_status(status)
        if status_code is not None:
            return status_code

    name = type(exc).__name__.lower()
    message = str(exc).lower()
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
    transport_code = _classify_transport_exception(exc, name=name, message=message)
    if transport_code is not None:
        return transport_code
    return ScrapeErrorCode.UNKNOWN_ERROR


_CURL_CODE_PATTERN = re.compile(
    r"(?:curl(?: error)?(?: code)?\s*[:=(]?\s*|curl:\s*\()(\d+)\)?",
    re.IGNORECASE,
)


def _classify_transport_exception(
    exc: BaseException,
    *,
    name: str | None = None,
    message: str | None = None,
) -> ScrapeErrorCode | None:
    """Map recognizable curl/Twisted transport failures to stable outcomes."""
    name = name or type(exc).__name__.lower()
    message = message or str(exc).lower()

    curl_code = getattr(exc, "curl_code", None)
    if not isinstance(curl_code, int) and "curl" in name:
        candidate = getattr(exc, "code", None)
        curl_code = candidate if isinstance(candidate, int) else None
    if not isinstance(curl_code, int):
        match = _CURL_CODE_PATTERN.search(message)
        curl_code = int(match.group(1)) if match else None

    if curl_code == 16:
        return ScrapeErrorCode.PROTOCOL_FAILED
    if curl_code == 35:
        return ScrapeErrorCode.TLS_CONNECTION_FAILED
    if curl_code == 60:
        return ScrapeErrorCode.TLS_VERIFICATION_FAILED
    if curl_code == 7:
        if "proxy" in message or "connect tunnel" in message:
            return ScrapeErrorCode.PROXY_FAILED
        return ScrapeErrorCode.CONNECTION_FAILED

    if "certificate" in message and any(
        marker in message for marker in ("issuer", "verify", "verification", "unable to get")
    ):
        return ScrapeErrorCode.TLS_VERIFICATION_FAILED
    if any(marker in message for marker in ("http/2", "http2", "settings frame")) and any(
        marker in message for marker in ("protocol", "settings", "stream")
    ):
        return ScrapeErrorCode.PROTOCOL_FAILED
    if "tls" in message and any(
        marker in message for marker in ("abrupt", "closed", "close", "handshake", "eof")
    ):
        return ScrapeErrorCode.TLS_CONNECTION_FAILED
    if "connectionlost" in name or name in {"connectionlost", "connectiondone"}:
        return ScrapeErrorCode.CONNECTION_FAILED
    return None


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


def classify_playwright_exception(exc: BaseException) -> ScrapeErrorCode:
    """Classify a browser (Playwright) fetch-time exception into a §34
    error code (SPEC-14, R3, `contracts/browser-spider.md`).

    Playwright's own ``TimeoutError`` (raised on a navigation/
    ``wait_for_selector``/``wait_for_load_state`` timeout) classifies as
    ``TIMEOUT``; any other Playwright error (browser/context crash,
    protocol error, target closed, etc.) classifies as
    ``PLAYWRIGHT_FAILED`` — both reuse existing `ScrapeErrorCode`
    members declared forward-compat by SPEC-07 (no enum change).

    Checks the imported ``playwright.async_api.TimeoutError`` type first
    (precise); falls back to a duck-typed by-name check (``"timeout" in
    type(exc).__name__.lower()``) so this still classifies correctly in
    an environment where Playwright wasn't importable at module load
    time (this module's own top-level import of it is defensive — see
    the module docstring) or where a wrapper reraises a differently
    packaged timeout exception. Every other exception (browser crashed,
    a selector/element error not otherwise recognized, etc.) is
    ``PLAYWRIGHT_FAILED`` — the single-attempt browser path has no
    retry ladder to fall back through (R4), so this is the catch-all
    "the browser fetch itself failed" code.
    """
    if _PlaywrightTimeoutError is not None and isinstance(exc, _PlaywrightTimeoutError):
        return ScrapeErrorCode.TIMEOUT
    if "timeout" in type(exc).__name__.lower():
        return ScrapeErrorCode.TIMEOUT
    transport_code = _classify_transport_exception(exc)
    if transport_code is not None:
        return transport_code
    return ScrapeErrorCode.PLAYWRIGHT_FAILED


# ---------------------------------------------------------------------------
# EPA C1 / F08 (2026-09-07): failure classes the old vocabulary conflated
# ---------------------------------------------------------------------------
#
# Two conflations cost real money, and both are fixed by *reading what we
# already measured* rather than by fetching anything more:
#
# 1. `TIMEOUT` blamed nobody. A connect timeout is the proxy vendor's
#    problem, a TTFB timeout is the host's, and a read timeout is page
#    weight -- three different fixes behind one code, so the per-domain
#    timeout tuner and the access-policy tuner both had to guess. A5
#    already splits `request_attempts` into `connect_ms`/`ttfb_ms`/
#    `read_ms`, so the phase that never completed is knowable.
#
# 2. `PRICE_NOT_FOUND` was claimed for any 200 with no price -- including
#    interstitials, consent walls, soft-404s and un-rendered JS shells.
#    That is a much stronger claim than the evidence supports, and it is
#    the claim the strategy optimizer, rediscovery and the domain
#    scorecard all learn from. Deep dive §6.1: "200 and fast is not
#    success."


def classify_timeout_phase(
    *,
    connect_ms: int | None = None,
    ttfb_ms: int | None = None,
    read_ms: int | None = None,
) -> ScrapeErrorCode:
    """Refine a timeout into the phase that never completed.

    The three arguments are exactly A5's ``request_attempts`` timing
    split, and ``None`` keeps its meaning there: *this boundary was never
    reached*, never "zero milliseconds". So the first ``None`` in
    lifecycle order names the phase that timed out.

    Falls back to the undifferentiated ``TIMEOUT`` when every phase
    completed (the timeout came from somewhere else -- an overall
    deadline, extraction) or when a transport reports no split at all.
    Deliberately opt-in and separate from :func:`classify_exception`:
    that function's ``TIMEOUT`` answer is unchanged, so no historical row
    and no existing caller is silently re-classified.
    """
    if connect_ms is None:
        return ScrapeErrorCode.CONNECT_TIMEOUT
    if ttfb_ms is None:
        return ScrapeErrorCode.TTFB_TIMEOUT
    if read_ms is None:
        return ScrapeErrorCode.READ_TIMEOUT
    return ScrapeErrorCode.TIMEOUT


def page_carries_product_identity(
    *,
    title: str | None = None,
    product_name: str | None = None,
    sku: str | None = None,
    structured_product_data: bool = False,
) -> bool:
    """Whether a 200 response looks like a product page at all.

    Deliberately generous: ANY one signal is enough. The question this
    answers is not "is this the right product" (that is identity
    validation, and it has its own codes) but the much weaker "did we get
    a product page or a wall". Being generous here means
    ``EXTRACTION_FAILED`` is only ever claimed when the page carried
    nothing product-shaped whatsoever -- the case where calling it
    ``PRICE_NOT_FOUND`` would teach the optimizer something false.

    Whitespace-only strings do not count: an empty ``<title></title>`` is
    the absence of a title, not a title.
    """
    if structured_product_data:
        return True
    return any(
        isinstance(value, str) and value.strip()
        for value in (title, product_name, sku)
    )


def classify_extraction_outcome(
    *,
    status_code: int,
    price_found: bool,
    has_product_identity: bool,
    identity_matches_target: bool | None = None,
) -> ScrapeErrorCode | None:
    """Classify what a fetched page actually produced.

    Returns ``None`` for a success (a price was extracted). Otherwise, in
    order:

    * a non-2xx/3xx status defers to :func:`classify_http_status` -- the
      status is the stronger evidence and always wins;
    * **no product identity at all** -> ``EXTRACTION_FAILED``. We cannot
      tell what we fetched, so this is evidence about our ACCESS PATH (an
      interstitial, a consent wall, a soft-404, a JS shell we never
      rendered) and about nothing else. It is also what makes
      ``AttemptBudget.suppress`` refuse the same method again for this
      target: re-running it would fetch the same wall at full price;
    * identity present but proven to be a DIFFERENT product ->
      ``IDENTITY_MISMATCH`` (unchanged, pre-existing code);
    * identity present, and either matched or unchecked ->
      ``PRICE_NOT_FOUND``, which now means what it always claimed to
      mean: we read this product's genuine page and it carried no price.
      That is a listing verdict, and the only one of these four the
      strategy optimizer and the domain scorecard should ever learn from.

    ``identity_matches_target=None`` means "not checked", not "mismatch"
    -- an unchecked page with a title stays ``PRICE_NOT_FOUND`` rather
    than being upgraded to an accusation we cannot support.
    """
    status_error = classify_http_status(status_code)
    if status_error is not None:
        return status_error
    if price_found:
        return None
    if not has_product_identity:
        return ScrapeErrorCode.EXTRACTION_FAILED
    if identity_matches_target is False:
        return ScrapeErrorCode.IDENTITY_MISMATCH
    return ScrapeErrorCode.PRICE_NOT_FOUND
