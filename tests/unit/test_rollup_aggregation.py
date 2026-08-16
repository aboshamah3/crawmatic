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
* (M3) The **day-boundary + sargability** contract: the day predicate is
  a half-open UTC range on the RAW `scraped_at` partition key
  (`utc_day_bounds`), never a `::date` cast — the cast defeated both
  partition pruning and the `(workspace_id, scraped_at)` index. Both
  edges (00:00:00.000000 and 23:59:59.999999 UTC) and their outside
  neighbours are pinned, at the helper, at the statement, and end-to-end
  through `run_daily_rollup` over a fake session.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

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
    run_daily_rollup,
    utc_day_bounds,
)


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


#: A fixed non-UTC offset (Asia/Riyadh's UTC+03:00, the store's local
#: zone) used to prove day membership follows the UTC day, not a local
#: calendar day.
_TZ_PLUS_3 = timezone(timedelta(hours=3))


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
    assert "DISTINCT" in sql
    assert "price_observations" in sql
    params = stmt.compile().params
    assert params["day_start"] == datetime(2026, 7, 5, tzinfo=timezone.utc)
    assert params["day_end"] == datetime(2026, 7, 6, tzinfo=timezone.utc)


def test_day_observations_stmt_scoped_by_workspace_and_variant() -> None:
    workspace_id = uuid.uuid4()
    variant_id = uuid.uuid4()
    stmt = _day_observations_stmt(date(2026, 7, 5), workspace_id, variant_id)
    sql = _compiled(stmt)
    assert "workspace_id" in sql
    assert "product_variant_id" in sql
    params = stmt.compile().params
    assert params["workspace_id"] == workspace_id
    assert params["product_variant_id"] == variant_id
    assert params["day_start"] == datetime(2026, 7, 5, tzinfo=timezone.utc)
    assert params["day_end"] == datetime(2026, 7, 6, tzinfo=timezone.utc)


# --- Sargability + day-boundary semantics (M3) -----------------------------
#
# These are the tests that protect the `scraped_at::date = D` ->
# `scraped_at >= D AND scraped_at < D+1day` rewrite. The cast defeated
# BOTH partition pruning and the `(workspace_id, scraped_at)` index
# (measured on prod: 1,120 buffers / 3.8 ms to return 1 row, every
# partition seq-scanned; 57 buffers / 0.09 ms after). The rewrite is only
# behaviour-preserving if the range is a HALF-OPEN **UTC** day, so both
# properties are pinned here.


@pytest.mark.parametrize(
    "stmt_factory",
    [
        lambda d: _driver_pairs_stmt(d),
        lambda d: _day_observations_stmt(d, uuid.uuid4(), uuid.uuid4()),
    ],
    ids=["driver_pairs", "day_observations"],
)
def test_day_predicates_are_sargable_on_the_raw_partition_key(stmt_factory) -> None:
    """No function/cast may wrap `scraped_at` — that is what defeats
    partition pruning and the `(workspace_id, scraped_at)` index."""
    sql = _compiled(stmt_factory(date(2026, 7, 5)))
    assert "scraped_at::date" not in sql
    assert "CAST(scraped_at" not in sql.upper()
    assert "scraped_at >=" in sql
    assert "scraped_at <" in sql


def test_utc_day_bounds_is_a_half_open_utc_day() -> None:
    start, end = utc_day_bounds(date(2026, 7, 5))
    assert start == datetime(2026, 7, 5, 0, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
    assert start.tzinfo is timezone.utc and end.tzinfo is timezone.utc


@pytest.mark.parametrize(
    ("moment", "included"),
    [
        # Inside the target UTC day, at both extremes.
        (datetime(2026, 7, 5, 0, 0, 0, 0, tzinfo=timezone.utc), True),
        (datetime(2026, 7, 5, 23, 59, 59, 999999, tzinfo=timezone.utc), True),
        # Just outside each edge.
        (datetime(2026, 7, 4, 23, 59, 59, 999999, tzinfo=timezone.utc), False),
        (datetime(2026, 7, 6, 0, 0, 0, 0, tzinfo=timezone.utc), False),
        # Same instants expressed in a non-UTC offset: membership must
        # follow the UTC day, never a local-clock day. 03:00+03:00 is
        # 00:00 UTC (in); 02:59:59+03:00 is 2026-07-04 23:59:59 UTC (out).
        (datetime(2026, 7, 5, 3, 0, 0, tzinfo=_TZ_PLUS_3), True),
        (datetime(2026, 7, 5, 2, 59, 59, tzinfo=_TZ_PLUS_3), False),
        (datetime(2026, 7, 6, 2, 59, 59, tzinfo=_TZ_PLUS_3), True),
        (datetime(2026, 7, 6, 3, 0, 0, tzinfo=_TZ_PLUS_3), False),
    ],
)
def test_day_bounds_edges(moment: datetime, included: bool) -> None:
    start, end = utc_day_bounds(date(2026, 7, 5))
    assert (start <= moment < end) is included


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


# --- run_daily_rollup end-to-end over a fake session (M3) ------------------
#
# The live rollup test (`tests/integration/test_daily_rollup_live.py`)
# needs a real Postgres and skips in this environment, so the
# day-boundary rewrite is additionally pinned here by driving the REAL
# `run_daily_rollup` over a fake session that applies the statements'
# OWN bind parameters (`day_start`/`day_end`) to a set of timestamped
# observations. Same rows in -> same rollup rows out: an observation at
# 00:00:00.000000 and one at 23:59:59.999999 of the target UTC day are
# both aggregated; the instants either side of those edges are not.


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """Minimal `Session.execute` stand-in for `run_daily_rollup`.

    Dispatches on the rendered SQL and — crucially — filters the seeded
    observations using the statement's own `day_start`/`day_end` binds,
    so the test exercises the real predicate rather than a restatement
    of it.
    """

    def __init__(self, observations: list, states: dict) -> None:
        self.observations = observations
        self.states = states
        self.upserts: list[dict] = []

    def execute(self, stmt):
        sql = str(stmt)
        if "DISTINCT" in sql and "price_observations" in sql:
            params = stmt.compile().params
            rows = self._in_day(params["day_start"], params["day_end"])
            seen = []
            for obs in rows:
                key = (obs.workspace_id, obs.product_variant_id, obs.product_id)
                if key not in seen:
                    seen.append(key)
            return _FakeResult(
                [
                    SimpleNamespace(
                        workspace_id=ws, product_variant_id=variant, product_id=product
                    )
                    for ws, variant, product in seen
                ]
            )
        if "variant_price_states" in sql:
            params = stmt.compile().params
            state = self.states.get(
                (params["workspace_id"], params["product_variant_id"])
            )
            return _FakeResult([state] if state is not None else [])
        if "price_observations" in sql:
            params = stmt.compile().params
            return _FakeResult(
                [
                    obs
                    for obs in self._in_day(params["day_start"], params["day_end"])
                    if obs.workspace_id == params["workspace_id"]
                    and obs.product_variant_id == params["product_variant_id"]
                ]
            )
        self.upserts.append(dict(stmt.compile().params))
        return _FakeResult([])

    def _in_day(self, day_start: datetime, day_end: datetime) -> list:
        return [obs for obs in self.observations if day_start <= obs.scraped_at < day_end]


def _observation(workspace_id, variant_id, product_id, scraped_at, price):
    return SimpleNamespace(
        workspace_id=workspace_id,
        product_variant_id=variant_id,
        product_id=product_id,
        scraped_at=scraped_at,
        price=Decimal(price),
        currency="SAR",
        success=True,
        comparable=True,
    )


def test_run_daily_rollup_includes_both_day_edges_and_excludes_neighbours() -> None:
    workspace_id = uuid.uuid4()
    variant_id = uuid.uuid4()
    product_id = uuid.uuid4()
    target = date(2026, 7, 5)

    observations = [
        # First and last representable instants of the target UTC day.
        _observation(
            workspace_id,
            variant_id,
            product_id,
            datetime(2026, 7, 5, 0, 0, 0, 0, tzinfo=timezone.utc),
            "100.0000",
        ),
        _observation(
            workspace_id,
            variant_id,
            product_id,
            datetime(2026, 7, 5, 23, 59, 59, 999999, tzinfo=timezone.utc),
            "300.0000",
        ),
        # Just outside each edge — must not leak into the day.
        _observation(
            workspace_id,
            variant_id,
            product_id,
            datetime(2026, 7, 4, 23, 59, 59, 999999, tzinfo=timezone.utc),
            "1.0000",
        ),
        _observation(
            workspace_id,
            variant_id,
            product_id,
            datetime(2026, 7, 6, 0, 0, 0, 0, tzinfo=timezone.utc),
            "9999.0000",
        ),
    ]
    states = {
        (workspace_id, variant_id): SimpleNamespace(
            client_price=Decimal("250.0000"), currency="SAR", latest_alert_type=None
        )
    }
    session = _FakeSession(observations, states)

    report = run_daily_rollup(session, target_date=target)

    assert report.rollups_upserted == 1
    assert report.variants_skipped_no_state == []
    (upsert,) = session.upserts
    assert upsert["date"] == target
    # Only the two in-day observations: the neighbours would have moved
    # min to 1.0000 and max to 9999.0000.
    assert upsert["cheapest_competitor_price"] == Decimal("100.0000")
    assert upsert["highest_competitor_price"] == Decimal("300.0000")
    assert upsert["average_competitor_price"] == Decimal("200.0000")
    assert upsert["comparable_competitor_count"] == 2


def test_run_daily_rollup_driver_scan_ignores_out_of_day_variants() -> None:
    """A variant observed ONLY outside the target day yields no rollup row
    at all (the driver scan's own boundary)."""
    workspace_id = uuid.uuid4()
    in_day_variant = uuid.uuid4()
    out_of_day_variant = uuid.uuid4()
    product_id = uuid.uuid4()

    observations = [
        _observation(
            workspace_id,
            in_day_variant,
            product_id,
            datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc),
            "50.0000",
        ),
        _observation(
            workspace_id,
            out_of_day_variant,
            product_id,
            datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc),
            "60.0000",
        ),
    ]
    states = {
        (workspace_id, in_day_variant): SimpleNamespace(
            client_price=Decimal("55.0000"), currency="SAR", latest_alert_type=None
        ),
        (workspace_id, out_of_day_variant): SimpleNamespace(
            client_price=Decimal("65.0000"), currency="SAR", latest_alert_type=None
        ),
    }
    session = _FakeSession(observations, states)

    report = run_daily_rollup(session, target_date=date(2026, 7, 5))

    assert report.rollups_upserted == 1
    (upsert,) = session.upserts
    assert upsert["product_variant_id"] == in_day_variant


# --- dry_run=True: SPEC-15 Task 1.4 -- counts computed, no write sent -------
#
# `scripts/backfill_daily_rollups.py`'s dry-run needs a caller-session
# that genuinely never sends a write statement (so a transaction-level
# `SET TRANSACTION READ ONLY` guard holds for the whole call) while still
# reporting accurate would-be counts. This proves that contract directly
# against `run_daily_rollup`, independent of the backfill script.


def test_run_daily_rollup_dry_run_counts_without_ever_executing_upsert() -> None:
    workspace_id = uuid.uuid4()
    variant_id = uuid.uuid4()
    product_id = uuid.uuid4()
    target = date(2026, 7, 5)

    observations = [
        _observation(
            workspace_id, variant_id, product_id,
            datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc), "10.0000",
        ),
    ]
    states = {
        (workspace_id, variant_id): SimpleNamespace(
            client_price=Decimal("15.0000"), currency="SAR", latest_alert_type=None
        )
    }
    session = _FakeSession(observations, states)

    report = run_daily_rollup(session, target_date=target, dry_run=True)

    # The count reflects the real aggregation (same as dry_run=False would
    # report for identical input)...
    assert report.rollups_upserted == 1
    assert report.variants_skipped_no_state == []
    # ...but NO upsert statement was ever sent to the session -- the only
    # statements executed are the three reads (driver scan, client
    # state, day observations).
    assert session.upserts == []


def test_run_daily_rollup_dry_run_still_skips_variants_with_no_state() -> None:
    """`variants_skipped_no_state` bookkeeping is unaffected by `dry_run`
    -- it happens before the write branch is ever reached."""
    workspace_id = uuid.uuid4()
    variant_id = uuid.uuid4()
    product_id = uuid.uuid4()
    target = date(2026, 7, 5)

    observations = [
        _observation(
            workspace_id, variant_id, product_id,
            datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc), "10.0000",
        ),
    ]
    session = _FakeSession(observations, states={})  # no variant_price_states row

    report = run_daily_rollup(session, target_date=target, dry_run=True)

    assert report.rollups_upserted == 0
    assert report.variants_skipped_no_state == [str(variant_id)]
    assert session.upserts == []
