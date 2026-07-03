"""In-process, short-TTL record of hostnames `SafeResolver` just rejected.

Twisted's `HostnameEndpoint`/`SimpleResolverComplexifier` machinery
(`twisted.internet.endpoints`, `twisted.internet._resolver`)
unconditionally discards the *original* exception a `DNS_RESOLVER`
-installed `IResolverSimple.getHostByName()` implementation raises --
including its class, message, and any `__cause__`/`__context__` chain --
converting *any* lookup failure (a genuine DNS miss or
`scrape_core.safety.resolver.SafeResolver`'s own unsafe-resolved-address
rejection) into a generic "0 addresses resolved"
`twisted.internet.error.DNSLookupError` (wrapped by Scrapy into
`scrapy.exceptions.CannotResolveHostError`) before it ever reaches a
spider's `errback`. That was verified empirically while building the
SPEC-07 tasks.md T053 fix: neither the rejection's class, its
`error_code` attribute, nor any `__cause__`/`__context__` link to it
survives that trip -- there is nothing left in the final exception for
`scrape_core.errors.classify_exception` to recognize.

This module is the side-channel that closes that gap: `SafeResolver`
records the exact hostname it just rejected (keyed by the same string
passed to `getHostByName`), and `classify_exception` -- given that same
hostname, read off the failed request's URL -- checks whether it was
rejected within the last few seconds (comfortably longer than the
single-reactor-thread hop between the rejection and that same request's
`errback` firing).

Deliberately pure stdlib (a plain `dict` + `time.monotonic`, no
Scrapy/Twisted import) so `scrape_core.errors` can import this module at
module level without pulling Twisted into its own "safe to unit-test
off-reactor" import graph.
"""

from __future__ import annotations

import time

__all__ = ["mark_rejected", "was_recently_rejected"]

# Long enough to comfortably bridge the single-reactor-thread hop between
# `SafeResolver._reject_unsafe` raising and the same request's `errback`
# consulting this registry; short enough that a stale entry from an
# earlier, unrelated request for the same hostname can't leak into a
# later, unrelated classification.
_TTL_SECONDS = 30.0

_rejected_at: dict[str, float] = {}


def _prune(now: float) -> None:
    cutoff = now - _TTL_SECONDS
    stale = [host for host, ts in _rejected_at.items() if ts < cutoff]
    for host in stale:
        del _rejected_at[host]


def mark_rejected(hostname: str) -> None:
    """Record that `hostname` was just refused an unsafe resolved address."""
    now = time.monotonic()
    _prune(now)
    _rejected_at[hostname.lower()] = now


def was_recently_rejected(hostname: str) -> bool:
    """Was `hostname` marked by `mark_rejected` within the last `_TTL_SECONDS`?"""
    now = time.monotonic()
    _prune(now)
    return hostname.lower() in _rejected_at
