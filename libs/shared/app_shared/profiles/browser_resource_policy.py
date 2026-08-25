"""Browser resource-blocking policy (EPA B6).

The 2026-08-24 canary's own proxy-byte breakdown found that most bytes
crossing the paid residential proxy on a browser fetch were Amazon
*subresources* — media/ad requests riding alongside the one page the
scraper actually wanted, never the priced page's own document. This
module is the versioned, testable policy behind that finding: **block by
default; certify exceptions per domain.**

:func:`should_block` is the pure decision function. It never performs
I/O, never resolves DNS, never touches Playwright — it takes a URL, a
Playwright/scrapy-playwright-shaped ``resource_type`` string
(``"document"``/``"image"``/``"media"``/``"font"``/``"script"``/
``"xhr"``/``"stylesheet"``/...), and an optional per-domain profile
object, and returns ``True`` iff the request should be aborted before
its body loads.

**Ordering (non-negotiable)**: resource blocking is a COST decision,
never a SAFETY one, and must never run in place of the SSRF/redirect
safety checks the browser guard already performs
(scrape-core's ``browser.ssrf.abort_unsafe_request``, built on this
repo's ``app_shared.url_safety`` — read that module first: it is the
save-time SSRF validator W5.5-D hardened against loose-IPv4/IDNA/
trailing-dot/host-charset bypasses). :func:`evaluate_request` is the
small orchestration wrapper that encodes this ordering explicitly: the
URL/redirect safety check runs FIRST, unconditionally, and only a URL
that passes it is even handed to :func:`should_block`. An unsafe URL is
aborted for being unsafe — the resource-blocking policy is never even
consulted, so a "certified" resource type can never launder an SSRF
target through this module.

**Default blocklist (versioned, :data:`BLOCKLIST_VERSION`)**:

* Resource TYPES — ``image``, ``media``, ``font`` (the plan's Step 1
  contract) plus ``stylesheet`` (carried forward from the pre-B6
  per-navigation-hop guard's ``_COST_BLOCKED_RESOURCE_TYPES`` in
  scrape-core's ``browser.ssrf`` — CSS *selectors* work without CSS
  *files* ever loading; removing this would have been a silent cost
  regression this module was not asked to introduce).
* Host CATEGORIES — ads/analytics/social (:data:`_AD_ANALYTICS_SOCIAL_HOST_SUFFIXES`),
  matched regardless of resource type: an ad network's own ``script`` or
  ``xhr`` calls are exactly the traffic this policy exists to stop, not
  just its images.
* ``document`` (the page's own top-level navigation) is NEVER blocked by
  this policy, unconditionally — the one thing the scraper actually
  fetches the page for.

**Certification (deferred wiring, not deferred code)**: ``only the
profile's certified_resources are allowed through`` — a resource type
present in ``domain_profile.certified_resources`` is let through
regardless of whether the type-blocklist or the host-category blocklist
would otherwise have blocked it. :func:`_certified_resource_types`
reads that attribute via a **tolerant** ``getattr`` defaulting to an
empty collection, so this module needs no per-domain profile schema
change to land: no ``DomainProfile``/``ScrapeProfile`` file is touched
by this change, and every domain runs the plain default blocklist until
a future step adds the column/field and re-runs that domain's B5
certification fixtures (the orchestrator's job, not this module's).
``domain_profile`` is therefore deliberately duck-typed (``Any``, not a
concrete class) — in practice the closest existing analog is
``app_shared.models.scrape_profiles.ScrapeProfile``, but any object (or
``None``) works.

**Byte accounting** (:func:`split_attempt_bytes`) is a separate, purely
additive concern: given the observed ``(resource_type, byte_count)``
pairs for one attempt, split them into the two TRANSPORT-OBSERVED
figures ``request_attempts.main_document_bytes``/``subresource_bytes``
(see that migration's docstring,
``alembic/versions/d5e8a3c164f2_request_attempt_byte_accounting.py``,
for why "transport-observed" and "provider-billed" are recorded as
DISTINCT facts, reconciled only in C5 — never conflated here).
"""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlsplit

from app_shared.url_safety import UnsafeUrlError, validate_competitor_url

__all__ = [
    "BLOCKLIST_VERSION",
    "BLOCKED_RESOURCE_TYPES",
    "should_block",
    "evaluate_request",
    "split_attempt_bytes",
]

#: Bump on any change to the default blocklist below (resource types OR
#: host categories) — old evidence/log lines about "why was this
#: blocked" should never be silently reinterpreted by a later rule-set
#: change, mirroring ``scripts/classify_match_set.py``'s
#: ``CLASSIFIER_VERSION`` convention.
BLOCKLIST_VERSION = 1

#: Resource types blocked on every domain by default (subject to
#: per-domain ``certified_resources`` override). ``document`` is
#: deliberately absent — see :func:`should_block`'s unconditional guard.
BLOCKED_RESOURCE_TYPES: frozenset[str] = frozenset({"image", "media", "font", "stylesheet"})

#: Ad/analytics/social host suffixes blocked regardless of resource type
#: (versioned with ``BLOCKLIST_VERSION`` above). Exact-host or
#: dot-suffix match against the lowercased hostname (``_is_blocked_host``)
#: — never a substring match, so e.g. ``notgoogletagmanager.com`` is not
#: caught by ``googletagmanager.com``.
_AD_ANALYTICS_SOCIAL_HOST_SUFFIXES: tuple[str, ...] = (
    # Amazon's own ad stack — the canary's largest single offender.
    "amazon-adsystem.com",
    "assoc-amazon.com",
    # Programmatic ad / display networks.
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "adnxs.com",
    "adsrvr.org",
    "criteo.com",
    "criteo.net",
    "outbrain.com",
    "taboola.com",
    "pubmatic.com",
    "rubiconproject.com",
    # Web/product analytics.
    "google-analytics.com",
    "googletagmanager.com",
    "scorecardresearch.com",
    "hotjar.com",
    "segment.io",
    "segment.com",
    "mixpanel.com",
    "amplitude.com",
    "clarity.ms",
    "bat.bing.com",
    # Social widgets/pixels.
    "facebook.net",
    "connect.facebook.net",
    "fbcdn.net",
    "analytics.twitter.com",
    "ads-twitter.com",
    "ct.pinterest.com",
    "analytics.tiktok.com",
)


def _hostname(url: str) -> str:
    """Lowercased hostname, or ``""`` for anything unparseable.

    Never raises — an unparseable URL simply matches no host category
    below (the type-blocklist and ``evaluate_request``'s safety check
    are what actually reject a malformed URL, not this helper).
    """
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_blocked_host(host: str) -> bool:
    if not host:
        return False
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _AD_ANALYTICS_SOCIAL_HOST_SUFFIXES
    )


def _certified_resource_types(domain_profile: Any) -> frozenset[str]:
    """Tolerant read of ``domain_profile.certified_resources``.

    Missing attribute, ``None``, or any other falsy value -> empty set.
    This is the seam that lets `should_block` ship today with a
    DEFAULT-ONLY blocklist: no domain-profile file needs a
    ``certified_resources`` field yet for this module to be correct —
    every profile that doesn't have one degrades to "nothing certified".
    """
    values = getattr(domain_profile, "certified_resources", None) or ()
    return frozenset(values)


def should_block(url: str, resource_type: str, domain_profile: Any = None) -> bool:
    """``True`` iff `resource_type` at `url` should be aborted, per the default policy.

    Pure and total: never raises, never performs I/O. Order of checks
    (all three are independent overrides of "block", evaluated in the
    safest-first order):

    1. ``resource_type == "document"`` -> always ``False``. The page's
       own navigation is never blocked by this policy.
    2. `resource_type` in `domain_profile`'s (tolerant) certified set ->
       always ``False``. Certification is a full override for that
       resource type on that domain — see the module docstring.
    3. `resource_type` in :data:`BLOCKED_RESOURCE_TYPES` -> ``True``.
    4. The URL's host matches an ad/analytics/social category
       (:data:`_AD_ANALYTICS_SOCIAL_HOST_SUFFIXES`) -> ``True``,
       regardless of `resource_type` (an ad network's own script/xhr
       calls are blocked exactly like its images).
    5. Otherwise ``False``.

    Callers that also need the SSRF/redirect safety check to run FIRST
    (i.e. every real request-interception call site) must use
    :func:`evaluate_request` instead of calling this directly.
    """
    if resource_type == "document":
        return False
    if resource_type in _certified_resource_types(domain_profile):
        return False
    if resource_type in BLOCKED_RESOURCE_TYPES:
        return True
    return _is_blocked_host(_hostname(url))


def evaluate_request(url: str, resource_type: str, domain_profile: Any = None) -> bool:
    """``True`` iff `url`/`resource_type` should be aborted -- safety FIRST, then policy.

    The one entry point every real request-interception call site should
    use (never :func:`should_block` directly), so the ordering
    requirement is structural rather than a convention callers must
    remember: :func:`app_shared.url_safety.validate_competitor_url` (the
    save-time SSRF validator; no DNS resolution, per its own contract)
    runs unconditionally first. A URL it rejects is aborted for being
    unsafe -- `should_block`/`certified_resources` are never even
    consulted, so certifying a resource type can never let an SSRF
    target through. Only a URL that passes reaches the cost policy.
    """
    try:
        validate_competitor_url(url)
    except UnsafeUrlError:
        return True
    return should_block(url, resource_type, domain_profile)


def split_attempt_bytes(observed: Iterable[tuple[str, int]]) -> tuple[int, int]:
    """Split one attempt's observed ``(resource_type, byte_count)`` pairs.

    Returns ``(main_document_bytes, subresource_bytes)`` — the exact
    pair ``request_attempts`` stores (both TRANSPORT-OBSERVED, see
    ``alembic/versions/d5e8a3c164f2_request_attempt_byte_accounting.py``).
    A ``"document"`` entry contributes to the first; every other
    resource type (including one this policy chose to let through)
    contributes to the second. Never negative; an empty `observed`
    yields ``(0, 0)``, matching "measured, zero bytes seen" — callers
    that instead have "not measured at all" for an attempt should pass
    ``None``/``None`` straight to the two DB columns, not call this
    function.
    """
    main_document_bytes = 0
    subresource_bytes = 0
    for resource_type, byte_count in observed:
        if resource_type == "document":
            main_document_bytes += byte_count
        else:
            subresource_bytes += byte_count
    return main_document_bytes, subresource_bytes
