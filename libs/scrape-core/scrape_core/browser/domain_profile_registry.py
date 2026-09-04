"""In-process side-channel carrying each dispatched browser target's
resolved ``ScrapeProfile`` (or any duck-typed stand-in, see
``app_shared.profiles.browser_resource_policy``'s module docstring) to
the ``PLAYWRIGHT_ABORT_REQUEST`` hook, keyed by hostname (EPA B6b).

**Why a registry, not a direct argument**: ``scrapy-playwright`` invokes
``PLAYWRIGHT_ABORT_REQUEST`` (``scrape_core.browser.ssrf.abort_unsafe_request``)
with exactly one positional argument -- the Playwright
``Request`` -- for *every* request on *every* navigation hop
(``scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler._make_request_handler``,
``self.abort_request(playwright_request)``). It never passes the
originating Scrapy request, its ``meta``, or the spider, so there is no
argument-passing seam available to thread ``target.profile`` through
directly -- exactly the same shape of problem
``scrape_core.safety.rejection_registry`` already solves for the
navigation-abort classification side-channel. This module is that same
pattern applied to the resource-blocking policy's per-domain profile:
``generic_browser_price_spider._browser_request_for`` records the
resolved profile for the target's own document hostname right before
dispatching the fetch; ``abort_unsafe_request`` looks it up by the
sub-resource request's own hostname.

**Same-host scoping is deliberate, not an oversight**: a sub-resource
request that shares its hostname with the profile's own domain (e.g. an
in-page ``xhr``/``fetch`` call back to the site's own API) gets that
domain's ``certified_resources``; a genuinely cross-origin sub-resource
(a CDN image host, an ad/analytics network) never matches any key this
module was given and falls back to the caller's own default (``None`` ->
default policy) -- which is exactly correct, since those are precisely
the hosts B6's blocklist exists to catch, never ones a domain's own
certification should be able to launder.

**Failure mode is fail-closed by construction**: a hostname this module
was never told about (no dispatch happened for it yet, or the process
restarted) simply returns ``None`` from :func:`get_domain_profile` --
:func:`~app_shared.profiles.browser_resource_policy.should_block`'s
tolerant ``getattr`` already treats ``None`` identically to "no
certified resources for this domain" (module docstring), so a registry
miss degrades to the plain default blocklist, never to allowing more
through. No TTL/expiry: entries are overwritten (never appended-only),
so the registry's size is bounded by the number of *distinct hostnames*
one Scrapyd job process dispatches to, not by request count -- a stale
entry surviving after a target's fetch completes is harmless (worst
case, a later unrelated request to the same host reuses a slightly-stale
but still-correct-for-that-domain profile).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "set_domain_profile",
    "get_domain_profile",
    "clear_domain_profile",
    "set_domain_transport",
    "get_domain_transport",
    "clear_domain_transport",
]

_profile_by_host: dict[str, Any] = {}

#: EPA B5: the same side-channel, carrying each dispatched target's
#: PROXY/DIRECT transport instead of its profile. Needed for exactly the
#: reason the profile map is: ``PLAYWRIGHT_ABORT_REQUEST`` receives only
#: the bare Playwright ``Request``, never the Scrapy request whose
#: ``meta`` says whether this leg was routed through a paid proxy
#: context. ``generic_browser_price_spider._browser_request_for`` records
#: it from the SAME predicate the ledger prices on
#: (``scrape_core.netledger_middleware._is_proxied``) so the two can
#: never drift into two different notions of "proxied".
_transport_by_host: dict[str, str] = {}

#: What :func:`get_domain_transport` returns for a host nobody recorded.
#: DIRECT is the safe default in both directions: it is what an
#: unproxied leg genuinely is, and it is the value for which B5's
#: document-only rule never fires — an unknown host therefore keeps the
#: pre-B5 decision rather than having sub-resources aborted on a guess.
DEFAULT_TRANSPORT = "DIRECT"


def set_domain_profile(hostname: str | None, profile: Any) -> None:
    """Record `profile` as the resolved profile for `hostname` (case-insensitive).

    A falsy `hostname` (``None``/``""`` -- an unparseable dispatch URL,
    which should never happen in practice but is not this module's job to
    validate) is a silent no-op: nothing is recorded, so a later lookup
    for that same falsy value (which :func:`get_domain_profile` also
    treats as "no entry") still correctly falls back to the default
    policy rather than raising here.
    """
    if not hostname:
        return
    _profile_by_host[hostname.lower()] = profile


def get_domain_profile(hostname: str | None) -> Any | None:
    """The most recently recorded profile for `hostname`, or ``None``.

    ``None`` is the correct "nothing known" signal for
    :func:`~app_shared.profiles.browser_resource_policy.should_block`'s
    `domain_profile` parameter -- never raises for an unknown/falsy
    hostname.
    """
    if not hostname:
        return None
    return _profile_by_host.get(hostname.lower())


def clear_domain_profile(hostname: str | None) -> None:
    """Remove any recorded profile for `hostname`. Test/teardown seam only --
    production call sites never need this (see module docstring: stale
    entries are harmless by construction)."""
    if not hostname:
        return
    _profile_by_host.pop(hostname.lower(), None)


def set_domain_transport(hostname: str | None, transport: str) -> None:
    """Record `transport` (``"PROXY"``/``"DIRECT"``) for `hostname`.

    Same falsy-hostname no-op and same overwrite-never-append bounds as
    :func:`set_domain_profile` (see module docstring). The value is the
    ledger's own transport notion, produced by the caller from
    ``scrape_core.netledger_middleware._is_proxied(meta)`` — this module
    never re-derives it.
    """
    if not hostname:
        return
    _transport_by_host[hostname.lower()] = transport


def get_domain_transport(hostname: str | None) -> str:
    """The transport recorded for `hostname`, or :data:`DEFAULT_TRANSPORT`.

    Never ``None`` and never raises: the caller
    (:func:`scrape_core.browser.ssrf.abort_unsafe_request`) passes the
    result straight into
    :func:`~app_shared.profiles.browser_resource_policy.should_block`,
    whose `transport` parameter has no "unknown" state — a host nobody
    recorded is treated as DIRECT, i.e. as the pre-B5 decision.
    """
    if not hostname:
        return DEFAULT_TRANSPORT
    return _transport_by_host.get(hostname.lower(), DEFAULT_TRANSPORT)


def clear_domain_transport(hostname: str | None) -> None:
    """Remove any recorded transport for `hostname`. Test/teardown seam only."""
    if not hostname:
        return
    _transport_by_host.pop(hostname.lower(), None)
