"""`cost_rollup.reconciliation_missing` says WHY (2026-09-29, plan E7.4).

Production fired "no reconciled cost for any operation" every day and it
could mean two unrelated things: nothing was ever imported from the
provider (the actual state: provider_usage_records is empty), or evidence
was imported and matched nothing. They are fixed in different places, so
they are two rules. Neither is quieter than before: both stay CRITICAL.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app_shared.opsmetrics.rules import Severity, evaluate
from app_shared.opsmetrics.snapshot import CostRollupHealth, OpsSnapshot

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)


def _snap(windows: int | None) -> OpsSnapshot:
    return OpsSnapshot(
        collected_at=NOW,
        cost_rollup=CostRollupHealth(
            available=True,
            watermark_available=True,
            watermark_last_complete_date=date(2026, 9, 28),
            watermark_age_days=1,
            latest_rollup_date=date(2026, 9, 28),
            fleet_bucket_rows=3,
            estimated_cost_micro_units_by_currency={"USD": 1_000},
            reconciled_operation_count=0,
            total_operation_count=98_403,
            ledger_freshness_seconds=60.0,
            provider_evidence_windows=windows,
        ),
    )


def _ids(windows: int | None) -> dict:
    return {a.rule_id: a for a in evaluate(_snap(windows))}


def test_no_imported_evidence_is_its_own_critical() -> None:
    fired = _ids(0)
    assert fired["cost_rollup.provider_evidence_missing"].severity is Severity.CRITICAL
    assert "cost_rollup.reconciliation_missing" not in fired


def test_imported_but_unmatched_keeps_the_original_rule() -> None:
    fired = _ids(4)
    assert fired["cost_rollup.reconciliation_missing"].severity is Severity.CRITICAL
    assert fired["cost_rollup.reconciliation_missing"].observed["provider_evidence_windows"] == 4
    assert "cost_rollup.provider_evidence_missing" not in fired


def test_unknown_evidence_count_keeps_the_original_rule() -> None:
    assert "cost_rollup.reconciliation_missing" in _ids(None)
