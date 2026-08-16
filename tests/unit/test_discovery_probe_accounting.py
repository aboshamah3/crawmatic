"""Discovery-probe accounting (Task 2.3, proxy-cost-reduction plan §2.3,
safety prerequisite for §3.3).

Discovery probes (`_probe_sample`, `apps/workers/app/workers/
tasks_strategy.py`) made ~87,000 real proxy requests but wrote 646
`request_attempts` rows -- per-URL accounting and the
`REQUESTS_PER_URL` circuit-breaker condition were blind to the largest
paid source. `_probe_sample` now writes one `RequestAttempt` row per
probe fetch it attempts, tagged `origin=RequestOrigin.DISCOVERY`, for
every URL it can resolve back to an existing `CompetitorProductMatch`
(`RequestAttempt.match_id` is NOT NULL -- an operator-supplied ad hoc
sample URL with no match yet gets no accounting row, matching
`tests/integration/test_discovery_run.py`'s ad hoc-URL scenarios).

Runs in a subprocess with a minimal env, mirroring
`test_discovery_early_exit.py`/`test_discovery_proxy_auth.py`: importing
`app.workers.tasks_strategy` constructs the Celery app (and therefore
`Settings`).
"""

from __future__ import annotations

import os
import subprocess
import sys

_CHECK = """
import sys
sys.path.insert(0, "apps/workers")

import uuid
from decimal import Decimal

from app_shared.enums import AccessMethod, RequestOrigin
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


class FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


# 1. A matched URL gets one discovery-origin RequestAttempt row per probed
#    (access_method, url) attempt.
workspace_id = uuid.uuid4()
competitor_id = uuid.uuid4()
match_id = uuid.uuid4()
url = "https://shop.example.com/p/1"

tasks_strategy._fetch = lambda session, workspace_id, access_method, url: PRICED_HTML
tasks_strategy._match_ids_for_urls = (
    lambda session, *, workspace_id, competitor_id, urls: {url: match_id}
)

session = FakeSession()
tasks_strategy._probe_sample(
    session,
    workspace_id=workspace_id,
    urls=[url],
    thresholds=THRESHOLDS,
    competitor_id=competitor_id,
)

# Full-sample DIRECT_HTTP qualifier early-exits the ladder (Fix 4b), so
# exactly one probe attempt (and therefore one attempt row) is recorded.
assert len(session.added) == 1, session.added
recorded = session.added[0]
assert recorded.origin == RequestOrigin.DISCOVERY, recorded.origin
assert recorded.workspace_id == workspace_id
assert recorded.match_id == match_id
assert recorded.url == url
assert recorded.access_method == AccessMethod.DIRECT_HTTP
assert recorded.success is True

# 2. An unmatched URL (no CompetitorProductMatch -- e.g. an operator ad hoc
#    sample) gets no attempt row at all: RequestAttempt.match_id is NOT
#    NULL and there is nothing to attribute it to.
tasks_strategy._match_ids_for_urls = (
    lambda session, *, workspace_id, competitor_id, urls: {}
)
session2 = FakeSession()
tasks_strategy._probe_sample(
    session2,
    workspace_id=workspace_id,
    urls=[url],
    thresholds=THRESHOLDS,
    competitor_id=competitor_id,
)
assert session2.added == [], session2.added

# 3. No competitor_id supplied (defensive default, e.g. an older caller) --
#    never even attempts a match lookup, never crashes.
session3 = FakeSession()
tasks_strategy._probe_sample(
    session3, workspace_id=workspace_id, urls=[url], thresholds=THRESHOLDS
)
assert session3.added == [], session3.added

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


def test_probe_sample_records_discovery_origin_attempt_rows() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHECK],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **_ENV},
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


def test_probe_sample_never_raises_when_recording_fails() -> None:
    """Accounting is best-effort telemetry (mirrors `stats_buffer
    .record_attempt`'s fail-open posture, contracts/stats-buffer.md step
    4): a session that blows up on `.add()` must never fail the probe
    itself -- the existing `test_discovery_early_exit.py` precedent
    passes a bare `object()` (no `.add()` at all) as `session` and must
    keep passing unmodified."""
    check = (
        """
import sys
sys.path.insert(0, "apps/workers")

import uuid
from decimal import Decimal

from app_shared.strategy.promotion import PromotionThresholds

from app.workers import tasks_strategy

THRESHOLDS = PromotionThresholds(
    confidence_threshold=Decimal("0.85"), min_successes=3, min_distinct_urls=3
)

tasks_strategy._fetch = lambda session, workspace_id, access_method, url: "<html></html>"
tasks_strategy._match_ids_for_urls = (
    lambda session, *, workspace_id, competitor_id, urls: {urls[0]: uuid.uuid4()}
)

# A bare object() has no .add()/.execute() -- recording must swallow the
# AttributeError and the probe must still complete normally.
tasks_strategy._probe_sample(
    object(),
    workspace_id=uuid.uuid4(),
    urls=["https://shop.example.com/p/1"],
    thresholds=THRESHOLDS,
    competitor_id=uuid.uuid4(),
)

print("OK")
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", check],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **_ENV},
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"
