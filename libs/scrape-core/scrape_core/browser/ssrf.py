"""``PLAYWRIGHT_ABORT_REQUEST`` guard — route-layer SSRF re-validation and
sub-resource policy (SPEC-14 T030, US4, `contracts/browser-safety.md` R6;
re-scoped by READY F01 / plan task A1).

**This module is defense in depth, NOT the enforcement point.** Read that
first, because the previous version of this docstring claimed the
opposite and the claim was wrong in a specific, exploitable way:

* ``scrapy-playwright`` attaches its ``route`` handler per navigation, and
  Chromium follows a 3xx ``Location`` **inside its own network stack**.
  This hook therefore sees only the **first request of a redirect
  chain** — hop 2..N never reach it at all. A public first hop that
  redirects to ``http://127.0.0.1:6379/`` was fetched, with this hook
  reporting nothing.
* Even for the requests it does see, it is *check-then-connect*: it
  resolves the name, decides, and then Chromium resolves the name again
  for the actual socket. A short-TTL answer changes in between, so the
  address validated is not necessarily the address dialed.

:mod:`scrape_core.browser.egress_guard` is the enforcement point. Chromium
is launched behind it (``--proxy-server=http://127.0.0.1:<port>``,
``--proxy-bypass-list=<-loopback>``), so **every** connection the browser
opens — each redirect hop, every sub-resource, service workers, popups and
WebSockets — is validated there and then dialed on the address the guard
itself resolved. That is what closes both gaps, and it is what
Constitution §VI is satisfied by for the browser path.

What this hook is still for, and why it stays wired:

* it is the only place that can abort a request *before its body loads*
  while still telling the spider **why** (``rejection_registry``, below),
  which is what keeps a blocked navigation classified ``BLOCKED`` rather
  than a generic network error;
* it carries the sub-resource cost/category block policy (EPA B6), which
  is a billing decision the guard has no business making;
* it fails a navigation on the *first* hop early, before Chromium spends
  a connection on it;
* it keeps working if the guard is ever disabled
  (``BROWSER_EGRESS_GUARD_ENABLED``), leaving degraded coverage rather
  than none.

Reuses, never re-implements, the existing SSRF logic: the scheme/
userinfo/IP-literal checks and the resolved-IP `_reject_ip` deny rule
both live in :func:`scrape_core.safety.fetch.validate_resolved_target`
(itself built on ``app_shared.url_safety.validate_competitor_url``/
``_reject_ip``). This module only supplies the scrapy-playwright-shaped
seam (navigation-vs-subresource gating, the off-event-loop-thread
resolve, and the ``rejection_registry`` side-channel a real abort needs
so the browser errback can still recognize *why* the fetch failed —
see below).

**Scope**: navigation/document requests
(``request.is_navigation_request()`` or ``resource_type == "document"``)
get the resolved-IP re-validation below. Sub-resource requests (EPA B6,
`app_shared.profiles.browser_resource_policy`) go through
:func:`~app_shared.profiles.browser_resource_policy.evaluate_request`:
a cheap, no-DNS URL/redirect safety check
(``app_shared.url_safety.validate_competitor_url``) FIRST, then the
versioned cost/category block policy (image/media/font/stylesheet by
resource type; ads/analytics/social by host, regardless of type) —
never the reverse, so a "certified" resource type can never launder an
unsafe URL through. Neither check marks the rejection registry for a
sub-resource (see below) — only a navigation abort does.

**Sub-resource DNS validation (READY F01)**: a sub-resource that survives
the policy above then gets the same DNS-aware resolved-IP check a
navigation gets — the no-DNS URL check alone passes
``http://internal.example/x.js`` whenever ``internal.example`` merely
*resolves* to 10.0.0.5. Doing that per sub-resource would be one DNS
lookup per asset, so the verdict is memoised per host for
``_SUBRESOURCE_DNS_CACHE_TTL_SECONDS`` (60 s). The memo is dropped at
every browser-CONTEXT switch
(:func:`clear_subresource_dns_cache`, called by the spider when
``meta["playwright_context"]`` changes): a new context is a new network
view, and a verdict carried across that boundary is precisely the
rebinding window the memo would otherwise open.

**Off-event-loop-thread resolve**: ``abort_unsafe_request`` runs as a
native coroutine inside the same asyncio loop scrapy-playwright/
``AsyncioSelectorReactor`` share (never a Twisted ``Deferred`` context),
so the blocking DNS resolution + `_reject_ip` check is offloaded via
``loop.run_in_executor`` — the asyncio-native equivalent of
``scrape_core.db.run_in_thread`` for this call site — never performed
directly on the event-loop thread.

**Why ``rejection_registry`` is needed here too**: when this function
returns ``True``, scrapy-playwright's handler calls ``route.abort()`` at
the Chromium network layer — the navigation simply fails with a generic
Playwright network error (e.g. ``net::ERR_FAILED``/``net::ERR_ABORTED``),
carrying no ``error_code`` of ours and no recognizable exception type.
That is the exact same "the real rejection reason gets discarded before
reaching the spider's ``errback``" problem
``scrape_core.safety.resolver.SafeResolver``/``rejection_registry``
already solves for the HTTP path's connect-time rejection (see that
module's docstring) — so this function marks the rejected hostname via
the same :func:`scrape_core.safety.rejection_registry.mark_rejected`
side-channel, letting ``scrape_core.errors.classify_exception``'s
``hostname``-keyed lookup recognize it and classify ``BLOCKED``
(`contracts/browser-safety.md` "Guarantee").
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from app_shared.profiles.browser_resource_policy import BLOCKLIST_VERSION as RESOURCE_POLICY_VERSION
from app_shared.profiles.browser_resource_policy import evaluate_request as evaluate_resource_request
from app_shared.url_safety import UnsafeUrlError

from scrape_core.browser.domain_profile_registry import get_domain_profile, get_domain_transport
from scrape_core.observability import log_event
from scrape_core.safety.fetch import Resolver, system_resolver, validate_resolved_target
from scrape_core.safety.rejection_registry import mark_rejected

if TYPE_CHECKING:  # pragma: no cover - type-checking only, never imported at runtime here
    # Deferred/type-only import (mirrors `scrape_core.browser.variant`'s "import
    # policy note"): this module must stay importable wherever `scrape_core` is
    # (e.g. `apps/scrapers`, which has no playwright dependency) even though
    # only the browser-only project ever actually wires this in as
    # `PLAYWRIGHT_ABORT_REQUEST`.
    from playwright.async_api import Request as PlaywrightRequest

logger = logging.getLogger(__name__)

__all__ = ["abort_unsafe_request", "clear_subresource_dns_cache"]

# EPA B6: sub-resource type/host-category cost blocking now lives in
# `app_shared.profiles.browser_resource_policy` (versioned, tested,
# certifiable per domain) -- this module no longer hardcodes a resource
# type set of its own. See `evaluate_resource_request` below and that
# module's docstring for the current default blocklist and the
# PLAN_AMAZON_NOON_PRICING Phase 2 origin of the image/media/font/
# stylesheet cost finding. These aborts never touch `rejection_registry`
# here either: marking the hostname would make `classify_exception`
# misread an ordinary asset abort as BLOCKED.
#
# EPA B6b: the sub-resource request's own hostname is looked up in
# `scrape_core.browser.domain_profile_registry` (see that module's
# docstring for why a registry, not a direct argument, is the only
# available seam here) to recover the real resolved `ScrapeProfile` the
# spider dispatched for that domain -- `generic_browser_price_spider.
# _browser_request_for` populates it right before dispatch. A registry
# miss (domain never dispatched in this process, or genuinely
# unresolvable) returns `None`, which is `should_block`'s documented
# default-policy value -- fail-closed to blocking, never to allowing.

#: Real (blocking) system DNS resolver — the production default. Lives in
#: :mod:`scrape_core.safety.fetch` so the browser guard and the off-reactor
#: discovery probe (``apps/workers``) share one definition. Always invoked
#: through ``loop.run_in_executor`` by :func:`abort_unsafe_request`, never
#: called directly on the event-loop thread.
_system_resolver = system_resolver

#: How long a sub-resource host's DNS-safety verdict is reused (READY
#: F01). A page pulls dozens of assets from a handful of hosts; without a
#: memo the DNS-aware check would be one resolve per asset. Bounded low
#: because the memo IS a rebinding window for its own lifetime -- 60 s is
#: shorter than any page's own lifetime here and shorter than the
#: navigation timeout, and the memo is dropped outright at every context
#: switch (see the module docstring).
_SUBRESOURCE_DNS_CACHE_TTL_SECONDS = 60.0

#: `host -> (expires_at_monotonic, is_safe)`. Guarded by a plain lock, not
#: an asyncio one: `abort_unsafe_request` runs on the reactor's loop but
#: the validation itself happens in a `run_in_executor` worker thread, so
#: the two sides are genuinely cross-thread.
_subresource_dns_cache: dict[str, tuple[float, bool]] = {}
_subresource_dns_lock = threading.Lock()


def clear_subresource_dns_cache() -> None:
    """Drop every memoised sub-resource DNS verdict.

    Called by the spider at each browser-context switch (see the module
    docstring for why the memo must not outlive a context). Safe to call
    from any thread and at any time -- the only cost of clearing too often
    is an extra DNS lookup.
    """
    with _subresource_dns_lock:
        _subresource_dns_cache.clear()


def _cached_subresource_verdict(host: str) -> bool | None:
    now = time.monotonic()
    with _subresource_dns_lock:
        entry = _subresource_dns_cache.get(host)
        if entry is None:
            return None
        expires_at, is_safe = entry
        if expires_at <= now:
            del _subresource_dns_cache[host]
            return None
        return is_safe


def _remember_subresource_verdict(host: str, is_safe: bool) -> None:
    with _subresource_dns_lock:
        _subresource_dns_cache[host] = (
            time.monotonic() + _SUBRESOURCE_DNS_CACHE_TTL_SECONDS,
            is_safe,
        )


async def _subresource_host_is_safe(
    url: str, host: str | None, resolver: Resolver
) -> bool:
    """Resolved-IP safety for a sub-resource, memoised per host.

    Fail-closed like the navigation path: an unresolvable or erroring host
    is unsafe, never "probably fine". A missing host (a `data:`/`blob:`
    sub-resource, say) is left alone -- there is nothing to resolve and
    `evaluate_request` has already had its say on the URL.
    """
    if not host:
        return True

    cached = _cached_subresource_verdict(host)
    if cached is not None:
        return cached

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _validate, url, resolver)
    except UnsafeUrlError as exc:
        logger.warning(
            "abort_unsafe_request: sub-resource host %r is unsafe (%s); aborting",
            host,
            exc.reason,
        )
        _remember_subresource_verdict(host, False)
        return False
    except Exception:  # noqa: BLE001 - fail-closed, same rule as the navigation path
        logger.warning(
            "abort_unsafe_request: sub-resource resolution failed for host %r; "
            "aborting (fail-closed)",
            host,
            exc_info=True,
        )
        # Deliberately NOT memoised: a transient resolver failure must not
        # blackhole a host for a whole minute.
        return False

    _remember_subresource_verdict(host, True)
    return True


def _is_navigation_request(request: Any) -> bool:
    """Navigation/document request per `contracts/browser-safety.md` R6.

    Checks both signals the contract names (`request.is_navigation_request()`
    and `resource_type == "document"`) so this stays correct even if one
    signal is ever unavailable/inconsistent on a given Playwright request
    object -- every sub-resource request lacks both and passes untouched.
    """
    is_navigation_request = getattr(request, "is_navigation_request", None)
    if callable(is_navigation_request):
        try:
            if is_navigation_request():
                return True
        except Exception:  # noqa: BLE001 - defensive; fall through to the other signal
            pass
    return getattr(request, "resource_type", None) == "document"


def _page_hostname(request: Any) -> str | None:
    """The hostname of the PAGE this sub-resource belongs to (EPA B5).

    Playwright exposes it as ``request.frame.url`` -- the document the
    frame is currently on, which for every sub-resource of a target page
    is that target's own URL. This is the value B5's document-only rule
    must match on, never the sub-resource's own host: an ``amazon.sa``
    page's scripts and images are served from ``m.media-amazon.com`` and
    friends, so matching the sub-resource host would leave the rule
    firing on almost nothing it exists to stop.

    Totally defensive by design -- ``frame`` is unavailable for
    service-worker requests and raises on some Playwright builds, and
    every unit-test stand-in is free not to have one. Any failure
    returns ``None``, and the caller falls back to the sub-resource's own
    hostname (correct for a same-origin sub-resource, and merely
    "matches nothing" otherwise -- i.e. the pre-B5 decision).
    """
    try:
        frame = getattr(request, "frame", None)
        frame_url = getattr(frame, "url", None)
        if not frame_url:
            return None
        return urlsplit(frame_url).hostname
    except Exception:  # noqa: BLE001 - never let a page-domain lookup fail a request
        return None


def _validate(url: str, resolver: Resolver) -> None:
    """Run entirely inside `loop.run_in_executor` -- the resolver call
    (blocking DNS) plus the reused safety checks, off the event-loop thread."""
    validate_resolved_target(url, resolver=resolver)


async def abort_unsafe_request(request: "PlaywrightRequest", *, resolver: Resolver | None = None) -> bool:
    """``True`` -> scrapy-playwright aborts `request` before its body loads.

    Wired as ``PLAYWRIGHT_ABORT_REQUEST`` (T031); called by scrapy-playwright
    for **every** Playwright request on **every** navigation hop (including
    each redirect Chromium follows internally). Navigation/document
    requests (`_is_navigation_request`) get the DNS-resolving resolved-IP
    SSRF check below; every other (sub-resource) request instead goes
    through :func:`~app_shared.profiles.browser_resource_policy.evaluate_request`
    (EPA B6) -- a no-DNS URL-safety check, then the versioned cost/category
    block policy, in that order (never the reverse).

    `resolver` is an injectable seam (defaults to the real
    :func:`_system_resolver`) purely for unit testing -- production wiring
    (``PLAYWRIGHT_ABORT_REQUEST = scrape_core.browser.ssrf.abort_unsafe_request``)
    never passes one, so `abort_unsafe_request(request)` -- the exact
    single-argument shape scrapy-playwright calls -- always uses the real
    resolver.

    Re-runs the reused :func:`~scrape_core.safety.fetch.validate_resolved_target`
    (scheme/userinfo/IP-literal checks, then the resolved-IP `_reject_ip`
    deny rule) for `request.url`, entirely inside `loop.run_in_executor`
    (never on the event-loop thread). A rejection -- either the reused
    safety check's own `UnsafeUrlError`, or any other resolution failure
    (fail-closed: an unresolvable/erroring host is never treated as safe) --
    marks the hostname via `rejection_registry.mark_rejected` (see module
    docstring for why) and returns `True`. A safe resolved IP, or a
    non-navigation request, returns `False`.
    """
    if not _is_navigation_request(request):
        # EPA B6: cost/category guard, not a safety guard -- but
        # `evaluate_request` itself runs a (cheap, no-DNS) URL-safety
        # check FIRST, so a sub-resource request now also gets basic
        # SSRF coverage it never had before this change (previously only
        # navigations were checked at all). EPA B6b: `domain_profile` is
        # now the real resolved profile for this request's own hostname,
        # recovered from `domain_profile_registry` (see module docstring
        # above and that module's own docstring) -- a registry miss
        # yields `None`, `should_block`'s documented default-policy
        # value, so an unresolved domain still runs the plain default
        # blocklist rather than allowing anything extra through. The
        # rejection registry is never marked here (see module docstring)
        # -- an ordinary asset abort must not make `classify_exception`
        # misread the page as BLOCKED.
        sub_resource_type = getattr(request, "resource_type", None)
        sub_resource_host = urlsplit(request.url).hostname
        domain_profile = get_domain_profile(sub_resource_host)
        # EPA B5: the document-only rule needs two facts this hook is not
        # handed -- WHICH page this sub-resource belongs to (recovered
        # from `request.frame.url`, falling back to the sub-resource's own
        # host) and whether that page's leg is PROXIED (recovered from the
        # same dispatch-time side-channel as the profile, recorded by the
        # spider from the ledger's own `_is_proxied` predicate). Both
        # default to "unlisted domain, DIRECT", for which `should_block`
        # decides exactly as it did before B5 -- and with the shipped
        # empty `BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS` the rule cannot
        # fire on any domain at all.
        page_host = _page_hostname(request) or sub_resource_host
        transport = get_domain_transport(page_host)
        blocked = evaluate_resource_request(
            request.url,
            sub_resource_type,
            domain_profile,
            domain=page_host,
            transport=transport,
        )
        if blocked:
            log_event(
                logger,
                "browser.request_blocked",
                url=request.url,
                resource_type=sub_resource_type,
                policy_version=RESOURCE_POLICY_VERSION,
                transport=transport,
            )
            return True

        # READY F01: the policy said "fetch this asset". `evaluate_request`
        # only ran the no-DNS URL check, which passes any hostname that
        # merely LOOKS public -- so a sub-resource whose host RESOLVES to a
        # private address (`<script src="http://private.test/x.js">`) got
        # through. Memoised per host, dropped per context (module
        # docstring). Still never marks `rejection_registry`: an aborted
        # sub-resource must not make `classify_exception` read the whole
        # page as BLOCKED.
        sub_resolver = resolver if resolver is not None else _system_resolver
        if not await _subresource_host_is_safe(request.url, sub_resource_host, sub_resolver):
            return True
        return False

    url = request.url
    host = urlsplit(url).hostname
    active_resolver = resolver if resolver is not None else _system_resolver

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _validate, url, active_resolver)
    except UnsafeUrlError as exc:
        if host:
            mark_rejected(host)
        logger.warning(
            "abort_unsafe_request: aborting unsafe navigation %r (%s)", url, exc.reason
        )
        return True
    except Exception:  # noqa: BLE001 - fail-closed: any resolve error aborts, never silently allows
        if host:
            mark_rejected(host)
        logger.warning(
            "abort_unsafe_request: resolution failed for %r; aborting (fail-closed)",
            url,
            exc_info=True,
        )
        return True

    return False
