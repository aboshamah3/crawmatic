"""Save-time SSRF URL-safety validator (`contracts/url-safety.md`, FR-007/008/009).

Pure, framework-agnostic — stdlib `urllib.parse` + `ipaddress` only, **no
DNS resolution**. The §11 mandatory save-time control: authoritative DNS
re-resolution and per-redirect re-validation are the SPEC-07 spider's
fetch-time job (research D2), out of scope here.

:func:`validate_competitor_url` is applied on every write path that
stores a `competitor_url` — single create, update, and bulk-upsert
(FR-009) — never on a read path. An unsafe URL is never stored.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from app_shared.enums import StrEnum

# Internal service hostnames (localhost + docker-compose service names +
# the cloud metadata hostname) — exact match against the lowercased host.
INTERNAL_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "postgres",
        "redis",
        "pgbouncer",
        "api",
        "scheduler",
        "worker",
        "scrapyd-http",
        "scrapyd-browser",
        "metadata.google.internal",
    }
)

# Platform-internal hostname suffixes — the lowercased host is rejected if
# it ends with any of these.
INTERNAL_HOST_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".internal",
    ".railway.internal",
)

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeUrlReason(StrEnum):
    """Why `validate_competitor_url` rejected a URL."""

    INVALID_URL = "INVALID_URL"
    BAD_SCHEME = "BAD_SCHEME"
    USERINFO_PRESENT = "USERINFO_PRESENT"
    PRIVATE_OR_INTERNAL_IP = "PRIVATE_OR_INTERNAL_IP"
    INTERNAL_HOSTNAME = "INTERNAL_HOSTNAME"


class UnsafeUrlError(ValueError):
    """Raised by :func:`validate_competitor_url` on any unsafe URL.

    Routers map this to `422 {"error":{"code":"UNSAFE_URL", ...}}`; the
    bulk-upsert path catches it per-row to build the `rejected[]` report
    (FR-013) instead of aborting the whole batch.
    """

    def __init__(self, reason: UnsafeUrlReason, message: str) -> None:
        self.reason = reason
        super().__init__(message)


def _is_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse `host` as an IP literal (bracketed IPv6 already stripped by `urlsplit`)."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _reject_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True iff `ip` must be rejected (not a safe public address)."""
    return (
        not ip.is_global
        or ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_competitor_url(url: str) -> None:
    """Raise :class:`UnsafeUrlError` unless `url` is a safe, public http(s) target.

    Order (per `contracts/url-safety.md`'s accept rule + reject->reason
    table):
    1. Parse with `urlsplit`; a missing/empty scheme (a relative or
       protocol-relative URL, e.g. `"not-a-url"` or `"//host/x"`) is
       `INVALID_URL`.
    2. Scheme allow-list `{http, https}` — a *present* but disallowed
       scheme (`ftp:`, `file:`, `javascript:`, `data:`, ...) is
       `BAD_SCHEME`, checked before the host-presence check so a
       schemeful-but-hostless URL (`file:///etc/passwd`,
       `javascript:alert(1)`) is correctly reported as a bad scheme, not
       a missing host.
    3. Missing host (e.g. `http://`) is `INVALID_URL`.
    4. Reject embedded userinfo (`user:pass@host`).
    5. Host classification: an IP literal (incl. bracketed IPv6) must be
       `is_global` and none of loopback/private/link-local/reserved/
       multicast/unspecified; a DNS name (lowercased) must not be in
       `INTERNAL_HOSTNAMES` and must not end with an
       `INTERNAL_HOST_SUFFIXES` entry.

    Returns `None` when safe. **No DNS resolution** is ever performed.
    """
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UnsafeUrlError(
            UnsafeUrlReason.INVALID_URL, f"could not parse URL: {url!r}"
        ) from exc

    scheme = parsed.scheme.lower()
    if not scheme:
        raise UnsafeUrlError(
            UnsafeUrlReason.INVALID_URL,
            f"URL has no scheme (relative/protocol-relative): {url!r}",
        )
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            UnsafeUrlReason.BAD_SCHEME,
            f"scheme {parsed.scheme!r} is not allowed (only http/https)",
        )

    host = parsed.hostname
    if not host:
        raise UnsafeUrlError(
            UnsafeUrlReason.INVALID_URL, f"URL has no parseable host: {url!r}"
        )

    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError(
            UnsafeUrlReason.USERINFO_PRESENT,
            "URL must not contain embedded credentials (user:pass@host)",
        )

    host = host.lower()
    ip = _is_ip_literal(host)
    if ip is not None:
        if _reject_ip(ip):
            raise UnsafeUrlError(
                UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP,
                f"host {host!r} is a private/internal/reserved IP address",
            )
        return

    if host in INTERNAL_HOSTNAMES or host.endswith(INTERNAL_HOST_SUFFIXES):
        raise UnsafeUrlError(
            UnsafeUrlReason.INTERNAL_HOSTNAME,
            f"host {host!r} is an internal service hostname",
        )
