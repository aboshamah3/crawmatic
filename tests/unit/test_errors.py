"""`scrape_core/errors.py::classify_exception` unit tests (SPEC-07 tasks.md
T053, FR-005/US2 Acceptance Scenario 1).

Pure/off-reactor -- no Twisted, no real DNS resolution, no reactor.
Exercises the two mechanisms `classify_exception` uses to recognize a
connect-time `SafeResolver` SSRF rejection as `BLOCKED` (not
`UNKNOWN_ERROR`) while a genuine DNS failure still maps to `DNS_ERROR`:

1. An `error_code` attribute set directly on the exception, or found by
   walking its `__cause__`/`__context__` chain (covers a caller that
   re-raises the original exception via `raise ... from`), or an
   `UnsafeResolvedAddressError` recognized by class name anywhere in
   that chain.
2. The `hostname` side-channel (`scrape_core.safety.rejection_registry`)
   -- the actual production path, since Twisted's `HostnameEndpoint`/
   `SimpleResolverComplexifier` machinery discards the original
   exception (class, `error_code`, and cause chain alike) before it
   reaches a spider's `errback`, converting *any* `getHostByName`
   failure into a generic, indistinguishable "0 addresses resolved"
   `DNSLookupError` (verified empirically while authoring this fix --
   see `errors.py`/`rejection_registry.py` docstrings).
"""

from __future__ import annotations

from app_shared.enums import ScrapeErrorCode

from scrape_core.errors import classify_exception, classify_http_status
from scrape_core.safety import rejection_registry
from scrape_core.safety.resolver import UnsafeResolvedAddressError


class _PlainFailure(Exception):
    """A generic, unrelated failure (no `error_code`, no chain, no
    recognizable name substring)."""


class _CannotResolveHostError(Exception):
    """Stands in for `scrapy.exceptions.CannotResolveHostError` -- the
    generic exception that actually reaches a spider `errback` for *both*
    a genuine DNS miss and a `SafeResolver` SSRF rejection, once Twisted's
    `HostnameEndpoint` machinery has discarded the original error."""


def _clear_registry() -> None:
    rejection_registry._rejected_at.clear()  # noqa: SLF001 - test-only reset


# --- direct error_code attribute (pre-existing behavior, still correct) -----


def test_error_code_attribute_is_used_directly() -> None:
    exc = Exception("blocked")
    exc.error_code = ScrapeErrorCode.BLOCKED  # type: ignore[attr-defined]

    assert classify_exception(exc) == ScrapeErrorCode.BLOCKED


# --- UnsafeResolvedAddressError carries error_code=BLOCKED (T053 part a) ----


def test_unsafe_resolved_address_error_carries_blocked_error_code() -> None:
    exc = UnsafeResolvedAddressError("host resolved to unsafe IP")

    assert exc.error_code == ScrapeErrorCode.BLOCKED
    assert classify_exception(exc) == ScrapeErrorCode.BLOCKED


# --- __cause__/__context__ chain walk (T053 part b) --------------------------


def test_classify_exception_walks_cause_chain_for_error_code() -> None:
    original = Exception("rejected")
    original.error_code = ScrapeErrorCode.BLOCKED  # type: ignore[attr-defined]
    try:
        raise _CannotResolveHostError("wrapped") from original
    except _CannotResolveHostError as wrapped:
        assert classify_exception(wrapped) == ScrapeErrorCode.BLOCKED


def test_classify_exception_walks_cause_chain_for_unsafe_resolved_address_error() -> None:
    original = UnsafeResolvedAddressError("host resolved to unsafe IP")
    try:
        raise _CannotResolveHostError("wrapped") from original
    except _CannotResolveHostError as wrapped:
        assert classify_exception(wrapped) == ScrapeErrorCode.BLOCKED


def test_classify_exception_walks_implicit_context_chain() -> None:
    """`raise` inside an `except:` block (no explicit `from`) still links
    via `__context__` -- classify_exception must not require `raise ...
    from ...` specifically."""
    try:
        try:
            raise UnsafeResolvedAddressError("host resolved to unsafe IP")
        except UnsafeResolvedAddressError:
            raise _CannotResolveHostError("wrapped")  # noqa: B904 - deliberate, testing __context__
    except _CannotResolveHostError as wrapped:
        assert classify_exception(wrapped) == ScrapeErrorCode.BLOCKED


# --- hostname side-channel: the actual connect-time-rejection production path


def test_classify_exception_recognizes_recently_rejected_hostname() -> None:
    """The real runtime path (SPEC-07 tasks.md T053): the exception that
    reaches `classify_exception` carries no usable identity at all (no
    `error_code`, no chain to the original rejection) -- only
    `SafeResolver`'s hostname side-channel distinguishes this from a
    genuine DNS failure."""
    _clear_registry()
    generic_exc = _CannotResolveHostError(
        "DNS lookup failed: no results for hostname lookup: private-target.invalid."
    )
    rejection_registry.mark_rejected("private-target.invalid")

    assert classify_exception(generic_exc, hostname="private-target.invalid") == (
        ScrapeErrorCode.BLOCKED
    )


def test_classify_exception_hostname_lookup_is_case_insensitive() -> None:
    _clear_registry()
    generic_exc = _CannotResolveHostError("DNS lookup failed: no results for hostname lookup: Private-Target.Invalid.")
    rejection_registry.mark_rejected("Private-Target.INVALID")

    assert classify_exception(generic_exc, hostname="private-target.invalid") == (
        ScrapeErrorCode.BLOCKED
    )


def test_classify_exception_genuine_dns_failure_still_maps_to_dns_error() -> None:
    """A hostname never marked rejected -- the generic wrapper is a real
    DNS miss, not an SSRF rejection, and must still classify as
    `DNS_ERROR`, never `BLOCKED`."""
    _clear_registry()
    generic_exc = _CannotResolveHostError(
        "DNS lookup failed: no results for hostname lookup: totally-bogus-nonexistent.invalid."
    )

    assert classify_exception(generic_exc, hostname="totally-bogus-nonexistent.invalid") == (
        ScrapeErrorCode.DNS_ERROR
    )


def test_classify_exception_dns_error_without_hostname_argument() -> None:
    """Existing callers that don't pass `hostname` keep today's
    class-name-based classification (backward compatible default)."""
    generic_exc = _CannotResolveHostError("DNS lookup failed")

    assert classify_exception(generic_exc) == ScrapeErrorCode.DNS_ERROR


# --- unrelated failures are unaffected ---------------------------------------


def test_classify_exception_timeout_by_name() -> None:
    class _TimeoutErrorLike(Exception):
        pass

    assert classify_exception(_TimeoutErrorLike("slow")) == ScrapeErrorCode.TIMEOUT


def test_classify_exception_unrecognized_falls_back_to_unknown_error() -> None:
    assert classify_exception(_PlainFailure("boom")) == ScrapeErrorCode.UNKNOWN_ERROR


# --- SPEC-10 US3 (T033): proxy/access failure vocabulary ---------------------


def test_classify_exception_recognizes_tunnel_error_by_name() -> None:
    """Stands in for Scrapy's own
    `scrapy.core.downloader.handlers.http11.TunnelError` (a proxy CONNECT
    failure, incl. a proxy-auth rejection) -- duck-typed by class name,
    same convention as the existing timeout/DNS checks."""

    class _TunnelErrorLike(Exception):
        pass

    assert classify_exception(_TunnelErrorLike("Could not open CONNECT tunnel")) == (
        ScrapeErrorCode.PROXY_FAILED
    )


def test_classify_exception_recognizes_proxy_connect_error_by_name() -> None:
    class _ProxyConnectionError(Exception):
        pass

    assert classify_exception(_ProxyConnectionError("proxy refused connection")) == (
        ScrapeErrorCode.PROXY_FAILED
    )


def test_classify_exception_timeout_still_wins_over_proxy_when_both_named() -> None:
    """Timeout/DNS class-name checks run before the proxy check -- a
    hypothetical exception combining both substrings still prefers the
    more established classification (documents the check order, doesn't
    assert a name no real exception actually uses)."""

    class _ProxyTimeoutErrorLike(Exception):
        pass

    assert classify_exception(_ProxyTimeoutErrorLike("proxy timeout")) == ScrapeErrorCode.TIMEOUT


def test_classify_http_status_407_is_proxy_failed() -> None:
    assert classify_http_status(407) == ScrapeErrorCode.PROXY_FAILED


def test_classify_exception_recognizes_rate_limited_via_error_code_attribute() -> None:
    """`RATE_LIMITED`/`LIMIT_REACHED` are decided directly by
    `generic_price_spider._prepare_dispatch` (a Redis-gating decision,
    not a raised exception) -- but the generic `error_code`-attribute
    chain walk still recognizes either if some caller ever does raise an
    exception carrying one, with no special-casing needed."""
    exc = RuntimeError("rate limited")
    exc.error_code = ScrapeErrorCode.RATE_LIMITED  # type: ignore[attr-defined]

    assert classify_exception(exc) == ScrapeErrorCode.RATE_LIMITED


def test_classify_exception_recognizes_limit_reached_via_error_code_attribute() -> None:
    exc = RuntimeError("budget exhausted")
    exc.error_code = ScrapeErrorCode.LIMIT_REACHED  # type: ignore[attr-defined]

    assert classify_exception(exc) == ScrapeErrorCode.LIMIT_REACHED
