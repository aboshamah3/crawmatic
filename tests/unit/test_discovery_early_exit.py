"""Discovery-ladder early-exit tests (2026-08-11 proxy-cost Fix 4b,
PLAN_PROXY_COST_REDUCTION.md).

`_probe_sample` walks the access ladder cheapest-first; once a method
qualifies on the whole sample nothing later can beat it (winner = most
qualifying URLs, ties to the cheapest access), so the remaining legs --
including the paid `PROXY_HTTP` one -- must be skipped outright. A
partial qualifier must still walk every leg.

Importing ``app.workers.tasks_strategy`` constructs the Celery app (and
therefore ``Settings``), so these run the module in a subprocess with a
minimal env -- the ``test_discovery_proxy_auth.py`` precedent.
"""

from __future__ import annotations

import os
import subprocess
import sys

_CHECK = """
import sys
# `apps/api` and `apps/workers` each ship a top-level `app` package -- pin
# the workers one first (the test_discovery_proxy_auth.py convention).
sys.path.insert(0, "apps/workers")

import uuid
from decimal import Decimal

from app_shared.enums import AccessMethod
from app_shared.strategy.promotion import PromotionThresholds

from app.workers import tasks_strategy

PRICED_HTML = (
    '<html><body><script type="application/ld+json">'
    '{"@type": "Product", "name": "Widget", "offers": '
    '{"@type": "Offer", "price": "149.00", "priceCurrency": "SAR"}}'
    "</script></body></html>"
)
THRESHOLDS = PromotionThresholds(
    confidence_threshold=Decimal("0.85"), min_successes=3, min_distinct_urls=3
)

# 1. full-sample DIRECT_HTTP qualifier => later legs never fetch
fetch_log = []
def fake_fetch(session, workspace_id, access_method, url):
    fetch_log.append(access_method)
    return PRICED_HTML
tasks_strategy._fetch = fake_fetch

urls = [f"https://shop.example.com/p/{i}" for i in range(4)]
tallies = tasks_strategy._probe_sample(
    object(), workspace_id=uuid.uuid4(), urls=urls, thresholds=THRESHOLDS
)
assert fetch_log == [AccessMethod.DIRECT_HTTP] * len(urls), fetch_log
winner = tasks_strategy.select_discovery_winner(tallies)
assert winner is not None and winner[0] is AccessMethod.DIRECT_HTTP

# 2. partial qualifier => every leg still probed
fetch_log.clear()
def flaky_fetch(session, workspace_id, access_method, url):
    fetch_log.append(access_method)
    return None if url.endswith("/p/0") else PRICED_HTML
tasks_strategy._fetch = flaky_fetch

urls = [f"https://shop.example.com/p/{i}" for i in range(3)]
tasks_strategy._probe_sample(
    object(), workspace_id=uuid.uuid4(), urls=urls, thresholds=THRESHOLDS
)
assert set(fetch_log) == {
    AccessMethod.DIRECT_HTTP,
    AccessMethod.DIRECT_HTTP_RETRY,
    AccessMethod.PROXY_HTTP,
}, fetch_log

print("OK")
"""

_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def test_probe_sample_early_exits_on_a_full_sample_direct_qualifier() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHECK],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **_ENV},
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"
