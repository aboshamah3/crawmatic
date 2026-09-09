"""EPA C6 / F18 — reservation by the KNOWN first rung.

Before F18 every HTTP batch was authorized as `PROXY` with a full byte
and money estimate, "fail-closed", because the dispatch planner could
not know which rung of the strategy ladder the spider would actually
land on. It can now: the playbook names a `cheap_path`, and that IS the
first rung. Reserving proxy money for a batch that will start (and,
overwhelmingly, finish) on free fleet egress is not conservatism — it is
a byte and money ceiling burned by traffic no provider ever bills, which
denies real paid work later in the same budget period.

What these tests pin down:

1. A `DIRECT` first rung reserves ZERO bytes, ZERO money and ZERO
   browser seconds — but is still a real authorization request, on
   `DIRECT` transport, with a real request count, so the breaker, the
   entitlement and the concurrency cap still apply to free traffic.
2. `PROXY`/`BROWSER` first rungs still reserve bytes and money exactly as
   before, priced through the one module that owns the rates.
3. `estimated_requests` is the COALESCED count of unique PHYSICAL
   requests, never the batch's match count — three matches on one
   competitor URL are one fetch.
4. Escalation reserves SEPARATELY, at escalation time, under
   `PROXY_ESCALATION` (proxy rung) or `BROWSER_ESCALATION` (browser
   rung), for the full cost of the rung being climbed to.
"""

from __future__ import annotations

import uuid

import pytest

from app_shared.costauth import (
    ESTIMATED_BROWSER_WALL_SECONDS_PER_REQUEST,
    AuthorizationPurpose,
    AuthorizationRequest,
    DenialReason,
    FirstRungReservation,
    costauth_denials_by_reason,
    escalation_reservation,
    estimate_bytes,
    estimate_reservation_micro_units,
    first_rung_reservation,
    reset_costauth_denials,
)
from app_shared.costauth.service import (
    FLEET_PROVIDER_BROWSER,
    FLEET_PROVIDER_DIRECT,
    FLEET_PROVIDER_PROXY,
    CostAuthorizationDenied,
)


# --- 1. the direct first rung ------------------------------------------------


def test_direct_first_rung_reserves_no_bytes_and_no_money() -> None:
    rung = first_rung_reservation(initial_transport="DIRECT", unique_requests=12)
    assert rung.transport == "DIRECT"
    assert rung.provider == FLEET_PROVIDER_DIRECT
    assert rung.estimated_bytes == 0
    assert rung.estimated_cost_micro_units == 0
    assert rung.estimated_browser_seconds == 0


def test_direct_first_rung_is_still_authorized_for_the_non_money_gates() -> None:
    """Zero money is not "skip the gate". A `DIRECT` rung still builds a
    complete `AuthorizationRequest` — that is what puts it in front of
    the breaker, the entitlement check and the concurrency cap, none of
    which care whether the traffic is billed."""
    rung = first_rung_reservation(initial_transport="DIRECT", unique_requests=4)
    request = AuthorizationRequest(
        workspace_id=uuid.uuid4(),
        domain="example.com",
        purpose=AuthorizationPurpose.REFRESH,
        transport=rung.transport,
        provider=rung.provider,
        estimated_bytes=rung.estimated_bytes,
        estimated_cost_micro_units=rung.estimated_cost_micro_units,
        estimated_requests=rung.estimated_requests,
        estimated_browser_seconds=rung.estimated_browser_seconds,
    )
    assert request.transport == "DIRECT"
    assert request.estimated_bytes == 0
    # The REQUEST dimension still binds: a direct fetch is still a fetch,
    # and a runaway loop can make a million of them.
    assert request.estimated_requests == 4


def test_direct_first_rung_still_counts_requests() -> None:
    assert (
        first_rung_reservation(
            initial_transport="DIRECT", unique_requests=9
        ).estimated_requests
        == 9
    )


# --- 2. the paid rungs are unchanged ----------------------------------------


def test_proxy_first_rung_reserves_bytes_and_money() -> None:
    rung = first_rung_reservation(initial_transport="PROXY", unique_requests=5)
    assert rung.transport == "PROXY"
    assert rung.provider == FLEET_PROVIDER_PROXY
    assert rung.estimated_bytes == estimate_bytes(5)
    assert rung.estimated_cost_micro_units == estimate_reservation_micro_units(
        transport="PROXY", requests=5
    )
    assert rung.estimated_cost_micro_units > 0
    assert rung.estimated_browser_seconds == 0


def test_browser_first_rung_reserves_bytes_money_and_browser_seconds() -> None:
    rung = first_rung_reservation(initial_transport="BROWSER", unique_requests=3)
    assert rung.transport == "BROWSER"
    assert rung.provider == FLEET_PROVIDER_BROWSER
    assert rung.estimated_bytes == estimate_bytes(3)
    assert rung.estimated_cost_micro_units == estimate_reservation_micro_units(
        transport="BROWSER", requests=3
    )
    assert (
        rung.estimated_browser_seconds
        == 3 * ESTIMATED_BROWSER_WALL_SECONDS_PER_REQUEST
    )


def test_a_paid_rung_never_reserves_a_ceiling_of_zero() -> None:
    """A ceiling of zero is not a ceiling."""
    for transport in ("PROXY", "BROWSER"):
        rung = first_rung_reservation(initial_transport=transport, unique_requests=1)
        assert rung.estimated_bytes > 0, transport
        assert rung.estimated_cost_micro_units > 0, transport


def test_transport_is_normalized_and_an_unknown_rung_raises() -> None:
    assert first_rung_reservation(
        initial_transport="direct", unique_requests=1
    ).transport == "DIRECT"
    with pytest.raises(ValueError, match="unknown transport"):
        first_rung_reservation(initial_transport="SMOKE_SIGNAL", unique_requests=1)


# --- 3. requests are COALESCED physical requests -----------------------------


def test_estimated_requests_is_the_coalesced_unique_physical_count() -> None:
    """Three matches pointing at ONE competitor URL are ONE fetch. The
    caller coalesces; this helper must carry the coalesced number through
    untouched rather than re-deriving it from a match count."""
    coalesced = first_rung_reservation(initial_transport="PROXY", unique_requests=1)
    naive = first_rung_reservation(initial_transport="PROXY", unique_requests=3)
    assert coalesced.estimated_requests == 1
    assert naive.estimated_requests == 3
    # And the money follows the coalesced count, not the match count.
    assert coalesced.estimated_cost_micro_units < naive.estimated_cost_micro_units


def test_a_zero_or_negative_request_count_floors_at_one() -> None:
    """A batch always makes at least one fetch; reserving for zero would
    reserve nothing at all on a paid rung."""
    for count in (0, -4):
        rung = first_rung_reservation(initial_transport="PROXY", unique_requests=count)
        assert rung.estimated_requests == 1
        assert rung.estimated_cost_micro_units > 0


# --- 4. escalation reserves separately, at escalation time -------------------


def test_escalating_to_proxy_uses_the_new_proxy_escalation_purpose() -> None:
    purpose, rung = escalation_reservation(to_transport="PROXY", unique_requests=2)
    assert purpose is AuthorizationPurpose.PROXY_ESCALATION
    assert purpose.value == "PROXY_ESCALATION"
    assert rung.transport == "PROXY"


def test_escalating_to_browser_keeps_the_existing_browser_purpose() -> None:
    purpose, rung = escalation_reservation(to_transport="BROWSER", unique_requests=2)
    assert purpose is AuthorizationPurpose.BROWSER_ESCALATION
    assert rung.transport == "BROWSER"


def test_escalation_reserves_the_full_cost_of_the_rung_climbed_to() -> None:
    """Not a delta against the first rung's grant. The first rung holds a
    live reservation for the transport IT named (zero, for DIRECT); a
    top-up that mutated it would make one grant describe two different
    physical transports."""
    _, escalated = escalation_reservation(to_transport="PROXY", unique_requests=6)
    standalone = first_rung_reservation(initial_transport="PROXY", unique_requests=6)
    assert escalated == standalone
    assert isinstance(escalated, FirstRungReservation)


def test_escalation_never_climbs_back_down_to_direct() -> None:
    with pytest.raises(ValueError, match="never back down"):
        escalation_reservation(to_transport="DIRECT", unique_requests=1)


def test_direct_first_rung_plus_proxy_escalation_costs_the_same_as_proxy_alone() -> None:
    """The F18 bet, stated as an equation: a ladder that starts DIRECT and
    escalates reserves exactly what an always-proxy batch reserved, and a
    ladder that never escalates reserves nothing. The saving is real
    money, not an accounting trick."""
    direct = first_rung_reservation(initial_transport="DIRECT", unique_requests=8)
    _, escalated = escalation_reservation(to_transport="PROXY", unique_requests=8)
    always_proxy = first_rung_reservation(initial_transport="PROXY", unique_requests=8)
    assert direct.estimated_cost_micro_units == 0
    assert (
        direct.estimated_cost_micro_units + escalated.estimated_cost_micro_units
        == always_proxy.estimated_cost_micro_units
    )


# --- 5. denials-by-reason, exported to opsmetrics ----------------------------


def test_every_denial_reason_is_a_present_series_even_at_zero() -> None:
    reset_costauth_denials()
    counts = costauth_denials_by_reason()
    assert set(counts) == {reason.value for reason in DenialReason}
    assert set(counts.values()) == {0}


def test_a_denial_increments_its_reason_and_only_its_reason() -> None:
    reset_costauth_denials()
    CostAuthorizationDenied(DenialReason.BREAKER_OPEN, "breaker is durably OPEN")
    CostAuthorizationDenied(DenialReason.BREAKER_OPEN)
    CostAuthorizationDenied(DenialReason.MONEY_BUDGET_EXCEEDED)
    counts = costauth_denials_by_reason()
    assert counts["BREAKER_OPEN"] == 2
    assert counts["MONEY_BUDGET_EXCEEDED"] == 1
    assert counts["CONCURRENCY_CAP_EXCEEDED"] == 0
    reset_costauth_denials()


def test_the_snapshot_is_a_copy_not_a_live_view() -> None:
    reset_costauth_denials()
    before = costauth_denials_by_reason()
    CostAuthorizationDenied(DenialReason.ENTITLEMENT_INACTIVE)
    assert before["ENTITLEMENT_INACTIVE"] == 0
    assert costauth_denials_by_reason()["ENTITLEMENT_INACTIVE"] == 1
    reset_costauth_denials()


def test_opsmetrics_renders_the_denial_counter_as_a_labelled_counter() -> None:
    from app_shared.opsmetrics import render_costauth_denials_prometheus

    text = render_costauth_denials_prometheus(
        {"BREAKER_OPEN": 4, "MONEY_BUDGET_EXCEEDED": 0}
    )
    assert "# TYPE crawmatic_costauth_denials_total counter" in text
    assert 'crawmatic_costauth_denials_total{reason="BREAKER_OPEN"} 4' in text
    # A never-fired reason is still a series -- a dashboard cannot alert
    # on the absence of a series it has never seen.
    assert 'crawmatic_costauth_denials_total{reason="MONEY_BUDGET_EXCEEDED"} 0' in text


def test_opsmetrics_defaults_to_the_live_costauth_tally() -> None:
    from app_shared.opsmetrics import render_costauth_denials_prometheus

    reset_costauth_denials()
    CostAuthorizationDenied(DenialReason.REQUEST_BUDGET_EXCEEDED)
    text = render_costauth_denials_prometheus()
    assert 'crawmatic_costauth_denials_total{reason="REQUEST_BUDGET_EXCEEDED"} 1' in text
    reset_costauth_denials()


# ---------------------------------------------------------------------------
# EPA C6 (F18), attempt 2 — the DISPATCH CALL SITE.
#
# The helpers above are only worth anything if the one place that reserves
# money for a batch actually uses them. These tests pin the whole chain:
# the strategy ladder's `AccessMethod` vocabulary -> the budget's rung
# vocabulary -> `plan_batches` stamping the rung, the playbook's cheap
# path and the coalesced request count onto the derived `Batch` ->
# `tasks_jobs._batch_authorization_request` spreading a `FirstRungReservation`
# into the `AuthorizationRequest` it hands the gate.
# ---------------------------------------------------------------------------

import os
import subprocess
import sys

from app_shared.costauth import reservation_rung
from app_shared.enums import AccessMethod, ScrapeProfileMode
from app_shared.jobs.batching import Batch, ResolvedTarget, plan_batches


def test_every_access_method_declares_exactly_one_budget_rung() -> None:
    """The ladder's vocabulary and the budget's vocabulary are different,
    and every member of the first must map to exactly one member of the
    second — a new `AccessMethod` with no declared cost is the bug this
    test exists to catch at build time rather than at reservation time."""
    assert {method: reservation_rung(method) for method in AccessMethod} == {
        AccessMethod.DIRECT_HTTP: "DIRECT",
        AccessMethod.DIRECT_HTTP_RETRY: "DIRECT",
        AccessMethod.PROXY_HTTP: "PROXY",
        AccessMethod.PLAYWRIGHT_DIRECT: "BROWSER",
        AccessMethod.PLAYWRIGHT_PROXY: "BROWSER",
    }


def test_an_unproxied_browser_still_bills_as_browser_not_direct() -> None:
    """`PLAYWRIGHT_DIRECT` makes no proxy request, but it still occupies a
    browser node for wall-seconds. Calling it free because its bytes are
    free would be exactly the over-optimism F18 is the mirror of."""
    assert reservation_rung(AccessMethod.PLAYWRIGHT_DIRECT) == "BROWSER"
    rung = first_rung_reservation(
        initial_transport=AccessMethod.PLAYWRIGHT_DIRECT, unique_requests=2
    )
    assert rung.transport == "BROWSER"
    assert rung.estimated_browser_seconds == (
        2 * ESTIMATED_BROWSER_WALL_SECONDS_PER_REQUEST
    )


def test_a_transport_outside_both_vocabularies_raises() -> None:
    with pytest.raises(ValueError):
        reservation_rung("CARRIER_PIGEON")


def _target(
    *,
    transport: str | None = None,
    cheap_path: str | None = None,
    url_hash: str | None = None,
    workspace: uuid.UUID | None = None,
) -> ResolvedTarget:
    return ResolvedTarget(
        match_id=uuid.uuid4(),
        competitor_domain="example.test",
        mode=ScrapeProfileMode.HTTP,
        strategy_method="m1",
        canonical_url_hash=url_hash,
        workspace_id=workspace,
        transport=transport,
        cheap_path=cheap_path,
    )


def test_plan_batches_stamps_the_rung_and_the_cheap_path_onto_the_batch() -> None:
    workspace = uuid.uuid4()
    batches = plan_batches(
        [
            _target(
                transport="DIRECT_HTTP",
                cheap_path="DIRECT_HTTP",
                url_hash=f"h{index}",
                workspace=workspace,
            )
            for index in range(3)
        ]
    )
    assert len(batches) == 1
    assert batches[0].initial_transport == "DIRECT_HTTP"
    assert batches[0].cheap_transport == "DIRECT_HTTP"
    assert batches[0].unique_physical_requests == 3


def test_three_matches_on_one_url_are_one_physical_request() -> None:
    """The reservation-side mirror of F17's aggregation-side over-count:
    reserving three requests for one fetch books a request ceiling against
    work that will never happen."""
    workspace = uuid.uuid4()
    batches = plan_batches(
        [
            _target(transport="PROXY_HTTP", url_hash="same", workspace=workspace)
            for _ in range(3)
        ]
    )
    assert len(batches[0].match_ids) == 3
    assert batches[0].unique_physical_requests == 1


def test_targets_with_no_identity_are_never_coalesced_with_each_other() -> None:
    """"We do not know this target's identity" must not collapse into
    "these targets share one" — and a chunk where NOBODY attached an
    identity reports `None`, not a fabricated count."""
    workspace = uuid.uuid4()
    unknown = plan_batches(
        [_target(transport="PROXY_HTTP", workspace=workspace) for _ in range(3)]
    )
    assert unknown[0].unique_physical_requests is None

    mixed = plan_batches(
        [
            _target(transport="PROXY_HTTP", url_hash="a", workspace=workspace),
            _target(transport="PROXY_HTTP", workspace=workspace),
            _target(transport="PROXY_HTTP", workspace=workspace),
        ]
    )
    assert mixed[0].unique_physical_requests == 3


def test_a_chunk_whose_members_disagree_reports_no_rung() -> None:
    """`(domain, mode, strategy_method)` does not contain the transport, so
    agreement is the caller's property, not this function's. Disagreement
    degrades to "unknown", which the call site reads fail-closed."""
    workspace = uuid.uuid4()
    batches = plan_batches(
        [
            _target(
                transport="DIRECT_HTTP",
                cheap_path="DIRECT_HTTP",
                url_hash="a",
                workspace=workspace,
            ),
            _target(
                transport="PROXY_HTTP",
                cheap_path="DIRECT_HTTP",
                url_hash="b",
                workspace=workspace,
            ),
        ]
    )
    assert batches[0].initial_transport is None
    assert batches[0].cheap_transport == "DIRECT_HTTP"


def test_a_pre_c6_caller_gets_exactly_the_pre_c6_batch() -> None:
    """Every field C6 adds defaults to `None`; a caller that attaches none
    of them plans byte-identically to before."""
    batches = plan_batches(
        [
            ResolvedTarget(
                match_id=uuid.uuid4(),
                competitor_domain="example.test",
                mode=ScrapeProfileMode.HTTP,
            )
        ]
    )
    assert batches[0].initial_transport is None
    assert batches[0].cheap_transport is None
    assert batches[0].unique_physical_requests is None


# --- the call site itself ---------------------------------------------
#
# `apps/workers` and `apps/api` each ship a top-level `app` package, and
# `celery_app.py` calls `get_settings()` at module scope, so
# `app.workers.tasks_jobs` is imported in a fresh subprocess exactly as
# `tests/unit/test_jobs_dispatch_task.py` does.

_CALL_SITE_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}

_CALL_SITE_CHECK = '''
import sys, uuid
sys.path.insert(0, "apps/workers")

from app_shared.costauth import AuthorizationPurpose
from app_shared.enums import ScrapeProfileMode
from app_shared.jobs.batching import Batch

import app.workers.tasks_jobs as tasks_jobs

WORKSPACE = uuid.uuid4()
JOB = uuid.uuid4()


class _Identity:
    key = "identity-key"


def request_for(**kwargs):
    purpose = kwargs.pop("purpose", AuthorizationPurpose.REFRESH)
    batch = Batch(
        batch_index=0,
        mode=kwargs.pop("mode", ScrapeProfileMode.HTTP),
        domain="example.test",
        match_ids=kwargs.pop("match_ids", [uuid.uuid4() for _ in range(4)]),
        **kwargs,
    )
    return tasks_jobs._batch_authorization_request(
        batch,
        workspace_id=WORKSPACE,
        scrape_job_id=JOB,
        purpose=purpose,
        identity=_Identity(),
    )


# 1. A DIRECT first rung reserves nothing but is still a real request.
req = request_for(initial_transport="DIRECT_HTTP", cheap_transport="DIRECT_HTTP")
assert req.transport == "DIRECT", req.transport
assert req.provider == "direct", req.provider
assert req.estimated_bytes == 0, req.estimated_bytes
assert req.estimated_cost_micro_units == 0, req.estimated_cost_micro_units
assert req.estimated_browser_seconds == 0
assert req.estimated_requests == 4, req.estimated_requests
assert req.purpose is AuthorizationPurpose.REFRESH
assert req.workspace_id == WORKSPACE and req.scrape_job_id == JOB
assert req.dedupe_key == "identity-key"

# 2. A PROXY first rung on a proxy-cheap domain still reserves bytes+money
#    and stays a plain REFRESH -- it is not a climb.
req = request_for(initial_transport="PROXY_HTTP", cheap_transport="PROXY_HTTP")
assert req.transport == "PROXY"
assert req.estimated_bytes > 0
assert req.estimated_cost_micro_units > 0
assert req.purpose is AuthorizationPurpose.REFRESH

# 3. PROXY above a DIRECT cheap path IS a climb: separate grant, own purpose.
req = request_for(initial_transport="PROXY_HTTP", cheap_transport="DIRECT_HTTP")
assert req.purpose is AuthorizationPurpose.PROXY_ESCALATION, req.purpose
assert req.transport == "PROXY"
assert req.estimated_bytes > 0

# 4. BROWSER above a DIRECT cheap path escalates under BROWSER_ESCALATION,
#    and reserves the FULL cost of the rung climbed to, not a delta.
req = request_for(
    initial_transport="PLAYWRIGHT_PROXY",
    cheap_transport="DIRECT_HTTP",
    mode=ScrapeProfileMode.BROWSER,
)
assert req.purpose is AuthorizationPurpose.BROWSER_ESCALATION, req.purpose
assert req.transport == "BROWSER"
assert req.estimated_browser_seconds == 4 * 30, req.estimated_browser_seconds

# 5. A RETRY keeps its own purpose even on an escalated rung -- a stall
#    re-POST is a second attempt on the rung already authorized, and
#    relabelling it would change which C2 rules can deny it.
req = request_for(
    initial_transport="PROXY_HTTP",
    cheap_transport="DIRECT_HTTP",
    purpose=AuthorizationPurpose.RETRY,
)
assert req.purpose is AuthorizationPurpose.RETRY, req.purpose
assert req.transport == "PROXY"

# 6. Unknown rung -> the pre-F18 fail-closed derivation from mode.
req = request_for()
assert req.transport == "PROXY", req.transport
assert req.estimated_bytes > 0
req = request_for(mode=ScrapeProfileMode.BROWSER)
assert req.transport == "BROWSER"
assert req.estimated_browser_seconds == 4 * 30

# 7. A transport this build has never heard of degrades to fail-closed
#    rather than crashing the whole dispatch pass.
req = request_for(initial_transport="CARRIER_PIGEON", cheap_transport="DIRECT_HTTP")
assert req.transport == "PROXY", req.transport

# 8. estimated_requests is the COALESCED count, never the match count.
req = request_for(
    initial_transport="PROXY_HTTP",
    cheap_transport="PROXY_HTTP",
    match_ids=[uuid.uuid4() for _ in range(9)],
    unique_physical_requests=2,
)
assert req.estimated_requests == 2, req.estimated_requests
cheap = request_for(
    initial_transport="PROXY_HTTP",
    cheap_transport="PROXY_HTTP",
    match_ids=[uuid.uuid4() for _ in range(9)],
)
assert cheap.estimated_requests == 9
assert req.estimated_cost_micro_units < cheap.estimated_cost_micro_units

# 9. A zero/absent coalesced count never reserves zero requests: the
#    request dimension counts fetches, and a batch always makes at least one.
req = request_for(
    initial_transport="DIRECT_HTTP",
    cheap_transport="DIRECT_HTTP",
    match_ids=[],
    unique_physical_requests=0,
)
assert req.estimated_requests == 1

print("OK")
'''


def _run_call_site(script: str) -> None:
    env = {**os.environ, **_CALL_SITE_ENV}
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip().endswith("OK")


def test_batch_authorization_request_reserves_by_the_known_first_rung() -> None:
    """The whole F18 call site in one subprocess: DIRECT reserves zero and
    is still authorized, paid rungs still reserve, a climb takes its own
    separate grant under its own purpose, a RETRY keeps its purpose, an
    unknown rung stays fail-closed, and `estimated_requests` is the
    coalesced physical count."""
    _run_call_site(_CALL_SITE_CHECK)
