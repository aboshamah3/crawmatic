"""`recompute_variant` task unit tests (SPEC-09 T021/T027, US1+US2,
contracts/price-analysis-task.md).

Fake session (`FakeAlertsSession`) + fake `MatchCurrentPrice` rows — no
DB. Per the contract: `set_workspace_context` runs before any query; a
missing variant is a no-op; a comparable competitor set writes correct
`variant_price_states` benchmarks/count/type + `variant_alert_states`
type/severity/status/lifecycle; a currency-mismatched competitor is
excluded from benchmarks and its `match_current_prices` row flipped
`comparable=false`/`CURRENCY_MISMATCH`; re-running with unchanged inputs
writes identical state (only `calculated_at`/`updated_at` advance).
Event-write cases (`price_alert_events`, US2 T023/T027): driving
NORMAL -> HIGH_PRICE -> NORMAL -> HIGH_PRICE yields exactly one CREATED,
one RESOLVED, one REOPENED; a same-type severity change yields UPDATED;
an unchanged re-run writes zero events while advancing `last_seen_at`;
`latest_alert_state_id` links the alert-state row.

Loaded in a fresh subprocess (mirrors `test_jobs_dispatch_task.py`/
`test_jobs_counters.py`'s `_REFRESH_COUNTERS_CHECK`), for the same two
reasons: (1) `apps/api` and `apps/workers` each ship their own top-level
``app`` package, so importing `app.workers.tasks_analysis` in the shared
test process is ambiguous once another test module has already imported
`apps/api`'s `app` package; (2) `celery_app.py` calls `get_settings()` at
module scope, needing a clean, self-contained env.
"""

from __future__ import annotations

import os
import subprocess
import sys

_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
}

_COMMON_SETUP = """
import sys
import uuid
from contextlib import contextmanager
from decimal import Decimal
from datetime import datetime, timezone

sys.path.insert(0, "apps/workers")
sys.path.insert(0, "tests/unit")

from _alerts_fake_session import FakeAlertsSession
from app_shared.enums import AlertEventType, AlertSeverity, AlertStatus, AlertType, ScrapeErrorCode
from app_shared.models.catalog import ProductVariant
from app_shared.models.observations import MatchCurrentPrice
from app_shared.models.alerts import PriceAlertEvent, VariantAlertState, VariantPriceState

import app.workers.tasks_analysis as tasks_analysis

call_order = []


def fake_set_workspace_context(session, workspace_id):
    call_order.append("set_workspace_context")


tasks_analysis.set_workspace_context = fake_set_workspace_context

fake_session = FakeAlertsSession()


@contextmanager
def fake_get_session():
    yield fake_session


tasks_analysis.get_session = fake_get_session

workspace_id = uuid.uuid4()
product_id = uuid.uuid4()
variant_id = uuid.uuid4()

variant = ProductVariant(
    workspace_id=workspace_id,
    product_id=product_id,
    title="Widget",
    current_price=Decimal("95"),
    currency="SAR",
    status="active",
)
variant.id = variant_id
fake_session.seed(variant)


def make_match(price, currency="SAR", success=True, comparable=True):
    match = MatchCurrentPrice(
        workspace_id=workspace_id,
        match_id=uuid.uuid4(),
        product_id=product_id,
        product_variant_id=variant_id,
        competitor_id=uuid.uuid4(),
        price=Decimal(price) if price is not None else None,
        currency=currency,
        comparable=comparable,
        success=success,
    )
    match.id = uuid.uuid4()
    return match
"""


def _run(script_body: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **_ENV}
    return subprocess.run(
        [sys.executable, "-c", _COMMON_SETUP + script_body],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_missing_variant_is_a_noop() -> None:
    script = """
unknown_variant_id = uuid.uuid4()
tasks_analysis.recompute_variant(
    workspace_id=str(workspace_id), product_variant_id=str(unknown_variant_id)
)

if fake_session.committed:
    print("SHOULD_NOT_HAVE_COMMITTED")
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    result = _run(script)
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


def test_set_workspace_context_before_any_query() -> None:
    script = """
fake_session.seed(make_match("90"), make_match("100"), make_match("110"))

tasks_analysis.recompute_variant(
    workspace_id=str(workspace_id), product_variant_id=str(variant_id)
)

if not call_order or call_order[0] != "set_workspace_context":
    print("ORDER_WRONG:" + str(call_order[:1]))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    result = _run(script)
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


def test_comparable_competitors_write_correct_price_and_alert_state() -> None:
    script = """
fake_session.seed(make_match("90"), make_match("100"), make_match("110"))

tasks_analysis.recompute_variant(
    workspace_id=str(workspace_id), product_variant_id=str(variant_id)
)

price_states = fake_session._rows.get(VariantPriceState, [])
if len(price_states) != 1:
    print("EXPECTED_ONE_PRICE_STATE:" + str(len(price_states)))
    sys.exit(1)
ps = price_states[0]

if ps.cheapest_competitor_price != Decimal("90"):
    print("WRONG_CHEAPEST:" + str(ps.cheapest_competitor_price))
    sys.exit(1)
if ps.highest_competitor_price != Decimal("110"):
    print("WRONG_HIGHEST:" + str(ps.highest_competitor_price))
    sys.exit(1)
if ps.average_competitor_price != Decimal("100"):
    print("WRONG_AVERAGE:" + str(ps.average_competitor_price))
    sys.exit(1)
if ps.comparable_competitor_count != 3:
    print("WRONG_COUNT:" + str(ps.comparable_competitor_count))
    sys.exit(1)
# client_price 95 <= cheapest 90? No: 95 > 90 -> HIGH_PRICE (step 3).
if ps.latest_alert_type != AlertType.HIGH_PRICE:
    print("WRONG_TYPE:" + str(ps.latest_alert_type))
    sys.exit(1)
if ps.latest_alert_severity != AlertSeverity.HIGH:
    print("WRONG_SEVERITY:" + str(ps.latest_alert_severity))
    sys.exit(1)

alert_states = fake_session._rows.get(VariantAlertState, [])
if len(alert_states) != 1:
    print("EXPECTED_ONE_ALERT_STATE:" + str(len(alert_states)))
    sys.exit(1)
alert_state = alert_states[0]
if alert_state.type != AlertType.HIGH_PRICE:
    print("WRONG_ALERT_TYPE:" + str(alert_state.type))
    sys.exit(1)
if alert_state.status != AlertStatus.ACTIVE:
    print("WRONG_STATUS:" + str(alert_state.status))
    sys.exit(1)
if alert_state.first_seen_at is None or alert_state.last_seen_at is None:
    print("MISSING_LIFECYCLE_TIMESTAMPS")
    sys.exit(1)
if alert_state.resolved_at is not None:
    print("UNEXPECTED_RESOLVED_AT")
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    result = _run(script)
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


def test_currency_mismatched_competitor_excluded_and_flipped() -> None:
    script = """
mismatched = make_match("500", currency="USD")
matching = [make_match("90"), make_match("100"), make_match("110")]
fake_session.seed(mismatched, *matching)

tasks_analysis.recompute_variant(
    workspace_id=str(workspace_id), product_variant_id=str(variant_id)
)

price_states = fake_session._rows.get(VariantPriceState, [])
ps = price_states[0]
if ps.comparable_competitor_count != 3:
    print("MISMATCH_INCLUDED_IN_COUNT:" + str(ps.comparable_competitor_count))
    sys.exit(1)
if ps.highest_competitor_price != Decimal("110"):
    print("MISMATCH_AFFECTED_HIGHEST:" + str(ps.highest_competitor_price))
    sys.exit(1)

if mismatched.comparable is not False:
    print("MISMATCH_NOT_FLIPPED_COMPARABLE")
    sys.exit(1)
if mismatched.error_code != ScrapeErrorCode.CURRENCY_MISMATCH:
    print("MISMATCH_ERROR_CODE_NOT_SET:" + str(mismatched.error_code))
    sys.exit(1)

# Matching-currency rows must be untouched.
for m in matching:
    if m.comparable is not True:
        print("MATCHING_ROW_WRONGLY_FLIPPED")
        sys.exit(1)

print("OK")
sys.exit(0)
"""
    result = _run(script)
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


def test_no_competitors_is_no_competitor_data() -> None:
    script = """
tasks_analysis.recompute_variant(
    workspace_id=str(workspace_id), product_variant_id=str(variant_id)
)

price_states = fake_session._rows.get(VariantPriceState, [])
ps = price_states[0]
if ps.latest_alert_type != AlertType.NO_COMPETITOR_DATA:
    print("WRONG_TYPE:" + str(ps.latest_alert_type))
    sys.exit(1)
if ps.comparable_competitor_count != 0:
    print("WRONG_COUNT:" + str(ps.comparable_competitor_count))
    sys.exit(1)
if ps.cheapest_competitor_price is not None or ps.average_competitor_price is not None or ps.highest_competitor_price is not None:
    print("BENCHMARKS_NOT_NULL")
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    result = _run(script)
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


# --- US2 (T027): event-write path ---------------------------------------


def test_event_transition_sequence_created_resolved_reopened_unchanged() -> None:
    """Drive HIGH_PRICE(created) -> NORMAL(resolved) -> HIGH_PRICE(reopened)
    -> HIGH_PRICE again (unchanged, zero new events) via one fixed comparable
    set (cheapest=100, average=101, highest=104) and only the variant's
    client price changing between calls (contracts/price-analysis-task.md
    step 9; the "NORMAL -> HIGH_PRICE -> NORMAL -> HIGH_PRICE" business-state
    sequence from tasks.md Phase 4, where the leading NORMAL is the implicit
    no-row starting point: the first persisted row is itself CREATED)."""
    script = """
fake_session.seed(make_match("100"), make_match("100"), make_match("100"), make_match("104"))

# Step 1: price=102 -> HIGH_PRICE (cheapest=100 < 102 <= highest=104). No
# prior row -> CREATED.
variant.current_price = Decimal("102")
tasks_analysis.recompute_variant(workspace_id=str(workspace_id), product_variant_id=str(variant_id))

events = fake_session._rows.get(PriceAlertEvent, [])
if len(events) != 1:
    print("EXPECTED_ONE_EVENT_AFTER_STEP1:" + str(len(events)))
    sys.exit(1)
ev1 = events[0]
if ev1.event_type != AlertEventType.CREATED:
    print("STEP1_WRONG_EVENT_TYPE:" + str(ev1.event_type))
    sys.exit(1)
if ev1.previous_type is not None or ev1.new_type != AlertType.HIGH_PRICE:
    print("STEP1_WRONG_TYPES:" + str((ev1.previous_type, ev1.new_type)))
    sys.exit(1)

alert_states = fake_session._rows.get(VariantAlertState, [])
if len(alert_states) != 1:
    print("EXPECTED_ONE_ALERT_STATE_ROW:" + str(len(alert_states)))
    sys.exit(1)
alert_state = alert_states[0]

price_states = fake_session._rows.get(VariantPriceState, [])
ps = price_states[0]
if ps.latest_alert_state_id != alert_state.id:
    print("LATEST_ALERT_STATE_ID_NOT_LINKED_STEP1")
    sys.exit(1)

# Step 2: price=97 -> NORMAL (<=cheapest=100; discount vs avg=101 is
# (101-97)/101*100 = 3.96%, in [1,5]). prior=HIGH_PRICE -> RESOLVED.
variant.current_price = Decimal("97")
tasks_analysis.recompute_variant(workspace_id=str(workspace_id), product_variant_id=str(variant_id))

events = fake_session._rows.get(PriceAlertEvent, [])
if len(events) != 2:
    print("EXPECTED_TWO_EVENTS_AFTER_STEP2:" + str(len(events)))
    sys.exit(1)
ev2 = events[1]
if ev2.event_type != AlertEventType.RESOLVED:
    print("STEP2_WRONG_EVENT_TYPE:" + str(ev2.event_type))
    sys.exit(1)
if ev2.previous_type != AlertType.HIGH_PRICE or ev2.new_type != AlertType.NORMAL:
    print("STEP2_WRONG_TYPES:" + str((ev2.previous_type, ev2.new_type)))
    sys.exit(1)

alert_states = fake_session._rows.get(VariantAlertState, [])
if len(alert_states) != 1:
    print("RESOLVE_SHOULD_UPDATE_SAME_ROW:" + str(len(alert_states)))
    sys.exit(1)
if alert_states[0].status != AlertStatus.RESOLVED or alert_states[0].resolved_at is None:
    print("STEP2_NOT_MARKED_RESOLVED")
    sys.exit(1)

# Step 3: back to price=102 -> HIGH_PRICE. prior=NORMAL, had_history=True
# -> REOPENED (resolved_at cleared).
variant.current_price = Decimal("102")
tasks_analysis.recompute_variant(workspace_id=str(workspace_id), product_variant_id=str(variant_id))

events = fake_session._rows.get(PriceAlertEvent, [])
if len(events) != 3:
    print("EXPECTED_THREE_EVENTS_AFTER_STEP3:" + str(len(events)))
    sys.exit(1)
ev3 = events[2]
if ev3.event_type != AlertEventType.REOPENED:
    print("STEP3_WRONG_EVENT_TYPE:" + str(ev3.event_type))
    sys.exit(1)
if ev3.previous_type != AlertType.NORMAL or ev3.new_type != AlertType.HIGH_PRICE:
    print("STEP3_WRONG_TYPES:" + str((ev3.previous_type, ev3.new_type)))
    sys.exit(1)

alert_states = fake_session._rows.get(VariantAlertState, [])
if alert_states[0].status != AlertStatus.ACTIVE or alert_states[0].resolved_at is not None:
    print("STEP3_NOT_REOPENED_PROPERLY")
    sys.exit(1)

# Step 4: unchanged re-run (same price=102) -> zero new events, only
# last_seen_at advances.
last_seen_before = alert_states[0].last_seen_at
import time
time.sleep(0.01)
tasks_analysis.recompute_variant(workspace_id=str(workspace_id), product_variant_id=str(variant_id))

events = fake_session._rows.get(PriceAlertEvent, [])
if len(events) != 3:
    print("UNCHANGED_RERUN_WROTE_EVENTS:" + str(len(events)))
    sys.exit(1)
alert_states = fake_session._rows.get(VariantAlertState, [])
if alert_states[0].last_seen_at <= last_seen_before:
    print("LAST_SEEN_AT_DID_NOT_ADVANCE_ON_UNCHANGED_RERUN")
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    result = _run(script)
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


def test_type_change_between_non_normal_alerts_is_updated() -> None:
    """A type change between two non-NORMAL alerts (HIGH_PRICE -> RISK) is
    UPDATED (contracts/alert-engine.md: "same-type severity change -> UPDATED"
    is the defensive branch of this same rule, exercised only via a
    hand-constructed input at the pure-engine level — T016 I1 — since
    severity is a pure function of type and can never differ for the same
    type through the real engine/task)."""
    script = """
fake_session.seed(make_match("100"), make_match("100"), make_match("100"), make_match("104"))

variant.current_price = Decimal("102")  # HIGH_PRICE (created)
tasks_analysis.recompute_variant(workspace_id=str(workspace_id), product_variant_id=str(variant_id))

variant.current_price = Decimal("110")  # > highest(104) -> RISK
tasks_analysis.recompute_variant(workspace_id=str(workspace_id), product_variant_id=str(variant_id))

events = fake_session._rows.get(PriceAlertEvent, [])
if len(events) != 2:
    print("EXPECTED_TWO_EVENTS:" + str(len(events)))
    sys.exit(1)
ev = events[1]
if ev.event_type != AlertEventType.UPDATED:
    print("WRONG_EVENT_TYPE:" + str(ev.event_type))
    sys.exit(1)
if ev.previous_type != AlertType.HIGH_PRICE or ev.new_type != AlertType.RISK:
    print("WRONG_TYPES:" + str((ev.previous_type, ev.new_type)))
    sys.exit(1)
if ev.previous_severity != AlertSeverity.HIGH or ev.new_severity != AlertSeverity.CRITICAL:
    print("WRONG_SEVERITIES:" + str((ev.previous_severity, ev.new_severity)))
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    result = _run(script)
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"


def test_rerun_with_unchanged_inputs_is_idempotent_only_timestamps_advance() -> None:
    script = """
import time

fake_session.seed(make_match("90"), make_match("100"), make_match("110"))

tasks_analysis.recompute_variant(
    workspace_id=str(workspace_id), product_variant_id=str(variant_id)
)
price_states = fake_session._rows.get(VariantPriceState, [])
alert_states = fake_session._rows.get(VariantAlertState, [])
if len(price_states) != 1 or len(alert_states) != 1:
    print("EXPECTED_SINGLE_ROWS")
    sys.exit(1)

ps_first = price_states[0]
as_first = alert_states[0]
first_snapshot = (
    ps_first.cheapest_competitor_price,
    ps_first.average_competitor_price,
    ps_first.highest_competitor_price,
    ps_first.comparable_competitor_count,
    ps_first.latest_alert_type,
    ps_first.latest_alert_severity,
)
first_alert_snapshot = (
    as_first.type,
    as_first.severity,
    as_first.status,
    as_first.client_price,
    as_first.benchmark_price,
    as_first.message,
)
first_seen_at_first_run = as_first.first_seen_at
last_seen_at_first_run = as_first.last_seen_at

time.sleep(0.01)

tasks_analysis.recompute_variant(
    workspace_id=str(workspace_id), product_variant_id=str(variant_id)
)

price_states = fake_session._rows.get(VariantPriceState, [])
alert_states = fake_session._rows.get(VariantAlertState, [])
if len(price_states) != 1 or len(alert_states) != 1:
    print("RERUN_CREATED_DUPLICATE_ROWS:" + str((len(price_states), len(alert_states))))
    sys.exit(1)

ps_second = price_states[0]
as_second = alert_states[0]
second_snapshot = (
    ps_second.cheapest_competitor_price,
    ps_second.average_competitor_price,
    ps_second.highest_competitor_price,
    ps_second.comparable_competitor_count,
    ps_second.latest_alert_type,
    ps_second.latest_alert_severity,
)
second_alert_snapshot = (
    as_second.type,
    as_second.severity,
    as_second.status,
    as_second.client_price,
    as_second.benchmark_price,
    as_second.message,
)

if first_snapshot != second_snapshot:
    print("STATE_CHANGED_ON_RERUN:" + str((first_snapshot, second_snapshot)))
    sys.exit(1)
if first_alert_snapshot != second_alert_snapshot:
    print("ALERT_STATE_CHANGED_ON_RERUN:" + str((first_alert_snapshot, second_alert_snapshot)))
    sys.exit(1)
if as_second.first_seen_at != first_seen_at_first_run:
    print("FIRST_SEEN_AT_CHANGED_ON_UNCHANGED_RERUN")
    sys.exit(1)
if as_second.last_seen_at <= last_seen_at_first_run:
    print("LAST_SEEN_AT_DID_NOT_ADVANCE")
    sys.exit(1)

print("OK")
sys.exit(0)
"""
    result = _run(script)
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"
