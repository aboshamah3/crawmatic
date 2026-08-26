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

tasks_strategy._fetch = (
    lambda session, workspace_id, access_method, url, **kwargs: PRICED_HTML
)
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

tasks_strategy._fetch = (
    lambda session, workspace_id, access_method, url, **kwargs: "<html></html>"
)
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


# --- EPA C4b: `_fetch` opens/closes C1's ledger row around the rung it ---
# actually dispatches. A `LedgerOpenError` must fail that rung CLOSED --
# skip it, never open the socket -- exactly like a refused SSRF target or
# an OPEN circuit breaker; the run itself must never crash over one
# unrecordable rung.

_LEDGER_WIRING_CHECK = """
import sys
sys.path.insert(0, "apps/workers")

import uuid

from app_shared.enums import AccessMethod
from app_shared.models.network_operations import NetworkTransport
from app_shared.netledger.recorder import LedgerOpenError

from app.workers import tasks_strategy

url = "https://shop.example.com/p/1"
workspace_id = uuid.uuid4()
authorization_id = uuid.uuid4()


class _OpenFailsRecorder:
    def __init__(self, *, costauth=None):
        pass

    def open(self, intent):
        raise LedgerOpenError("boom")

    def close(self, network_request_id, outcome):  # pragma: no cover - unreached
        raise AssertionError("close must never run when open failed")


direct_calls = []


def _fake_fetch_direct(url, *, retry):
    direct_calls.append(retry)
    return "<html></html>"


tasks_strategy.NetLedgerRecorder = _OpenFailsRecorder
tasks_strategy._fetch_direct = _fake_fetch_direct

# 1. LedgerOpenError -> the rung is skipped: `_fetch` returns None and the
#    underlying transport call (`_fetch_direct`) is never reached -- no
#    socket without a ledger row.
result = tasks_strategy._fetch(
    object(), workspace_id, AccessMethod.DIRECT_HTTP, url,
    authorization_id=authorization_id,
)
assert result is None, result
assert direct_calls == [], direct_calls


# 2. The happy path: `open` succeeds, the rung actually fetches, and
#    `close` is called with `settle_authorization=False` (the run settles
#    its own ladder-wide grant once, not per rung) and the intent carries
#    the threaded `authorization_id` + the right `NetworkTransport`.
opened = []
closed = []


class _RecordingRecorder:
    def __init__(self, *, costauth=None):
        pass

    def open(self, intent):
        opened.append(intent)
        return uuid.uuid4()

    def close(self, network_request_id, outcome):
        closed.append(outcome)


tasks_strategy.NetLedgerRecorder = _RecordingRecorder
direct_calls.clear()
result = tasks_strategy._fetch(
    object(), workspace_id, AccessMethod.DIRECT_HTTP, url,
    authorization_id=authorization_id,
)
assert result == "<html></html>", result
assert direct_calls == [False], direct_calls
assert len(opened) == 1, opened
assert opened[0].authorization_id == authorization_id, opened[0].authorization_id
assert opened[0].transport is NetworkTransport.DIRECT, opened[0].transport
assert opened[0].workspace_id == workspace_id, opened[0].workspace_id
assert len(closed) == 1, closed
assert closed[0].settle_authorization is False, closed[0].settle_authorization

# 3. PROXY_HTTP prices the rung (currency/billing_unit set); DIRECT_HTTP
#    (case 2, above) does not -- a fleet-side direct fetch is recorded
#    with a NULL cost, never a fabricated zero.
def _fake_fetch_via_proxy(session, workspace_id, url):
    return "<html>proxy</html>"


tasks_strategy._fetch_via_proxy = _fake_fetch_via_proxy
closed.clear()
result = tasks_strategy._fetch(
    object(), workspace_id, AccessMethod.PROXY_HTTP, url,
    authorization_id=authorization_id,
)
assert result == "<html>proxy</html>", result
assert opened[-1].transport is NetworkTransport.PROXY, opened[-1].transport
assert closed[0].currency == "USD", closed[0].currency
assert closed[0].billing_unit == "REQUEST", closed[0].billing_unit
assert closed[0].estimated_cost_minor_units is not None, closed[0].estimated_cost_minor_units
assert closed[0].settle_authorization is False, closed[0].settle_authorization

print("OK")
"""


def test_fetch_ledger_open_error_skips_the_rung_without_dispatching() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _LEDGER_WIRING_CHECK],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **_ENV},
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


# --- EPA Phase C F5 + F6: what the run RESERVES and what it SETTLES ------

_RUN_PRELUDE = """
import sys
sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

import uuid
from contextlib import contextmanager

from app_shared.enums import DiscoveryRunStatus

from app.workers import tasks_strategy


class FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


fake_session = FakeSession()


@contextmanager
def fake_get_session():
    yield fake_session


tasks_strategy.get_session = fake_get_session
tasks_strategy.set_workspace_context = lambda session, workspace_id: None
tasks_strategy._auto_run_in_flight = lambda *a, **k: False
tasks_strategy._runs_in_trailing_day = lambda *a, **k: 0
# SSRF validation has its own suite; here it would only add a DNS round
# trip to a test about money.
tasks_strategy.validate_competitor_url = lambda url: None
tasks_strategy._get_or_create_profile = lambda *a, **k: object()
tasks_strategy.seed_from_discovery = lambda *a, **k: None

workspace_id = str(uuid.uuid4())
competitor_id = str(uuid.uuid4())
SAMPLE = [
    "https://shop.example.com/p/1",
    "https://shop.example.com/p/2",
    "https://shop.example.com/p/3",
]


def the_run():
    runs = [o for o in fake_session.added if isinstance(o, tasks_strategy.StrategyDiscoveryRun)]
    if len(runs) != 1:
        fail("EXPECTED_EXACTLY_ONE_RUN_ROW_GOT:" + str(len(runs)))
    return runs[0]


def fail(message):
    print(message)
    sys.exit(1)
"""

# F6: a DENIED discovery must FAIL its run, not probe anyway and not
# finish silently. The run row is what the operator sees, so a refusal has
# to be visible there rather than as a run that never happened.
_DISCOVERY_DENIAL_CHECK = _RUN_PRELUDE + """
from _costauth_test_stub import DenyingCostAuthorizationService, stub_cost_authorization

asked = []


class _RecordingDeny(DenyingCostAuthorizationService):
    def authorize(self, req):
        asked.append(req)
        return super().authorize(req)


stub_cost_authorization(tasks_strategy, _RecordingDeny)

probed = []
tasks_strategy._probe_sample = lambda *a, **k: probed.append(1) or {}

tasks_strategy.run_discovery(
    workspace_id,
    competitor_id,
    "shop.example.com",
    "https://shop.example.com/p",
    sample_urls=list(SAMPLE),
)

# The run must have reached the GATE -- `_fail_run` is also how an
# out-of-bounds sample and an all-unsafe url list end, so a FAILED run
# alone would not prove the denial branch ran.
if len(asked) != 1:
    fail("THE_GATE_WAS_NOT_CONSULTED_GOT:" + str(len(asked)))

if probed:
    fail("A_DENIED_DISCOVERY_RUN_PROBED_ANYWAY")

run = the_run()
if run.status is not DiscoveryRunStatus.FAILED:
    fail("DENIED_RUN_NOT_MARKED_FAILED:" + str(run.status))

print("OK")
sys.exit(0)
"""

# F5: the reservation is sized for the WHOLE ladder walk, and settlement
# records what the walk OBSERVED -- never the estimate that was reserved.
_DISCOVERY_ACCOUNTING_CHECK = _RUN_PRELUDE + """
from _costauth_test_stub import AlwaysGrantCostAuthorizationService, stub_cost_authorization

requests_seen = []
settled = []


class _RecordingCostAuth(AlwaysGrantCostAuthorizationService):
    def authorize(self, req):
        requests_seen.append(req)
        return super().authorize(req)

    def settle(self, authorization_id, actual=None):
        settled.append(actual)


stub_cost_authorization(tasks_strategy, _RecordingCostAuth)


def fake_probe(session, *, workspace_id, urls, thresholds, competitor_id=None,
               authorization_id=None, budget_decision_version=None,
               entitlement_version=None, breaker_decision=None, observed=None):
    # Stand in for the ladder walk: three rungs per url, only the PROXY
    # one costing money -- exactly the shape `_fetch` records.
    for _ in urls:
        observed.record(bytes_used=1000, cost_minor_units=None)   # DIRECT
        observed.record(bytes_used=1000, cost_minor_units=None)   # DIRECT_RETRY
        observed.record(bytes_used=2000, cost_minor_units=7)      # PROXY
    return {}


tasks_strategy._probe_sample = fake_probe
tasks_strategy.select_discovery_winner = lambda tallies: None

tasks_strategy.run_discovery(
    workspace_id,
    competitor_id,
    "shop.example.com",
    "https://shop.example.com/p",
    sample_urls=list(SAMPLE),
)

if len(requests_seen) != 1:
    fail("EXPECTED_ONE_AUTHORIZATION_GOT:" + str(len(requests_seen)))

req = requests_seen[0]
ladder = len(tasks_strategy._ACCESS_LADDER)
if req.estimated_requests != len(SAMPLE) * ladder:
    fail(
        "RESERVATION_MISSED_THE_LADDER_FACTOR:"
        + str((req.estimated_requests, len(SAMPLE), ladder))
    )
if req.estimated_bytes < len(SAMPLE) * ladder:
    fail("RESERVED_BYTES_MISSED_THE_LADDER_FACTOR:" + str(req.estimated_bytes))

if len(settled) != 1:
    fail("EXPECTED_ONE_TERMINAL_SETTLE_GOT:" + str(len(settled)))

actual = settled[0]
# OBSERVED, not reserved: 9 fetches, 12000 bytes, 21 minor units (three
# priced PROXY rungs at 7).
if actual.requests != len(SAMPLE) * ladder:
    fail("SETTLED_REQUEST_COUNT_IS_NOT_THE_OBSERVED_ONE:" + str(actual.requests))
if actual.bytes_used != len(SAMPLE) * 4000:
    fail("SETTLED_BYTES_ARE_NOT_THE_OBSERVED_ONES:" + str(actual.bytes_used))
if actual.cost_minor_units != len(SAMPLE) * 7:
    fail("SETTLED_COST_IS_NOT_THE_OBSERVED_ONE:" + str(actual.cost_minor_units))
# ...and it is emphatically NOT the reserved estimate.
if actual.cost_minor_units == req.estimated_cost_minor_units:
    fail("SETTLED_THE_RESERVED_ESTIMATE_INSTEAD_OF_THE_OBSERVED_TOTAL")

print("OK")
sys.exit(0)
"""

_RUN_ENV = {
    **_ENV,
    "STRATEGY_DISCOVERY_MIN_SAMPLE": "1",
    "STRATEGY_DISCOVERY_MAX_SAMPLE": "10",
    "STRATEGY_DISCOVERY_MAX_RUNS_PER_KEY_PER_DAY": "0",
}


def _run_discovery_check(script: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **_RUN_ENV},
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


def test_a_denied_discovery_run_fails_the_run_and_probes_nothing() -> None:
    """EPA Phase C F6: `run_discovery`'s denial branch.

    Discovery is the most expensive task in the system to run by mistake —
    it walks the whole access ladder over the whole sample — so a denial
    must stop it before the first fetch AND be visible on the run row,
    rather than leaving an operator with a discovery that silently never
    happened."""
    _run_discovery_check(_DISCOVERY_DENIAL_CHECK)


def test_discovery_reserves_the_whole_ladder_and_settles_what_it_observed() -> None:
    """EPA Phase C F5: the two halves of honest discovery accounting.

    RESERVE for `len(urls) * len(_ACCESS_LADDER)` fetches, because that is
    what `_probe_sample` actually makes — reserving `len(urls)` left the
    only ceiling over an unbounded discovery loop short by the ladder
    factor. SETTLE the OBSERVED total, because settling the reserved
    estimate makes settlement a no-op dressed as accounting: the budget
    records the guess forever and no over- or under-run is ever visible."""
    _run_discovery_check(_DISCOVERY_ACCOUNTING_CHECK)
