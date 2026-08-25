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
import re
import string
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

# The only characters a normalized (IDNA-folded, lowercased) DNS host may
# contain. Everything else — a backslash (`http://10.0.0.1\.evil.com/`,
# where WHATWG clients read the host as `10.0.0.1` but `urlsplit` reads
# the whole string), a percent escape (`http://%6c%6fcalhost/`), a raw
# space or NUL — means this validator and the eventual HTTP client would
# disagree about which host is being addressed, and a disagreement
# between parsers is precisely the SSRF primitive. Underscore is allowed:
# it appears in real sub-domains and creates no such ambiguity.
_HOST_NAME_RE = re.compile(r"^[a-z0-9._-]+$")

_HEX_DIGITS = frozenset(string.hexdigits)
_OCTAL_DIGITS = frozenset("01234567")
_DECIMAL_DIGITS = frozenset(string.digits)


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


def _normalize_host(host: str) -> str | None:
    """Fold `host` to the form a resolver would actually look up.

    Two normalizations, both of which a naive validator skips and every
    real resolver applies — so skipping them is a deny-list bypass, not a
    nicety:

    * **IDNA/nameprep.** A non-ASCII host is NFKC-mapped to ASCII before
      the lookup, so `ⓛocalhost`, `localhost。` and `１２７.0.0.1` are
      `localhost`, `localhost.` and `127.0.0.1` to the resolver
      (verified: each resolves to 127.0.0.1 through the system
      resolver).
      Folding here makes the deny lists below see the same string the
      resolver will.
    * **The trailing root dot.** `localhost.` is the fully-qualified
      spelling of `localhost` and resolves identically; without stripping
      it, one keystroke walks past both `INTERNAL_HOSTNAMES` and the
      IP-literal check.

    Returns `None` when the host cannot be folded at all (an
    over-long/empty IDNA label, or a host that is nothing but dots) —
    the caller turns that into `INVALID_URL`, the safe direction.
    """
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except (UnicodeError, UnicodeDecodeError):
            return None

    host = host.lower().rstrip(".")
    return host or None


def _parse_loose_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Decode the non-dotted-quad IPv4 spellings `inet_aton` accepts.

    `ipaddress.ip_address` deliberately only accepts the strict dotted
    quad, so `2130706433`, `0x7f000001`, `017700000001`, `0177.0.0.1` and
    `127.1` all fall out of its IP branch and look like ordinary DNS
    names — while the system resolver (and therefore the actual fetch) resolves
    every one of them to 127.0.0.1. That gap is a working SSRF bypass,
    not a theoretical one, which is why this reimplements `inet_aton`'s
    grammar rather than trusting the strict parser alone:

    * 1–4 dot-separated parts;
    * each part is hex (`0x…`), octal (leading `0`) or decimal;
    * the **last** part absorbs all the bytes the earlier parts left
      over (so `127.1` is `127.0.0.1`, and `10.1` is `10.0.0.1`).

    Returns `None` for anything that is not such a literal — an ordinary
    hostname like `3com.com` must never be mistaken for an address.
    Pure arithmetic: no `socket`, no resolution (this module performs
    neither, by contract).
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None

    values: list[int] = []
    for part in parts:
        if not part:
            return None
        if part[:2] in ("0x", "0X"):
            digits = part[2:]
            if not digits or not set(digits) <= _HEX_DIGITS:
                return None
            values.append(int(digits, 16))
        elif part[0] == "0" and len(part) > 1:
            digits = part[1:]
            if not set(digits) <= _OCTAL_DIGITS:
                return None
            values.append(int(digits, 8))
        else:
            if not set(part) <= _DECIMAL_DIGITS:
                return None
            values.append(int(part, 10))

    *head, last = values
    if any(value > 0xFF for value in head):
        return None
    if last >= 1 << (8 * (4 - len(head))):
        return None

    packed = last
    for index, value in enumerate(head):
        packed |= value << (8 * (3 - index))
    return ipaddress.IPv4Address(packed)


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
    5. Normalize the host to the form a resolver would look up
       (`_normalize_host`: IDNA/NFKC fold + trailing-root-dot strip) —
       an unfoldable host is `INVALID_URL`.
    6. Host classification: an IP literal — strict dotted-quad/IPv6 **or**
       an `inet_aton` decimal/hex/octal/short form (`_parse_loose_ipv4`) —
       must be `is_global` and none of loopback/private/link-local/
       reserved/multicast/unspecified; a DNS name must match
       `_HOST_NAME_RE`, must not be in `INTERNAL_HOSTNAMES`, and must not
       end with an `INTERNAL_HOST_SUFFIXES` entry.

    Steps 5 and 6's loose-IPv4/IDNA/trailing-dot/host-charset handling
    are the READY-013-c hardening: each closed a fixture-demonstrated
    bypass (`tests/unit/test_url_safety_hostile.py`) where this validator
    and the eventual HTTP client disagreed about which host a URL names.

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

    normalized = _normalize_host(host)
    if normalized is None:
        raise UnsafeUrlError(
            UnsafeUrlReason.INVALID_URL,
            f"URL host cannot be normalized to a resolvable name: {url!r}",
        )
    host = normalized

    # An IP literal in *any* spelling the resolver would accept — the
    # strict dotted-quad/IPv6 form first, then the `inet_aton` decimal/
    # hex/octal/short forms. Both are judged by the address they denote,
    # never by their notation, so a public address stays acceptable in
    # either spelling and a private one is rejected in either.
    ip = _is_ip_literal(host) or _parse_loose_ipv4(host)
    if ip is not None:
        if _reject_ip(ip):
            raise UnsafeUrlError(
                UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP,
                f"host {host!r} is a private/internal/reserved IP address",
            )
        return

    if not _HOST_NAME_RE.match(host):
        raise UnsafeUrlError(
            UnsafeUrlReason.INVALID_URL,
            f"URL host contains characters no resolvable hostname has: {url!r}",
        )

    if host in INTERNAL_HOSTNAMES or host.endswith(INTERNAL_HOST_SUFFIXES):
        raise UnsafeUrlError(
            UnsafeUrlReason.INTERNAL_HOSTNAME,
            f"host {host!r} is an internal service hostname",
        )
