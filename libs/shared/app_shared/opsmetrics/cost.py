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


def usd(proxied_requests: float) -> float:
    """Convert a proxied-attempt count to estimated USD."""
    return round(proxied_requests * USD_PER_PROXIED_REQUEST, 4)


__all__ = [
    "AMAZON_HEALTHY_REQUESTS_PER_URL",
    "AMAZON_SHARE_OF_VARIABLE",
    "LINKS_PER_FULL_REFRESH",
    "USD_FIXED_MONTHLY",
    "USD_PER_FULL_REFRESH",
    "USD_PER_PROXIED_REQUEST",
    "usd",
]
