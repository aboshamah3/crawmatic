"""`SafeResolver` — the connect-time SSRF defense (research D2).

Installed via Scrapy's `DNS_RESOLVER` setting
(`scrape_core.safety.resolver.SafeResolver`), this wraps Scrapy's own
caching threaded resolver and **refuses to hand back an unsafe IP**: the
socket literally cannot connect to a private/loopback/link-local/
reserved/multicast/unspecified address, because the resolver itself
raises before the connection is attempted. This is what defeats DNS
rebinding at connect time — the address a Scrapy request actually
connects to is the one validated here, not merely the one seen when the
request was built (which `safety.middleware.SsrfGuardMiddleware`
checks, but cannot itself re-resolve without duplicating this).

Subclasses `scrapy.resolver.CachingThreadedResolver` (Scrapy's own
`DNS_RESOLVER` default) purely to reuse its threaded/caching DNS lookup
machinery — the only change here is rejecting an unsafe result before
handing it back.

`UnsafeResolvedAddressError` carries `error_code = ScrapeErrorCode.BLOCKED`
(SPEC-07 tasks.md T053, FR-005/US2 AC1) so a direct classification of
*this* exception recognizes it without name-sniffing. That attribute
alone is not enough to fix the real bug, though: Twisted's
`HostnameEndpoint`/`SimpleResolverComplexifier` machinery unconditionally
discards whatever `getHostByName()` raises -- class, message, and
`error_code` alike -- converting the failure into a generic "0
addresses resolved" `DNSLookupError` before it ever reaches the spider's
`errback` (verified empirically; see `rejection_registry`'s docstring).
`_reject_unsafe` therefore also records the rejected hostname in
`rejection_registry` -- the side-channel `scrape_core.errors.
classify_exception` consults (given the failed request's hostname) to
recognize the rejection despite the generic exception Twisted actually
delivers.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

from scrapy.resolver import CachingThreadedResolver

from app_shared.enums import ScrapeErrorCode
from app_shared.url_safety import _reject_ip

from scrape_core.safety.rejection_registry import mark_rejected

if TYPE_CHECKING:
    from collections.abc import Sequence

    from twisted.internet.defer import Deferred

__all__ = ["SafeResolver", "UnsafeResolvedAddressError"]


class UnsafeResolvedAddressError(Exception):
    """Raised when DNS resolution yields an address `_reject_ip` denies.

    Surfacing this as a plain `Exception` (not `IgnoreRequest`) keeps
    this module Scrapy-request-agnostic — Twisted's connection machinery
    treats any resolver-callback exception as a connection failure,
    which propagates to the spider's `errback` like any other
    connection error (classified by `scrape_core.errors.classify_exception`).

    Carries `error_code = ScrapeErrorCode.BLOCKED` for any caller that
    classifies this exception directly (e.g. a unit test, or a future
    codepath that doesn't route through the Twisted `HostnameEndpoint`
    machinery that otherwise discards it) -- see module docstring for
    why the real fix also needs `rejection_registry`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = ScrapeErrorCode.BLOCKED


class SafeResolver(CachingThreadedResolver):
    """Resolve via the normal caching/threaded lookup, then reject an unsafe IP."""

    def getHostByName(
        self, name: str, timeout: "Sequence[int]" = ()
    ) -> "Deferred[str]":
        deferred = super().getHostByName(name, timeout)
        deferred.addCallback(self._reject_unsafe, name)
        return deferred

    @staticmethod
    def _reject_unsafe(ip_str: str, name: str) -> str:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            mark_rejected(name)
            raise UnsafeResolvedAddressError(
                f"resolved address for {name!r} is not a valid IP: {ip_str!r}"
            ) from exc
        if _reject_ip(ip):
            mark_rejected(name)
            raise UnsafeResolvedAddressError(
                f"host {name!r} resolved to unsafe IP {ip_str!r}; connection refused"
            )
        return ip_str
