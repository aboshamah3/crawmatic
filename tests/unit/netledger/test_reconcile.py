"""Tests for EPA C5: provider usage import and reconciliation.

Two test classes:

* the **offline** ones (always run) assert value-object validation and
  the pure policy helpers (variance math, currency policy) — no
  database required.
* the **live-Postgres** ones (skipped unless ``NETWORK_OPS_TEST_
  DATABASE_URL`` names a reachable scratch database — same dedicated
  variable and fixture shape as ``tests/unit/models/
  test_network_operations.py``, reused rather than reinvented since this
  module reconciles against exactly that ledger) prove the step-1
  scenarios the plan names explicitly:

  1. the preserved canary shape — 68 app-side operations vs 147
     provider-billed requests for the SAME host — produces a FAIL report
     with the gap listed in ``unexplained_operations`` (``tests/
     unit/netledger/test_reconcile.py::TestLiveReconciliation::
     test_canary_gap_produces_a_fail_report``);
  2. a synthetic MATCHED window — a parent operation + buffered
     sub-resource children (the C4 shape), transport-observed bytes
     agreeing with the provider's total within 2% — PASSES
     (``test_synthetic_matched_window_with_children_passes``);
  3. a late-arriving correction APPENDS a second settlement version
     without touching the first
     (``test_late_arrival_appends_new_settlement_version``).

``NETWORK_OPS_TEST_DATABASE_URL`` is read from ``os.environ`` only, never
from ``.env``/``Settings`` — see the precedent module's own docstring for
why.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app_shared.models.network_operations import (
    NetworkOperation,
    NetworkOperationSettlement,
    NetworkTransport,
    SettlementMethod,
)
from app_shared.models.provider_usage import ProviderUsageGranularity
from app_shared.netledger.reconcile import (
    DEFAULT_BYTE_VARIANCE_THRESHOLD_PCT,
    ProviderUsageRow,
    ProviderUsageSource,
    ReconciliationPolicyError,
    _single_currency,
    _variance_pct,
    import_provider_usage,
    reconcile_window,
    windows_pending_reconciliation,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Offline: value-object validation and pure policy helpers
# ---------------------------------------------------------------------------


class TestProviderUsageRowValidation:
    def test_rejects_float_total_bytes(self) -> None:
        with pytest.raises(TypeError):
            ProviderUsageRow(occurred_at=None, target_host="x", bytes_up=None,
                              bytes_down=None, total_bytes=1.5)  # type: ignore[arg-type]

    def test_rejects_negative_total_bytes(self) -> None:
        with pytest.raises(ValueError):
            ProviderUsageRow(occurred_at=None, target_host="x", bytes_up=None,
                              bytes_down=None, total_bytes=-1)

    def test_rejects_negative_request_count(self) -> None:
        with pytest.raises(ValueError):
            ProviderUsageRow(occurred_at=None, target_host="x", bytes_up=None,
                              bytes_down=None, total_bytes=100, request_count=-1)

    def test_rejects_naive_occurred_at(self) -> None:
        with pytest.raises(ValueError):
            ProviderUsageRow(occurred_at=datetime(2026, 8, 24, 20, 0, 0), target_host="x",
                              bytes_up=None, bytes_down=None, total_bytes=100)

    def test_accepts_a_well_formed_row(self) -> None:
        row = ProviderUsageRow(
            occurred_at=datetime(2026, 8, 24, 20, 0, 0, tzinfo=timezone.utc),
            target_host="amazon.sa", bytes_up=100, bytes_down=900, total_bytes=1000,
            request_count=1,
        )
        assert row.total_bytes == 1000


class TestProviderUsageSourceValidation:
    def _rows(self) -> list[ProviderUsageRow]:
        return [
            ProviderUsageRow(occurred_at=None, target_host="x", bytes_up=None,
                              bytes_down=None, total_bytes=100)
        ]

    def test_rejects_naive_window_bounds(self) -> None:
        with pytest.raises(ValueError):
            ProviderUsageSource(
                provider="dataimpulse",
                window_start=datetime(2026, 8, 24, 20, 0, 0),
                window_end=datetime(2026, 8, 25, 6, 0, 0, tzinfo=timezone.utc),
                rows=self._rows(), source_ref="test", raw_bytes=b"x",
            )

    def test_rejects_end_before_start(self) -> None:
        start = datetime(2026, 8, 25, 6, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 8, 24, 20, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            ProviderUsageSource(
                provider="dataimpulse", window_start=start, window_end=end,
                rows=self._rows(), source_ref="test", raw_bytes=b"x",
            )

    def test_cost_requires_currency(self) -> None:
        start = datetime(2026, 8, 24, 20, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 8, 25, 6, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            ProviderUsageSource(
                provider="dataimpulse", window_start=start, window_end=end,
                rows=self._rows(), source_ref="test", raw_bytes=b"x",
                total_cost_micro_units=100,
            )


class TestVariancePolicy:
    def test_zero_provider_and_zero_app_is_zero_variance(self) -> None:
        assert _variance_pct(0, 0) == 0.0

    def test_zero_provider_nonzero_app_is_undefined(self) -> None:
        assert _variance_pct(500, 0) is None

    def test_exact_match_is_zero_variance(self) -> None:
        assert _variance_pct(1000, 1000) == 0.0

    def test_variance_is_symmetric_in_magnitude(self) -> None:
        # 16,094,690 provider bytes vs a much smaller app figure (the
        # canary's own shape) is a large, well-defined variance.
        pct = _variance_pct(100_000, 16_094_690)
        assert pct is not None
        assert pct > 99.0

    def test_within_two_percent_passes_the_default_threshold(self) -> None:
        pct = _variance_pct(1_000_000, 1_015_000)
        assert pct is not None
        assert pct <= DEFAULT_BYTE_VARIANCE_THRESHOLD_PCT


class TestCurrencyPolicy:
    def test_single_currency_is_accepted(self) -> None:
        ops = [_fake_op(currency="USD"), _fake_op(currency="USD")]
        assert _single_currency(ops) == "USD"

    def test_no_currency_at_all_defaults_to_usd(self) -> None:
        assert _single_currency([_fake_op(currency=None)]) == "USD"

    def test_mixed_currency_raises_a_documented_policy_error(self) -> None:
        ops = [_fake_op(currency="USD"), _fake_op(currency="SAR")]
        with pytest.raises(ReconciliationPolicyError):
            _single_currency(ops)


class _FakeOp:
    """Minimal stand-in for a NetworkOperation carrying just `.currency`
    — `_single_currency` reads nothing else, so a real ORM row is
    unnecessary for this one pure-policy test."""

    def __init__(self, currency: str | None) -> None:
        self.currency = currency


def _fake_op(*, currency: str | None) -> _FakeOp:
    return _FakeOp(currency=currency)


# ---------------------------------------------------------------------------
# Live-Postgres: the step-1 scenarios
# ---------------------------------------------------------------------------

_DSN_ENV = "NETWORK_OPS_TEST_DATABASE_URL"


def _scratch_dsn() -> str | None:
    return os.environ.get(_DSN_ENV)


@pytest.fixture(scope="module")
def engine():  # type: ignore[no-untyped-def]
    dsn = _scratch_dsn()
    if not dsn:
        pytest.skip(f"{_DSN_ENV} unset — live reconciliation tests skipped")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url={dsn}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{proc.stdout}\n{proc.stderr}")
    eng = create_engine(dsn, future=True)
    # A clean slate every session run: this module's tests assert exact
    # counts (68 app rows, 147 provider rows, ...) that a leftover row
    # from a previous run against the same scratch database would
    # silently corrupt.
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "TRUNCATE network_operation_settlements, network_operation_allocations, "
                "network_operations, provider_usage_records CASCADE"
            )
        )
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session_scope(engine):  # type: ignore[no-untyped-def]
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    return scope


def _make_operation(
    *, provider: str, domain: str, bytes_compressed: int, closed_at: datetime,
    cost_micro_units: int | None = None, currency: str = "USD",
    provider_account: str | None = None, parent_operation_id: uuid.UUID | None = None,
) -> NetworkOperation:
    nrid = uuid.uuid4()
    return NetworkOperation(
        id=uuid.uuid4(),
        network_request_id=nrid,
        parent_operation_id=parent_operation_id,
        canonical_url_hash="sha256:" + nrid.hex,
        domain=domain,
        http_method="GET",
        provider=provider,
        provider_account=provider_account,
        transport=NetworkTransport.PROXY,
        created_at=closed_at - timedelta(seconds=1),
        closed_at=closed_at,
        bytes_compressed=bytes_compressed,
        bytes_decompressed=bytes_compressed * 4,
        response_status=200,
        estimated_cost_micro_units=cost_micro_units,
        currency=currency if cost_micro_units is not None else None,
    )


def _provider_source(
    *, provider: str, host: str, window_start: datetime, window_end: datetime,
    row_bytes: list[int], provider_account: str | None = None,
) -> ProviderUsageSource:
    rows = [
        ProviderUsageRow(
            occurred_at=window_start + timedelta(minutes=idx),
            target_host=host, bytes_up=b // 2, bytes_down=b - b // 2,
            total_bytes=b, request_count=1, http_status=200, success=True,
            raw={"i": idx, "bytes": b},
        )
        for idx, b in enumerate(row_bytes)
    ]
    raw_bytes = f"{provider}:{host}:{window_start.isoformat()}:{row_bytes}".encode()
    return ProviderUsageSource(
        provider=provider, provider_account=provider_account,
        window_start=window_start, window_end=window_end, rows=rows,
        source_ref=f"test:{host}:{len(row_bytes)}", raw_bytes=raw_bytes,
        granularity=ProviderUsageGranularity.PER_REQUEST,
    )


class TestLiveReconciliation:
    """Behaviours only a real Postgres (triggers, constraints) can prove."""

    # -- step 1, scenario 1: the canary shape -----------------------------

    def test_canary_gap_produces_a_fail_report(self, engine, session_scope) -> None:  # type: ignore[no-untyped-def]
        """68 app-side operations vs 147 provider-billed requests for the
        SAME host (the plan's own certified canary figures) must FAIL,
        with the gap named in `unexplained_operations` — never silently
        passed or silently dropped."""
        window_start = datetime(2026, 8, 24, 20, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 8, 25, 6, 0, 0, tzinfo=timezone.utc)
        host = "canary-gap.example"
        provider = "dataimpulse-canary-gap"

        with session_scope() as session:
            for i in range(68):
                session.add(
                    _make_operation(
                        provider=provider, domain=host, bytes_compressed=1_000,
                        closed_at=window_start + timedelta(minutes=i),
                        cost_micro_units=10,
                    )
                )
            session.commit()

        # 147 provider-billed requests, each carrying real bytes — the
        # canary's own multiplier: far more provider evidence than the
        # engine's ledger can account for at this host.
        source = _provider_source(
            provider=provider, host=host, window_start=window_start,
            window_end=window_end, row_bytes=[110_000] * 147,
        )
        with session_scope() as session:
            window = import_provider_usage(source, session_scope=session_scope)

        with session_scope() as session:
            report = reconcile_window(window, session_scope=session_scope)

        assert report.passed is False
        assert report.app_requests == 68
        assert report.provider_requests == 147
        assert len(report.unexplained_operations) >= 1
        assert any(
            u.kind == "BYTE_VARIANCE_EXCEEDS_THRESHOLD" for u in report.unexplained_operations
        )
        # No settlement should be written for a host the report itself
        # flags as unexplained — reconciliation must not guess a cost for
        # a gap it cannot explain.
        assert report.settlements_written == ()

    # -- step 1, scenario 2: the synthetic matched window ------------------

    def test_synthetic_matched_window_with_children_passes(self, engine, session_scope) -> None:  # type: ignore[no-untyped-def]
        """A parent navigation + buffered sub-resource children (the C4
        shape) whose SUMMED transport-observed bytes agree with the
        provider's total within 2% must PASS, and each operation gets a
        PRO_RATA_BYTES settlement."""
        window_start = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 8, 24, 13, 0, 0, tzinfo=timezone.utc)
        host = "matched-window.example"
        provider = "dataimpulse-matched"

        parent = _make_operation(
            provider=provider, domain=host, bytes_compressed=50_000,
            closed_at=window_start + timedelta(minutes=1),
        )
        children = [
            _make_operation(
                provider=provider, domain=host, bytes_compressed=5_000,
                closed_at=window_start + timedelta(minutes=1, seconds=i),
                parent_operation_id=parent.network_request_id,
            )
            for i in range(6)
        ]
        app_total_bytes = 50_000 + 6 * 5_000  # 80,000

        with session_scope() as session:
            session.add(parent)
            for child in children:
                session.add(child)
            session.commit()

        # Provider total within 2% of the app-side sum.
        provider_total = int(app_total_bytes * 1.01)
        source = _provider_source(
            provider=provider, host=host, window_start=window_start,
            window_end=window_end, row_bytes=[provider_total],
        )
        with session_scope() as session:
            window = import_provider_usage(source, session_scope=session_scope)

        window = replace(window, total_cost_micro_units=8_000, currency="USD")

        with session_scope() as session:
            report = reconcile_window(window, session_scope=session_scope)

        assert report.passed is True
        assert report.byte_variance_pct is not None
        assert report.byte_variance_pct <= DEFAULT_BYTE_VARIANCE_THRESHOLD_PCT
        assert report.app_requests == 7  # 1 parent + 6 children
        assert len(report.settlements_written) == 7
        assert report.unexplained_operations == ()

        with session_scope() as session:
            total_reconciled = session.execute(
                select(sa.func.sum(NetworkOperationSettlement.reconciled_cost_micro_units))
                .where(
                    NetworkOperationSettlement.operation_id.in_(
                        [parent.network_request_id]
                        + [c.network_request_id for c in children]
                    )
                )
            ).scalar_one()
        # Largest-remainder split: the parts sum EXACTLY to the window's
        # stated cost.
        assert total_reconciled == 8_000

    # -- step 1, scenario 3: late arrival appends, never edits -------------

    def test_late_arrival_appends_new_settlement_version(self, engine, session_scope) -> None:  # type: ignore[no-untyped-def]
        window_start = datetime(2026, 8, 24, 14, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 8, 24, 15, 0, 0, tzinfo=timezone.utc)
        host = "late-arrival.example"
        provider = "dataimpulse-late"

        op = _make_operation(
            provider=provider, domain=host, bytes_compressed=20_000,
            closed_at=window_start + timedelta(minutes=1), cost_micro_units=500,
        )
        with session_scope() as session:
            session.add(op)
            session.commit()

        first_source = _provider_source(
            provider=provider, host=host, window_start=window_start,
            window_end=window_end, row_bytes=[20_100],
        )
        with session_scope() as session:
            first_window = import_provider_usage(first_source, session_scope=session_scope)
        with session_scope() as session:
            first_report = reconcile_window(first_window, session_scope=session_scope)

        assert first_report.passed is True
        assert len(first_report.settlements_written) == 1

        with session_scope() as session:
            v1 = session.execute(
                select(NetworkOperationSettlement)
                .where(NetworkOperationSettlement.operation_id == op.network_request_id)
            ).scalar_one()
            v1_cost = v1.reconciled_cost_micro_units
            v1_method = v1.method
            v1_created_at = v1.created_at
            assert v1.settlement_version == 1
            assert v1_method == SettlementMethod.EXACT

        # A LATE, DIFFERENT export for the same window/host — a
        # correction with a different byte total, hence a different
        # source_hash and a genuinely new fact.
        second_source = _provider_source(
            provider=provider, host=host, window_start=window_start,
            window_end=window_end, row_bytes=[20_300],
        )
        with session_scope() as session:
            second_window = import_provider_usage(second_source, session_scope=session_scope)
        assert second_window.id != first_window.id

        with session_scope() as session:
            second_report = reconcile_window(second_window, session_scope=session_scope)

        assert second_report.passed is True
        assert len(second_report.settlements_written) == 1

        with session_scope() as session:
            rows = list(
                session.execute(
                    select(NetworkOperationSettlement)
                    .where(NetworkOperationSettlement.operation_id == op.network_request_id)
                    .order_by(NetworkOperationSettlement.settlement_version)
                ).scalars()
            )
        assert [r.settlement_version for r in rows] == [1, 2]
        assert rows[1].method == SettlementMethod.CORRECTION
        # The FIRST version is untouched — same cost, same method, same
        # created_at — proving this was an APPEND, not an edit.
        assert rows[0].reconciled_cost_micro_units == v1_cost
        assert rows[0].method == v1_method
        assert rows[0].created_at == v1_created_at

    # -- extra: a genuine re-run of the SAME window is idempotent ---------

    def test_rerunning_the_same_window_does_not_duplicate_settlements(
        self, engine, session_scope
    ) -> None:  # type: ignore[no-untyped-def]
        window_start = datetime(2026, 8, 24, 16, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 8, 24, 17, 0, 0, tzinfo=timezone.utc)
        host = "idempotent-rerun.example"
        provider = "dataimpulse-idempotent"

        op = _make_operation(
            provider=provider, domain=host, bytes_compressed=9_000,
            closed_at=window_start + timedelta(minutes=1), cost_micro_units=90,
        )
        with session_scope() as session:
            session.add(op)
            session.commit()

        source = _provider_source(
            provider=provider, host=host, window_start=window_start,
            window_end=window_end, row_bytes=[9_050],
        )
        with session_scope() as session:
            window = import_provider_usage(source, session_scope=session_scope)

        with session_scope() as session:
            first = reconcile_window(window, session_scope=session_scope)
        with session_scope() as session:
            second = reconcile_window(window, session_scope=session_scope)

        assert len(first.settlements_written) == 1
        assert second.settlements_written == ()
        assert second.settlements_skipped_idempotent == 1

        with session_scope() as session:
            count = session.execute(
                select(sa.func.count())
                .select_from(NetworkOperationSettlement)
                .where(NetworkOperationSettlement.operation_id == op.network_request_id)
            ).scalar_one()
        assert count == 1

    # -- extra: re-importing the identical file is a no-op ------------------

    def test_reimporting_the_identical_export_is_idempotent(self, engine, session_scope) -> None:  # type: ignore[no-untyped-def]
        window_start = datetime(2026, 8, 24, 18, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 8, 24, 19, 0, 0, tzinfo=timezone.utc)
        source = _provider_source(
            provider="dataimpulse-reimport", host="reimport.example",
            window_start=window_start, window_end=window_end, row_bytes=[1000, 2000],
        )
        with session_scope() as session:
            first = import_provider_usage(source, session_scope=session_scope)
        with session_scope() as session:
            second = import_provider_usage(source, session_scope=session_scope)

        assert first.already_imported is False
        assert second.already_imported is True
        assert first.id == second.id

        with session_scope() as session:
            from app_shared.models.provider_usage import ProviderUsageRecord

            count = session.execute(
                select(sa.func.count())
                .select_from(ProviderUsageRecord)
                .where(ProviderUsageRecord.window_id == first.id)
            ).scalar_one()
        assert count == 2

    # -- extra: the append-only trigger really is enforced ------------------

    def test_provider_usage_records_are_append_only(self, engine, session_scope) -> None:  # type: ignore[no-untyped-def]
        window_start = datetime(2026, 8, 24, 8, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 8, 24, 9, 0, 0, tzinfo=timezone.utc)
        source = _provider_source(
            provider="dataimpulse-append-only", host="append-only.example",
            window_start=window_start, window_end=window_end, row_bytes=[500],
        )
        with session_scope() as session:
            window = import_provider_usage(source, session_scope=session_scope)

        with engine.begin() as conn:
            with pytest.raises(sa.exc.DBAPIError) as excinfo:
                conn.execute(
                    sa.text(
                        "UPDATE provider_usage_records SET total_bytes = 1 "
                        "WHERE window_id = :wid"
                    ),
                    {"wid": window.id},
                )
        assert "append-only" in str(excinfo.value).lower()

    # -- extra: windows_pending_reconciliation drives the beat task --------

    def test_windows_pending_reconciliation_finds_the_imported_day(
        self, engine, session_scope
    ) -> None:  # type: ignore[no-untyped-def]
        window_start = datetime(2026, 8, 20, 3, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 8, 20, 4, 0, 0, tzinfo=timezone.utc)
        source = _provider_source(
            provider="dataimpulse-beat-task", host="beat-task.example",
            window_start=window_start, window_end=window_end, row_bytes=[100, 200],
        )
        with session_scope() as session:
            imported = import_provider_usage(source, session_scope=session_scope)

        with session_scope() as session:
            found = windows_pending_reconciliation(
                session, target_date=window_start.date(), provider="dataimpulse-beat-task"
            )
        assert len(found) == 1
        assert found[0].id == imported.id
        assert found[0].total_requests == 2
        assert found[0].total_bytes == 300
        # Window-level cost is not durably persisted (see the module
        # docstring) — reconstructed windows always carry None here.
        assert found[0].total_cost_micro_units is None
