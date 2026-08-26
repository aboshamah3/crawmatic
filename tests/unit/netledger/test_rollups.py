"""Unit tests for `app_shared.netledger.rollups` (EPA C6).

Pure-function correctness against raw fixture rows -- no database at all
(the aggregators take already-fetched rows by design, see that module's
own docstring). Three things this proves, matching the C6 plan's Step 1
failing-tests list:

1. rollup correctness vs. raw fixture data (grouping, latest-settlement
   reduction, the tenant fraction-of-settlement split);
2. the bounded top-N + "other" cardinality contract;
3. (scope-gating on the tenant read is proven separately, in
   `tests/unit/test_cost_rollups_scope_gating.py` -- watermark
   staleness -> CRITICAL is proven in `tests/unit/test_ops_metrics_rules.py`).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from app_shared.models.network_cost_rollups import (
    COST_ROLLUP_OTHER_DOMAIN,
    COST_ROLLUP_OTHER_METHOD,
    COST_ROLLUP_OTHER_PROFILE_VERSION,
    COST_ROLLUP_UNKNOWN_PROFILE_VERSION,
)
from app_shared.models.network_operations import FRACTION_SCALE
from app_shared.netledger.rollups import (
    RawAllocationRow,
    RawOperationRow,
    RawSettlementRow,
    aggregate_fleet_cost_buckets,
    aggregate_tenant_cost_buckets,
    cost_rollup_day_bounds,
    default_cost_rollup_target_date,
    latest_settlements_by_operation,
)

WS_A = uuid.uuid4()
WS_B = uuid.uuid4()


def _op(
    domain: str = "amazon.sa",
    method: str = "PROXY",
    profile_version: str | None = "3",
    estimated_cost_minor_units: int = 100,
    currency: str = "USD",
    op_id: uuid.UUID | None = None,
) -> RawOperationRow:
    """One fetched operation row.

    ``method`` is the TRANSPORT and ``profile_version`` is the domain
    playbook's version -- the two dimensions EPA Phase C F4 replaced the
    degenerate ones with (a constant HTTP verb, a near-unique budget
    decision tag). See `app_shared.models.network_cost_rollups`.
    """
    return RawOperationRow(
        network_request_id=op_id or uuid.uuid4(),
        domain=domain,
        method=method,
        profile_version=profile_version,
        estimated_cost_minor_units=estimated_cost_minor_units,
        currency=currency,
    )


class TestLatestSettlementsByOperation:
    def test_picks_the_highest_settlement_version(self) -> None:
        op_id = uuid.uuid4()
        settlements = [
            RawSettlementRow(op_id, settlement_version=1, reconciled_cost_minor_units=90, currency="USD"),
            RawSettlementRow(op_id, settlement_version=3, reconciled_cost_minor_units=95, currency="USD"),
            RawSettlementRow(op_id, settlement_version=2, reconciled_cost_minor_units=92, currency="USD"),
        ]
        latest = latest_settlements_by_operation(settlements)
        assert latest[op_id].settlement_version == 3
        assert latest[op_id].reconciled_cost_minor_units == 95

    def test_empty_input_yields_empty_map(self) -> None:
        assert latest_settlements_by_operation([]) == {}


class TestAggregateFleetCostBuckets:
    def test_groups_by_domain_method_profile_version_currency(self) -> None:
        ops = [
            _op(estimated_cost_minor_units=100),
            _op(estimated_cost_minor_units=200),
            _op(domain="noon.com", estimated_cost_minor_units=50),
        ]
        buckets = aggregate_fleet_cost_buckets(ops, [])
        by_domain = {b.domain: b for b in buckets}
        assert by_domain["amazon.sa"].operation_count == 2
        assert by_domain["amazon.sa"].estimated_cost_minor_units == 300
        assert by_domain["noon.com"].operation_count == 1
        assert by_domain["noon.com"].estimated_cost_minor_units == 50
        # No settlements at all -> every bucket's reconciled figure is
        # None (distinct from "reconciled to zero").
        assert all(b.reconciled_cost_minor_units is None for b in buckets)
        assert all(b.workspace_id is None for b in buckets)

    def test_none_profile_version_maps_to_the_unknown_sentinel(self) -> None:
        """A domain with no playbook row (the LEFT JOIN's NULL) still gets
        a bucket -- dropping uncertified domains would hide exactly the
        spend an operator most wants to see."""
        ops = [_op(profile_version=None)]
        buckets = aggregate_fleet_cost_buckets(ops, [])
        assert buckets[0].profile_version == COST_ROLLUP_UNKNOWN_PROFILE_VERSION

    def test_transport_separates_buckets_the_way_a_cost_report_needs(self) -> None:
        """EPA Phase C F4: the "method" dimension must SEPARATE spend.

        A browser navigation and a direct fetch differ by orders of
        magnitude in cost, and that is the split this dimension exists to
        show. Under the old mapping (the literal HTTP verb, `GET` on
        every scraping fetch this fleet makes) these three collapsed into
        one indistinguishable bucket.
        """
        ops = [
            _op(method="DIRECT", estimated_cost_minor_units=0),
            _op(method="PROXY", estimated_cost_minor_units=100),
            _op(method="BROWSER", estimated_cost_minor_units=900),
        ]
        buckets = aggregate_fleet_cost_buckets(ops, [])
        by_method = {b.method: b for b in buckets}
        assert set(by_method) == {"DIRECT", "PROXY", "BROWSER"}
        assert by_method["BROWSER"].estimated_cost_minor_units == 900
        assert by_method["PROXY"].estimated_cost_minor_units == 100

    def test_profile_version_groups_rather_than_shatters(self) -> None:
        """...and the "profile-version" dimension must not be unique.

        Twenty operations on one domain under one playbook version are
        ONE bucket. Under the old mapping (a per-decision counter tag)
        they were twenty, nineteen of which the top-N bound then swept
        into `__other__` -- a rollup that had aggregated nothing.
        """
        ops = [_op(profile_version="7", estimated_cost_minor_units=10) for _ in range(20)]
        buckets = aggregate_fleet_cost_buckets(ops, [], top_n=2)
        assert len(buckets) == 1
        assert buckets[0].profile_version == "7"
        assert buckets[0].operation_count == 20
        assert buckets[0].estimated_cost_minor_units == 200

    def test_reconciled_cost_uses_the_latest_settlement_only(self) -> None:
        op1 = _op(estimated_cost_minor_units=100)
        op2 = _op(estimated_cost_minor_units=200)
        settlements = [
            RawSettlementRow(op1.network_request_id, 1, 90, "USD"),
            RawSettlementRow(op1.network_request_id, 2, 95, "USD"),  # correction, latest
        ]
        buckets = aggregate_fleet_cost_buckets([op1, op2], settlements)
        assert len(buckets) == 1
        bucket = buckets[0]
        assert bucket.estimated_cost_minor_units == 300
        # Only op1 has a settlement (the latest, 95) -- op2 contributes
        # nothing to reconciled_cost_minor_units, but the bucket is still
        # "reconciled" (not None) because at least one operation is.
        assert bucket.reconciled_cost_minor_units == 95

    def test_settlement_currency_mismatch_is_not_folded_in(self) -> None:
        """A settlement in a different currency than the operation is
        never silently summed across currencies."""
        op = _op(currency="USD", estimated_cost_minor_units=100)
        settlements = [RawSettlementRow(op.network_request_id, 1, 90, "EUR")]
        buckets = aggregate_fleet_cost_buckets([op], settlements)
        assert buckets[0].reconciled_cost_minor_units is None

    def test_top_n_bounding_keeps_the_largest_and_collapses_the_rest(self) -> None:
        # Five distinct domains, distinct operation counts, one currency.
        ops: list[RawOperationRow] = []
        counts = {"a.com": 5, "b.com": 4, "c.com": 3, "d.com": 2, "e.com": 1}
        for domain, count in counts.items():
            for _ in range(count):
                ops.append(_op(domain=domain, estimated_cost_minor_units=10))

        buckets = aggregate_fleet_cost_buckets(ops, [], top_n=2)

        # Bounded: top 2 (a.com, b.com) kept as themselves, everything
        # else collapsed into exactly ONE "other" row for this currency.
        assert len(buckets) == 3
        by_domain = {b.domain: b for b in buckets}
        assert by_domain["a.com"].operation_count == 5
        assert by_domain["b.com"].operation_count == 4
        other = by_domain[COST_ROLLUP_OTHER_DOMAIN]
        assert other.method == COST_ROLLUP_OTHER_METHOD
        assert other.profile_version == COST_ROLLUP_OTHER_PROFILE_VERSION
        # c.com(3) + d.com(2) + e.com(1) = 6 operations, 60 minor units.
        assert other.operation_count == 6
        assert other.estimated_cost_minor_units == 60

    def test_top_n_bounding_collapses_per_currency_not_across_currencies(self) -> None:
        ops = [
            _op(domain="a.com", currency="USD", estimated_cost_minor_units=10),
            _op(domain="b.com", currency="USD", estimated_cost_minor_units=10),
            _op(domain="c.com", currency="USD", estimated_cost_minor_units=10),
            _op(domain="x.com", currency="AED", estimated_cost_minor_units=10),
            _op(domain="y.com", currency="AED", estimated_cost_minor_units=10),
        ]
        buckets = aggregate_fleet_cost_buckets(ops, [], top_n=1)
        currencies_with_other = {
            b.currency for b in buckets if b.domain == COST_ROLLUP_OTHER_DOMAIN
        }
        # Both currencies overflowed top_n=1 -> one "other" row EACH,
        # never merged into a single cross-currency row.
        assert currencies_with_other == {"USD", "AED"}

    def test_output_row_count_never_exceeds_top_n_plus_currency_count(self) -> None:
        ops = [_op(domain=f"d{i}.com", estimated_cost_minor_units=1) for i in range(50)]
        buckets = aggregate_fleet_cost_buckets(ops, [], top_n=5)
        # 5 kept + 1 "other" (single currency) = 6, never 50.
        assert len(buckets) == 6


class TestAggregateTenantCostBuckets:
    def test_splits_by_workspace_using_allocations(self) -> None:
        op = _op(estimated_cost_minor_units=300, currency="USD")
        allocations = [
            RawAllocationRow(op.network_request_id, WS_A, fraction_ppb=FRACTION_SCALE * 2 // 3, allocated_cost_minor_units=200, currency="USD"),
            RawAllocationRow(op.network_request_id, WS_B, fraction_ppb=FRACTION_SCALE // 3, allocated_cost_minor_units=100, currency="USD"),
        ]
        buckets = aggregate_tenant_cost_buckets([op], allocations, [])
        by_ws = {b.workspace_id: b for b in buckets}
        assert by_ws[WS_A].estimated_cost_minor_units == 200
        assert by_ws[WS_B].estimated_cost_minor_units == 100
        assert by_ws[WS_A].operation_count == 1
        assert by_ws[WS_A].reconciled_cost_minor_units is None

    def test_reconciled_share_uses_the_workspace_fraction_of_the_latest_settlement(self) -> None:
        op = _op(estimated_cost_minor_units=300, currency="USD")
        half = FRACTION_SCALE // 2
        allocations = [
            RawAllocationRow(op.network_request_id, WS_A, fraction_ppb=half, allocated_cost_minor_units=150, currency="USD"),
            RawAllocationRow(op.network_request_id, WS_B, fraction_ppb=half, allocated_cost_minor_units=150, currency="USD"),
        ]
        settlements = [RawSettlementRow(op.network_request_id, 1, reconciled_cost_minor_units=200, currency="USD")]
        buckets = aggregate_tenant_cost_buckets([op], allocations, settlements)
        by_ws = {b.workspace_id: b for b in buckets}
        # Each workspace holds exactly half the fraction -> half of 200.
        assert by_ws[WS_A].reconciled_cost_minor_units == 100
        assert by_ws[WS_B].reconciled_cost_minor_units == 100

    def test_top_n_is_applied_independently_per_workspace(self) -> None:
        """A whale tenant's many domains must never crowd a small
        tenant's own top-N bucket out of existence -- bounding is PER
        WORKSPACE, never across workspaces."""
        ops: list[RawOperationRow] = []
        allocations: list[RawAllocationRow] = []

        # Whale: 10 distinct domains for WS_A.
        for i in range(10):
            op = _op(domain=f"whale{i}.com", estimated_cost_minor_units=10)
            ops.append(op)
            allocations.append(
                RawAllocationRow(op.network_request_id, WS_A, fraction_ppb=FRACTION_SCALE, allocated_cost_minor_units=10, currency="USD")
            )

        # Small tenant: exactly 1 domain for WS_B.
        small_op = _op(domain="small.com", estimated_cost_minor_units=5)
        ops.append(small_op)
        allocations.append(
            RawAllocationRow(small_op.network_request_id, WS_B, fraction_ppb=FRACTION_SCALE, allocated_cost_minor_units=5, currency="USD")
        )

        buckets = aggregate_tenant_cost_buckets(ops, allocations, [], top_n=3)
        ws_b_buckets = [b for b in buckets if b.workspace_id == WS_B]
        assert len(ws_b_buckets) == 1
        assert ws_b_buckets[0].domain == "small.com"


class TestDateHelpers:
    def test_default_target_date_is_yesterday_utc(self) -> None:
        now = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
        assert default_cost_rollup_target_date(now) == date(2026, 8, 25)

    def test_day_bounds_are_half_open_utc(self) -> None:
        start, end = cost_rollup_day_bounds(date(2026, 8, 25))
        assert start == datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)
