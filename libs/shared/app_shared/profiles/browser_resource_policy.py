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

**Document-only browser legs (EPA B5 2026-09-03 / EPA C3 2026-09-08,
canary-gated)**: for a domain an operator lists in
``BROWSER_DOCUMENT_ONLY_DOMAINS``, every sub-resource type in
:data:`PROXIED_BLOCKED_RESOURCE_TYPES` is aborted on **both** the DIRECT
and the PROXY leg — the page's own document is fetched and nothing else,
regardless of which transport carries it. (B5 shipped this for the PROXY
leg only, reasoning that a DIRECT leg's bytes are free egress and not
worth the rule's complexity; C3 extends it to DIRECT too because a
document-only DIRECT fetch is also faster and lighter on the target,
independent of who pays for the bytes — see
``scripts/canary_document_only_browser.py``'s calculator, which now
measures both legs.) ``BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS`` — the
2026-09-03 setting name — is kept as an **alias**: a domain listed there
gets the identical both-legs treatment, so nothing an operator already
configured needs to change. Both settings default to ``()``: with
nothing listed on either name, every domain on every transport decides
exactly as it did before B5, which is the whole point — the rule ships
dark and the owner turns it on for one domain (``amazon.sa``, gated on
C11) after the canary (``scripts/canary_document_only_browser.py``)
measures success rate, wall time and bytes per page on both sides.

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

import os
from typing import Any, Iterable
from urllib.parse import urlsplit

from app_shared.config import get_settings
from app_shared.url_safety import UnsafeUrlError, validate_competitor_url

__all__ = [
    "BLOCKLIST_VERSION",
    "BLOCKED_RESOURCE_TYPES",
    "PROXIED_BLOCKED_RESOURCE_TYPES",
    "should_block",
    "evaluate_request",
    "split_attempt_bytes",
]

#: Bump on any change to the default blocklist below (resource types OR
#: host categories) — old evidence/log lines about "why was this
#: blocked" should never be silently reinterpreted by a later rule-set
#: change, mirroring ``scripts/classify_match_set.py``'s
#: ``CLASSIFIER_VERSION`` convention.
#: * v1 — the default type/host blocklist below.
#: * v2 (EPA B5, 2026-09-03) — adds the canary-gated document-only rule
#:   for PROXIED legs of operator-listed domains
#:   (:data:`PROXIED_BLOCKED_RESOURCE_TYPES`). The bump is what keeps a
#:   canary run's ``policy_version`` stamps distinguishable from every
#:   line logged before it, even though the v2 default (nothing listed)
#:   decides identically to v1 on every domain.
#: * v3 (EPA C3, 2026-09-08) — the document-only rule now applies to
#:   **both** the DIRECT and PROXY legs of an operator-listed domain, not
#:   only PROXY (:data:`BROWSER_DOCUMENT_ONLY_DOMAINS`). The 2026-09-03
#:   setting name (``BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS``) is kept as
#:   an alias into the same (now both-legs) list rather than removed, so
#:   an operator who already listed a domain under the old name gets the
#:   extended behaviour automatically. The default is still ``()`` on
#:   both names: nothing changes for anyone until a domain is listed.
BLOCKLIST_VERSION = 3

#: Resource types blocked on every domain by default (subject to
#: per-domain ``certified_resources`` override). ``document`` is
#: deliberately absent — see :func:`should_block`'s unconditional guard.
BLOCKED_RESOURCE_TYPES: frozenset[str] = frozenset({"image", "media", "font", "stylesheet"})

#: EPA B5: everything a *document-only* proxied leg aborts. Applied ONLY
#: to a domain an operator listed in
#: ``BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS`` AND only when that leg's
#: `transport` is ``"PROXY"``. ``document`` is deliberately absent (and
#: unreachable anyway — :func:`should_block`'s first guard returns before
#: this set is consulted): the page's own navigation is the one thing the
#: proxy is being paid for.
PROXIED_BLOCKED_RESOURCE_TYPES: frozenset[str] = frozenset(
    {
        "image",
        "media",
        "font",
        "stylesheet",
        "script",
        "xhr",
        "fetch",
        "other",
        "ping",
        "websocket",
    }
)

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


def _parse_domain_list(raw: Iterable[Any] | str | None) -> tuple[str, ...]:
    """Normalize a domain list from either a settings tuple or a raw
    comma-separated env string into lowercase, stripped entries."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts: Iterable[Any] = raw.split(",")
    else:
        parts = raw
    return tuple(str(domain).strip().lower() for domain in parts if str(domain).strip())


def _document_only_domains() -> tuple[str, ...]:
    """Operator-listed domains whose browser legs are document-only on
    BOTH transports (EPA B5 2026-09-03 / EPA C3 2026-09-08).

    Union of two sources, de-duplicated:

    1. ``Settings.BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS`` through the
       module-level :func:`get_settings` (the seam tests monkeypatch, per
       this repo's ``monkeypatch.setattr(<module>, "get_settings", ...)``
       convention) — the 2026-09-03 setting name, kept as an alias so a
       domain already listed there keeps working unchanged.
    2. The ``BROWSER_DOCUMENT_ONLY_DOMAINS`` environment variable, read
       directly (EPA C3 packet note: this module's own EPA task may not
       add a new field to ``Settings`` while a sibling task is also
       editing ``config.py``; a raw env read needs no schema change and
       is exactly as fail-safe as the settings path below).

    ANY failure on either source — a process with no env at all, an
    unparseable value, a stand-in settings object without the field —
    degrades that source to ``()``, i.e. to the pre-B5 behaviour, because
    :func:`should_block` is documented as pure and total and is called
    from inside a Playwright route handler where raising would fail an
    ordinary sub-resource in a way nothing downstream could explain.
    """
    try:
        settings_listed = _parse_domain_list(
            getattr(get_settings(), "BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS", ())
        )
    except Exception:  # noqa: BLE001 - see docstring: never raise into a route handler
        settings_listed = ()
    try:
        env_listed = _parse_domain_list(os.environ.get("BROWSER_DOCUMENT_ONLY_DOMAINS"))
    except Exception:  # noqa: BLE001 - same rule, same reason
        env_listed = ()
    merged: list[str] = []
    for domain in (*settings_listed, *env_listed):
        if domain not in merged:
            merged.append(domain)
    return tuple(merged)


def _is_listed_domain(domain: str | None, listed: tuple[str, ...]) -> bool:
    """Exact-host or dot-suffix match against `listed` (never a substring).

    ``amazon.sa`` therefore covers ``www.amazon.sa`` and any deeper
    subdomain — one registrable domain is one site, and the canary lists
    a site — while ``notamazon.sa`` is a different site and never
    matches, exactly like :func:`_is_blocked_host`'s host-category rule.
    """
    if not domain or not listed:
        return False
    host = domain.strip().lower().rstrip(".")
    return any(host == entry or host.endswith("." + entry) for entry in listed)


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


def should_block(
    url: str,
    resource_type: str,
    domain_profile: Any = None,
    *,
    domain: str | None = None,
    transport: str = "DIRECT",
) -> bool:
    """``True`` iff `resource_type` at `url` should be aborted, per the default policy.

    Pure and total: never raises, never performs I/O. Order of checks
    (all are independent overrides of "block", evaluated in the
    safest-first order):

    1. ``resource_type == "document"`` -> always ``False``. The page's
       own navigation is never blocked by this policy, on any transport,
       listed or not.
    2. **EPA B5/C3, canary-gated**: `domain` is listed in
       ``BROWSER_DOCUMENT_ONLY_DOMAINS`` (or its alias,
       ``BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS``) AND `resource_type` is
       in :data:`PROXIED_BLOCKED_RESOURCE_TYPES` -> ``True``, on **any**
       `transport` (EPA C3, 2026-09-08 — B5 originally gated this on
       ``transport == "PROXY"`` only; that condition is gone as of
       :data:`BLOCKLIST_VERSION` 3). Deliberately ABOVE certification:
       "document only" means document only, so a per-domain
       ``certified_resources`` entry cannot re-admit a sub-resource on a
       listed domain's leg while the canary is measuring what
       document-only actually costs.
    3. `resource_type` in `domain_profile`'s (tolerant) certified set ->
       always ``False``. Certification is a full override for that
       resource type on that domain — see the module docstring.
    4. `resource_type` in :data:`BLOCKED_RESOURCE_TYPES` -> ``True``.
    5. The URL's host matches an ad/analytics/social category
       (:data:`_AD_ANALYTICS_SOCIAL_HOST_SUFFIXES`) -> ``True``,
       regardless of `resource_type` (an ad network's own script/xhr
       calls are blocked exactly like its images).
    6. Otherwise ``False``.

    `domain` is the **page's** domain, not the sub-resource's own host: an
    Amazon page's images and scripts live on ``m.media-amazon.com``/
    ``images-*.ssl-images-amazon.com``, which would never match a listed
    ``amazon.sa``, so matching on the sub-resource host would make the
    rule bite on almost nothing it exists to stop. `transport` is the
    same PROXY/DIRECT notion the ledger records
    (scrape-core's ``netledger_middleware.is_proxied`` — named with the
    hyphen deliberately: no file under ``app_shared`` may contain that
    package's importable name at all, even in prose,
    ``tests/unit/test_import_boundaries.py``): kept as a parameter (and
    still recorded in the report header per C3's test) even though rule 2
    no longer branches on it, because a caller/report still needs to know
    which leg a given decision was made for.

    `domain` is keyword-only and defaults to "no domain", so every pre-B5
    positional call site keeps its exact previous decision, and with both
    settings shipped empty rule 2 can never fire at all.

    Callers that also need the SSRF/redirect safety check to run FIRST
    (i.e. every real request-interception call site) must use
    :func:`evaluate_request` instead of calling this directly.
    """
    if resource_type == "document":
        return False
    if resource_type in PROXIED_BLOCKED_RESOURCE_TYPES and _is_listed_domain(
        domain, _document_only_domains()
    ):
        return True
    if resource_type in _certified_resource_types(domain_profile):
        return False
    if resource_type in BLOCKED_RESOURCE_TYPES:
        return True
    return _is_blocked_host(_hostname(url))


def evaluate_request(
    url: str,
    resource_type: str,
    domain_profile: Any = None,
    *,
    domain: str | None = None,
    transport: str = "DIRECT",
) -> bool:
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

    `domain`/`transport` (EPA B5/C3) are forwarded verbatim to
    :func:`should_block` — see its docstring for why the PAGE's domain,
    not the sub-resource's own host, is the thing matched, and why
    `transport` no longer gates rule 2 as of :data:`BLOCKLIST_VERSION` 3.
    """
    try:
        validate_competitor_url(url)
    except UnsafeUrlError:
        return True
    return should_block(url, resource_type, domain_profile, domain=domain, transport=transport)


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
