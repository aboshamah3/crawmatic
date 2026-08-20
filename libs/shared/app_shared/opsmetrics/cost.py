"""Measured cost constants (audit 2026-08-15 §7 "Financial assessment").

Every number here is **measured**, not invented, and carries its
provenance. They exist so spend metrics can be reported in dollars
without any process needing a provider API key: the only unit this
codebase can count itself is *proxied request attempts*
(``request_attempts.access_method IN (PROXY_HTTP, PLAYWRIGHT_PROXY)``),
so the conversion factor has to live somewhere explicit and citable.

Provenance
----------

``USD_PER_PROXIED_REQUEST``
    2026-08-10 DataImpulse usage-API window (memory note
    ``dataimpulse-usage-api``): ~$1.96 of scraping spend attributable to
    the scrapers across ~15,500 proxied request attempts recorded in
    ``request_attempts`` for the same window => $0.0001265 per proxied
    attempt. Bytes, not requests, are what the provider actually bills,
    so this is an average over a representative domain mix (amazon /
    noon / stech are the only proxied domains) and is accurate to about
    a factor of 1.5 for a heavily skewed mix. That is precise enough for
    a *stop-loss*, which is what it is used for; it is not an invoice.

``USD_FIXED_MONTHLY``
    2026-08-12 cost report: $9.08/month Railway platform floor
    (post-loop-fix), independent of scraping volume.

``USD_PER_FULL_REFRESH`` / ``LINKS_PER_FULL_REFRESH``
    2026-08-12 cost report: ~$2.00 all-in for a complete 4,587-link
    refresh, of which ~$1.98 is variable proxy cost.

``AMAZON_SHARE_OF_VARIABLE``
    2026-08-12 cost report: amazon.sa was ~$1.346 of the ~$1.98
    variable cost per full refresh (~67%).

``USD_PER_PROXIED_REQUEST_BY_DOMAIN`` / ``DIRECT_USD_PER_REQUEST``
    Task 2.4 (proxy-cost-reduction plan §2.4). The 2026-08-12 report's §2
    billing table gives ``$/link`` and ``req/link`` per competitor, not
    ``$/req`` directly, so the per-request rate is derived here as
    ``$/link ÷ req/link``:

    ============ =========== ========== ==============
    domain       $/link       req/link   $/req (derived)
    ============ =========== ========== ==============
    amazon.sa    $0.001066    5.06       $0.00021067
    stech.ink    $0.000387    5.30       $0.00007302
    noon.com     $0.000382    4.71       $0.00008110
    ============ =========== ========== ==============

    (report lines 58-60). Every other competitor in that table is never
    ``PROXY_HTTP``/``PLAYWRIGHT_PROXY`` -- fetched direct, so it carries
    "no proxy cost at all" (report line 47-49) -- and its uniform
    ``$0.0000046/link`` (report lines 61-69, pure Railway compute, no
    ``req/link`` given because it is not multiplied by retries the way
    the report measures) is ``DIRECT_USD_PER_REQUEST``, the fallback for
    any domain not in the table above.

Deliberately a module of plain constants rather than ``Settings`` knobs:
these are *observations about the world*, and an operator who changes
them is falsifying the dashboard rather than tuning behaviour. The
thresholds that act on them ARE tunable -- see
``app_shared.opsmetrics.rules``.
"""

from __future__ import annotations

#: USD per proxied request attempt (see module docstring for provenance).
USD_PER_PROXIED_REQUEST: float = 0.0001265

#: USD/month Railway platform floor, independent of scrape volume.
USD_FIXED_MONTHLY: float = 9.08

#: All-in USD for one complete refresh of the measured catalog.
USD_PER_FULL_REFRESH: float = 2.00

#: Catalog size the above refresh figure was measured against.
LINKS_PER_FULL_REFRESH: int = 4587

#: amazon.sa's share of variable (proxy) cost per full refresh.
AMAZON_SHARE_OF_VARIABLE: float = 0.67

#: Healthy requests-per-URL for amazon on a good run (2026-08-11 full
#: scrape). The 30-day production figure is 8.44 -- see the SLO doc.
AMAZON_HEALTHY_REQUESTS_PER_URL: float = 2.48

#: Per-domain USD per PAID request attempt, derived from the 2026-08-12
#: report's §2 billing table ($/link ÷ req/link -- see module docstring
#: for the full derivation and source lines). Keyed on the same bare
#: hostname `Competitor.domain` stores and `opsmetrics.snapshot._DOMAIN_SQL`
#: derives from `request_attempts.url`.
USD_PER_PROXIED_REQUEST_BY_DOMAIN: dict[str, float] = {
    "amazon.sa": 0.00021067,
    "stech.ink": 0.00007302,
    "noon.com": 0.00008110,
}

#: $/req for every domain NOT in `USD_PER_PROXIED_REQUEST_BY_DOMAIN` --
#: the report's uniform direct-site rate (report lines 61-69). Used as
#: the fallback in `usd_per_request_for_domain`.
DIRECT_USD_PER_REQUEST: float = 0.0000046


def usd(proxied_requests: float) -> float:
    """Convert a proxied-attempt count to estimated USD using the FLEET
    average (`USD_PER_PROXIED_REQUEST`). Prefer `usd_for_domain` when the
    domain is known -- the fleet average is off by ~2.8x for amazon.sa
    relative to the direct-site floor (see module docstring)."""
    return round(proxied_requests * USD_PER_PROXIED_REQUEST, 4)


def usd_per_request_for_domain(domain: str) -> float:
    """This domain's own $/req from the 2026-08-12 report.

    Falls back to `DIRECT_USD_PER_REQUEST` for any domain not in
    `USD_PER_PROXIED_REQUEST_BY_DOMAIN` -- every other measured
    competitor in that report never touches the paid proxy at all, so
    the direct-site compute floor is the right default rather than the
    proxied fleet average.
    """
    return USD_PER_PROXIED_REQUEST_BY_DOMAIN.get(domain, DIRECT_USD_PER_REQUEST)


def usd_for_domain(domain: str, proxied_requests: float) -> float:
    """USD estimate using THIS domain's own $/req rate, not the fleet
    average `usd` uses. Task 2.4: the cost-per-successful-price metric
    needs the domain-specific rate to be meaningful."""
    return round(proxied_requests * usd_per_request_for_domain(domain), 6)


__all__ = [
    "AMAZON_HEALTHY_REQUESTS_PER_URL",
    "AMAZON_SHARE_OF_VARIABLE",
    "DIRECT_USD_PER_REQUEST",
    "LINKS_PER_FULL_REFRESH",
    "USD_FIXED_MONTHLY",
    "USD_PER_FULL_REFRESH",
    "USD_PER_PROXIED_REQUEST",
    "USD_PER_PROXIED_REQUEST_BY_DOMAIN",
    "usd",
    "usd_for_domain",
    "usd_per_request_for_domain",
]
