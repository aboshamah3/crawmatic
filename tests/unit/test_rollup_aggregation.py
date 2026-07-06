"""Unit tests for `app_shared.maintenance.rollups` (SPEC-15 T021, US2,
contracts/daily-rollup.md, FR-009/010/011/012/013/014).

Pure, DB-independent:

* `aggregate_competitor_prices` — the pure per-(workspace, variant, day)
  aggregation: currency-mismatch (and failed/unpriced/non-comparable)
  exclusion from BOTH the min/avg/max aggregate AND the count (FR-011),
  correct min/avg/max, exact `Decimal` arithmetic (never float/NaN/Inf,
  FR-012), and the zero-comparable -> count-0/NULL-competitor-price case
  (FR-013).
* `default_target_date` — yesterday UTC, tz-aware enforcement.
* The three statement builders (`_driver_pairs_stmt`/
  `_day_observations_stmt`/`_client_state_stmt`) are compiled to
  `postgresql`-dialect SQL text (mirroring
  `tests/unit/test_partition_registry.py`'s `_compiled` helper) and
  asserted to carry the right predicates/params — never executed, no
  live DB.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql

from app_shared.maintenance.rollups import (
    CompetitorAggregate,
    ObservationRow,
    _client_state_stmt,
    _day_observations_stmt,
    _driver_pairs_stmt,
    aggregate_competitor_prices,
    default_target_date,
)


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


# --- aggregate_competitor_prices: correct min/avg/max/count -----------------


def test_correct_min_avg_max_and_count() -> None:
    rows = [
        ObservationRow(price=Decimal("10.0000"), currency="USD", success=True, comparable=True),
        ObservationRow(price=Decimal("12.0000"), currency="USD", success=True, comparable=True),
        ObservationRow(price=Decimal("14.0000"), currency="USD", success=True, comparable=True),
    ]
    result = aggregate_competitor_prices(rows, client_currency="USD")
    assert result == CompetitorAggregate(
        cheapest=Decimal("10.0000"),
        average=Decimal("12.0000"),
        highest=Decimal("14.0000"),
        comparable_count=3,
    )


def test_average_is_exact_decimal_quantized_to_money_scale() -> None:
    # 10 + 11 + 12 = 33 / 3 = 11 exactly -- pick a case with a
    # non-terminating quotient to prove quantization, not truncation:
    # 10.00 + 10.00 + 10.01 = 30.01 / 3 = 10.003333... -> 10.0033 (4dp,
    # ROUND_HALF_UP).
    rows = [
        ObservationRow(price=Decimal("10.0000"), currency="USD", success=True, comparable=True),
        ObservationRow(price=Decimal("10.0000"), currency="USD", success=True, comparable=True),
        ObservationRow(price=Decimal("10.0100"), currency="USD", success=True, comparable=True),
    ]
    result = aggregate_competitor_prices(rows, client_currency="USD")
    assert result.average == Decimal("10.0033")
    # Exact Decimal, never float.
    assert isinstance(result.average, Decimal)
    assert not isinstance(result.average, float)


# --- FR-011: currency mismatch (and non-comparable/failed/unpriced) --------
# --- excluded from BOTH the aggregate AND the count -------------------------


def test_currency_mismatch_excluded_from_aggregate_and_count() -> None:
    rows = [
        ObservationRow(price=Decimal("10.0000"), currency="USD", success=True, comparable=True),
        # Currency mismatch -- SPEC-09 already flips `comparable=False`,
        # but the currency filter is asserted independently here too.
        ObservationRow(price=Decimal("1.0000"), currency="EUR", success=True, comparable=False),
    ]
    result = aggregate_competitor_prices(rows, client_currency="USD")
    assert result.comparable_count == 1
    assert result.cheapest == Decimal("10.0000")
    assert result.average == Decimal("10.0000")
    assert result.highest == Decimal("10.0000")


def test_non_comparable_same_currency_row_still_excluded() -> None:
    """`comparable=False` excludes a row even when its currency matches --
    the persisted SPEC-09 flag is authoritative, not re-derived from
    currency alone."""
    rows = [
        ObservationRow(price=Decimal("10.0000"), currency="USD", success=True, comparable=True),
        ObservationRow(price=Decimal("999.0000"), currency="USD", success=True, comparable=False),
    ]
    result = aggregate_competitor_prices(rows, client_currency="USD")
    assert result.comparable_count == 1
    assert result.highest == Decimal("10.0000")


def test_failed_observation_excluded_even_if_priced() -> None:
    rows = [
        ObservationRow(price=Decimal("10.0000"), currency="USD", success=True, comparable=True),
        ObservationRow(price=Decimal("5.0000"), currency="USD", success=False, comparable=True),
    ]
    result = aggregate_competitor_prices(rows, client_currency="USD")
    assert result.comparable_count == 1
    assert result.cheapest == Decimal("10.0000")


def test_null_price_row_excluded() -> None:
    rows = [
        ObservationRow(price=Decimal("10.0000"), currency="USD", success=True, comparable=True),
        ObservationRow(price=None, currency=None, success=False, comparable=False),
    ]
    result = aggregate_competitor_prices(rows, client_currency="USD")
    assert result.comparable_count == 1


# --- FR-013: zero comparable competitors -> count 0, NULL prices -----------


def test_zero_comparable_rows_yields_count_zero_and_null_prices() -> None:
    rows = [
        ObservationRow(price=Decimal("1.0000"), currency="EUR", success=True, comparable=False),
    ]
    result = aggregate_competitor_prices(rows, client_currency="USD")
    assert result == CompetitorAggregate(
        cheapest=None, average=None, highest=None, comparable_count=0
    )


def test_empty_rows_yields_count_zero_and_null_prices() -> None:
    result = aggregate_competitor_prices([], client_currency="USD")
    assert result.comparable_count == 0
    assert result.cheapest is None
    assert result.average is None
    assert result.highest is None


# --- default_target_date: yesterday UTC, tz-aware enforcement --------------


def test_default_target_date_is_yesterday_utc() -> None:
    now = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
    assert default_target_date(now) == date(2026, 7, 5)


def test_default_target_date_crosses_month_boundary() -> None:
    now = datetime(2026, 8, 1, 0, 30, tzinfo=timezone.utc)
    assert default_target_date(now) == date(2026, 7, 31)


def test_default_target_date_rejects_naive_datetime() -> None:
    naive = datetime(2026, 7, 6, 10, 0)
    with pytest.raises(ValueError):
        default_target_date(naive)


# --- Statement builders: compiled predicate shape, no live DB --------------


def test_driver_pairs_stmt_filters_by_scraped_at_date() -> None:
    stmt = _driver_pairs_stmt(date(2026, 7, 5))
    sql = _compiled(stmt)
    assert "scraped_at::date" in sql
    assert "DISTINCT" in sql
    assert "price_observations" in sql
    assert stmt.compile().params["target_date"] == date(2026, 7, 5)


def test_day_observations_stmt_scoped_by_workspace_and_variant() -> None:
    workspace_id = uuid.uuid4()
    variant_id = uuid.uuid4()
    stmt = _day_observations_stmt(date(2026, 7, 5), workspace_id, variant_id)
    sql = _compiled(stmt)
    assert "workspace_id" in sql
    assert "product_variant_id" in sql
    assert "scraped_at::date" in sql
    params = stmt.compile().params
    assert params["workspace_id"] == workspace_id
    assert params["product_variant_id"] == variant_id


def test_client_state_stmt_scoped_by_workspace_and_variant() -> None:
    workspace_id = uuid.uuid4()
    variant_id = uuid.uuid4()
    stmt = _client_state_stmt(workspace_id, variant_id)
    sql = _compiled(stmt)
    assert "variant_price_states" in sql
    assert "workspace_id" in sql
    assert "product_variant_id" in sql
    params = stmt.compile().params
    assert params["workspace_id"] == workspace_id
    assert params["product_variant_id"] == variant_id
