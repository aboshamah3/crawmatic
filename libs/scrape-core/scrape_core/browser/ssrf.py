"""``PLAYWRIGHT_ABORT_REQUEST`` guard — per-navigation-hop resolved-IP SSRF
re-validation (SPEC-14 T030, US4, `contracts/browser-safety.md` R6).

``scrapy-playwright`` does **not** consult Scrapy's ``DNS_RESOLVER`` at
all — Chromium performs its own OS-level DNS resolution — so the
process-wide connect-time defense (``scrape_core.safety.resolver.SafeResolver``,
installed via ``DNS_RESOLVER``) never runs for a browser navigation.
Chromium also follows a redirect's ``Location`` **internally**, bypassing
Scrapy's ``RedirectMiddleware``/``SsrfGuardMiddleware`` entirely for that
hop. :func:`abort_unsafe_request`, wired as ``PLAYWRIGHT_ABORT_REQUEST``,
is therefore the *only* guard that ever sees each Chromium-internal
navigation — including every redirect hop — and is what actually closes
the SSRF gap for the browser path (Constitution §VI, NON-NEGOTIABLE).

Reuses, never re-implements, the existing SSRF logic: the scheme/
userinfo/IP-literal checks and the resolved-IP `_reject_ip` deny rule
both live in :func:`scrape_core.safety.fetch.validate_resolved_target`
(itself built on ``app_shared.url_safety.validate_competitor_url``/
``_reject_ip``). This module only supplies the scrapy-playwright-shaped
seam (navigation-vs-subresource gating, the off-event-loop-thread
resolve, and the ``rejection_registry`` side-channel a real abort needs
so the browser errback can still recognize *why* the fetch failed —
see below).

**Scope**: applies only to navigation/document requests
(``request.is_navigation_request()`` or ``resource_type == "document"``);
every sub-resource (image/script/stylesheet/xhr/...) passes untouched —
SSRF-relevant fetches are the page navigations themselves, not their
assets.

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
import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from app_shared.url_safety import UnsafeUrlError

from scrape_core.safety.fetch import Resolver, validate_resolved_target
from scrape_core.safety.rejection_registry import mark_rejected

if TYPE_CHECKING:  # pragma: no cover - type-checking only, never imported at runtime here
    # Deferred/type-only import (mirrors `scrape_core.browser.variant`'s "import
    # policy note"): this module must stay importable wherever `scrape_core` is
    # (e.g. `apps/scrapers`, which has no playwright dependency) even though
    # only the browser-only project ever actually wires this in as
    # `PLAYWRIGHT_ABORT_REQUEST`.
    from playwright.async_api import Request as PlaywrightRequest

logger = logging.getLogger(__name__)

__all__ = ["abort_unsafe_request"]


def _system_resolver(host: str) -> list[str]:
    """Real (blocking) system DNS resolver — the production default.

    Always invoked through ``loop.run_in_executor`` by
    :func:`abort_unsafe_request`, never called directly on the event-loop
    thread.
    """
    infos = socket.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


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


def _validate(url: str, resolver: Resolver) -> None:
    """Run entirely inside `loop.run_in_executor` -- the resolver call
    (blocking DNS) plus the reused safety checks, off the event-loop thread."""
    validate_resolved_target(url, resolver=resolver)


async def abort_unsafe_request(request: "PlaywrightRequest", *, resolver: Resolver | None = None) -> bool:
    """``True`` -> scrapy-playwright aborts `request` before its body loads.

    Wired as ``PLAYWRIGHT_ABORT_REQUEST`` (T031); called by scrapy-playwright
    for **every** Playwright request on **every** navigation hop (including
    each redirect Chromium follows internally). Only navigation/document
    requests are checked (`_is_navigation_request`) -- every other resource
    type returns `False` immediately, no resolve attempted.

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
