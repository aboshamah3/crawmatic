"""Fetch-time URL safety / SSRF (`contracts/fetch-url-safety.md`, research D2).

`validate_resolved_target` extends, rather than duplicates, the save-time
`app_shared.url_safety.validate_competitor_url`: it reuses that
function's scheme allow-list, userinfo rejection, and IP-literal deny
checks verbatim, then adds a resolved-IP check the save-time validator
deliberately does not perform (no DNS resolution at save time, per its
own docstring).

Pure stdlib (`urllib.parse` + `ipaddress`) — no Scrapy/Twisted import
here, so this module (and its resolver/allowlist seam) is fully
unit-testable off-reactor with a fake injected resolver. The two
Scrapy-side enforcement points (`safety.resolver.SafeResolver`,
`safety.middleware.SsrfGuardMiddleware`) are what actually call this at
fetch time; this module has no opinion on how/when it is invoked.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from app_shared.url_safety import (
    UnsafeUrlError,
    UnsafeUrlReason,
    _reject_ip,
    validate_competitor_url,
)

__all__ = ["Resolver", "validate_resolved_target"]

# host -> resolved IP address strings. Injected by the caller: production
# wiring passes the real system resolver; fixture tests inject a fake
# resolver returning a canned public/private/loopback IP (research D2 /
# spec Clarification #3).
Resolver = Callable[[str], "Iterable[str]"]


def validate_resolved_target(
    url: str,
    *,
    resolver: Resolver,
    allowlist: Iterable[str] | None = None,
) -> None:
    """Raise :class:`UnsafeUrlError` unless `url` (and its resolved IP) is safe.

    Order (contracts/fetch-url-safety.md):

    1. `validate_competitor_url(url)` — the save-time scheme/userinfo/
       IP-literal checks (reused verbatim, never re-implemented).
    2. Resolve the host via the **injected** `resolver` callable
       (`host -> Iterable[ip_str]`).
    3. Reject each resolved IP with `app_shared.url_safety._reject_ip`
       (not `is_global`, or loopback/private/link-local/reserved/
       multicast/unspecified) unless it is in the explicit `allowlist`.

    **Injectable seam**: happy-path fixture tests pass a `resolver`
    returning a public IP, or an `allowlist` covering the loopback
    fixture server's resolved address. Production passes the real
    system resolver with `allowlist=None` — prod always validates the
    real resolved IP with no allowlist.

    Returns `None` when safe.
    """
    validate_competitor_url(url)

    host = urlsplit(url).hostname
    if not host:
        # validate_competitor_url already guarantees a parseable host for
        # any URL that reaches this point, but stay defensive rather than
        # let a bare AttributeError leak out of this seam.
        raise UnsafeUrlError(
            UnsafeUrlReason.INVALID_URL, f"URL has no parseable host: {url!r}"
        )
    host = host.lower()

    allowed = frozenset(allowlist) if allowlist is not None else frozenset()

    resolved_ips = list(resolver(host))
    if not resolved_ips:
        raise UnsafeUrlError(
            UnsafeUrlReason.INVALID_URL,
            f"resolver returned no addresses for host {host!r}",
        )

    for ip_str in resolved_ips:
        if ip_str in allowed:
            continue
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise UnsafeUrlError(
                UnsafeUrlReason.INVALID_URL,
                f"resolver returned a non-IP address for host {host!r}: {ip_str!r}",
            ) from exc
        if _reject_ip(ip):
            raise UnsafeUrlError(
                UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP,
                f"host {host!r} resolved to unsafe IP {ip_str!r}",
            )
