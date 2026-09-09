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
* (EPA C7, F12) The **latest ELIGIBLE observation per `(workspace,
  variant, match, day)`** collapse: a competitor observed ten times that
  day counts once, at its latest usable price, so `comparable_count`
  counts competitors and the average is not weighted by refresh
  frequency.
* The set-based batch statement (`rollup_sql.rollup_batch_stmt`, which
  replaced the three per-pair statement builders) is compiled to
  `postgresql`-dialect SQL text (mirroring
  `tests/unit/test_partition_registry.py`'s `_compiled` helper) and
  asserted to carry the right predicates/params — never executed, no
  live DB. Its full render contract is
  `tests/unit/test_rollup_sql_render.py`.
* (EPA C7) `run_daily_rollup`'s **batching/checkpoint loop** over a fake
  session: keyset cursor carry-forward, per-batch commit, checkpoint
  written in the batch's own transaction, resume, restart, and the
  bounded-invocation stops.
* (M3) The **day-boundary + sargability** contract: the day predicate is
  a half-open UTC range on the RAW `scraped_at` partition key
  (`utc_day_bounds`), never a `::date` cast — the cast defeated both
  partition pruning and the `(workspace_id, scraped_at)` index. Both
  edges (00:00:00.000000 and 23:59:59.999999 UTC) and their outside
  neighbours are pinned, at the helper and at the statement. The
  behavioural end-to-end proof now needs a real Postgres and lives in
  `tests/integration/test_rollup_set_based.py`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app_shared.maintenance.rollup_sql import MIN_UUID, rollup_batch_stmt
from app_shared.maintenance.rollups import (
    CompetitorAggregate,
    ObservationRow,
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


# --- Latest ELIGIBLE observation per match (EPA C7, F12) -------------------
#
# The semantic change: a competitor observed ten times in a day counts
# ONCE, at its latest usable price. Before C7 the aggregate was weighted
# by how often each competitor happened to be refreshed, which is a
# property of the scrape schedule and not of the market.


def _obs(price, *, match_id=None, at=None, currency="USD", success=True, comparable=True):
    return ObservationRow(
        price=None if price is None else Decimal(price),
        currency=currency,
        success=success,
        comparable=comparable,
        match_id=match_id,
        scraped_at=at,
    )


def _at(hour: int) -> datetime:
    return datetime(2026, 7, 5, hour, tzinfo=timezone.utc)


def test_ten_observations_of_one_competitor_count_once() -> None:
    """The 10-vs-1 skew case, at the pure-function level."""
    noisy = uuid.uuid4()
    quiet = uuid.uuid4()
    rows = [_obs("10.0000", match_id=noisy, at=_at(hour)) for hour in range(10)]
    rows.append(_obs("30.0000", match_id=quiet, at=_at(11)))

    result = aggregate_competitor_prices(rows, client_currency="USD")

    assert result.comparable_count == 2, "count must be competitors, not readings"
    assert result.average == Decimal("20.0000"), (
        "ten readings of the cheap competitor must not drag the average to 11.82"
    )
    assert result.cheapest == Decimal("10.0000")
    assert result.highest == Decimal("30.0000")


def test_the_surviving_reading_per_match_is_the_latest_one() -> None:
    match = uuid.uuid4()
    rows = [
        _obs("10.0000", match_id=match, at=_at(1)),
        _obs("99.0000", match_id=match, at=_at(9)),
        _obs("50.0000", match_id=match, at=_at(5)),
    ]

    result = aggregate_competitor_prices(rows, client_currency="USD")

    assert result.comparable_count == 1
    assert result.cheapest == result.highest == Decimal("99.0000")


def test_latest_reading_that_is_ineligible_does_not_hide_the_earlier_usable_one() -> None:
    """'Latest ELIGIBLE', not 'latest, then check'. A failed scrape at
    23:50 must not remove the competitor from the whole day."""
    match = uuid.uuid4()
    rows = [
        _obs("40.0000", match_id=match, at=_at(8)),
        _obs(None, match_id=match, at=_at(23), success=False),
    ]

    result = aggregate_competitor_prices(rows, client_currency="USD")

    assert result.comparable_count == 1
    assert result.cheapest == Decimal("40.0000")


def test_rows_without_a_match_id_are_never_merged_together() -> None:
    """Two rows that cannot be shown to be the same competitor must not
    be assumed to be one — that would silently under-count."""
    rows = [_obs("10.0000", at=_at(1)), _obs("20.0000", at=_at(2))]

    result = aggregate_competitor_prices(rows, client_currency="USD")

    assert result.comparable_count == 2
    assert result.average == Decimal("15.0000")


def test_a_row_with_no_scraped_at_never_outranks_one_that_has_it() -> None:
    match = uuid.uuid4()
    rows = [
        _obs("10.0000", match_id=match, at=_at(3)),
        _obs("99.0000", match_id=match, at=None),
    ]

    result = aggregate_competitor_prices(rows, client_currency="USD")

    assert result.comparable_count == 1
    assert result.cheapest == Decimal("10.0000")


def test_collapse_happens_per_match_across_currencies_correctly() -> None:
    """A competitor's latest reading being in the wrong currency is an
    eligibility failure like any other: its previous same-currency
    reading survives, and the mismatched one is never counted."""
    match = uuid.uuid4()
    rows = [
        _obs("25.0000", match_id=match, at=_at(4)),
        _obs("25.0000", match_id=match, at=_at(20), currency="SAR", comparable=False),
    ]

    result = aggregate_competitor_prices(rows, client_currency="USD")

    assert result.comparable_count == 1
    assert result.average == Decimal("25.0000")


# --- The batch statement: compiled predicate shape, no live DB -------------
#
# The full render contract lives in `tests/unit/test_rollup_sql_render.py`
# (EPA C7). What is kept here is the day-boundary/sargability property
# these tests were originally written for, re-pointed at the statement
# that now carries it.


def test_batch_stmt_binds_the_half_open_utc_day() -> None:
    target = date(2026, 7, 5)
    stmt = rollup_batch_stmt(target, *utc_day_bounds(target))
    sql = _compiled(stmt)
    assert "price_observations" in sql
    assert "variant_price_states" in sql
    params = stmt.compile().params
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
# properties are pinned here. EPA C7 moved the predicate into the
# set-based batch statement; it did not change it.


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry_run"])
def test_day_predicates_are_sargable_on_the_raw_partition_key(dry_run: bool) -> None:
    """No function/cast may wrap `scraped_at` — that is what defeats
    partition pruning and the `(workspace_id, scraped_at)` index."""
    target = date(2026, 7, 5)
    sql = _compiled(rollup_batch_stmt(target, *utc_day_bounds(target), dry_run=dry_run))
    assert "scraped_at::date" not in sql
    assert "CAST(SCRAPED_AT" not in sql.upper()
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


# --- run_daily_rollup's batching/checkpoint loop over a fake session -------
#
# What is exercised here is the DRIVER, not the SQL: how many batches it
# issues, what cursor it carries forward, what it writes to
# `rollup_completion`, when it declares the day complete, and that a dry
# run never sends a write. The statement's own semantics need a real
# Postgres and are proven in
# `tests/integration/test_rollup_set_based.py`.


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else None

    def all(self) -> list:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _BatchingSession:
    """`Session.execute` stand-in that serves the batch statement from a
    scripted list of per-batch results and records everything else."""

    def __init__(
        self,
        batches: list[dict],
        *,
        completion_table: bool = True,
        existing: dict | None = None,
    ) -> None:
        self.batches = list(batches)
        self.completion_table = completion_table
        self.existing = existing
        self.batch_params: list[dict] = []
        self.checkpoints: list[dict] = []
        self.cleared = 0
        self.commits = 0
        self.statements: list[str] = []

    def execute(self, stmt):
        sql = str(stmt)
        self.statements.append(sql)
        params = stmt.compile().params
        if "to_regclass" in sql:
            return _FakeResult([self.completion_table])
        if sql.strip().startswith("WITH"):
            self.batch_params.append(dict(params))
            row = self.batches.pop(0) if self.batches else _empty_batch()
            return _FakeResult([SimpleNamespace(**row)])
        if "INSERT INTO rollup_completion" in sql:
            if "complete = FALSE" in sql:
                self.cleared += 1
            else:
                self.checkpoints.append(dict(params))
            return _FakeResult([])
        if "FROM rollup_completion" in sql:
            return _FakeResult(
                [SimpleNamespace(**self.existing)] if self.existing else []
            )
        raise AssertionError(f"unexpected statement: {sql[:120]}")

    def commit(self) -> None:
        self.commits += 1


def _empty_batch() -> dict:
    return {
        "driver_rows": 0,
        "rollups_upserted": 0,
        "variants_skipped_no_state": None,
        "last_workspace_id": None,
        "last_product_variant_id": None,
    }


def _batch(driver_rows: int, upserted: int, *, ws=None, variant=None, skipped=None) -> dict:
    return {
        "driver_rows": driver_rows,
        "rollups_upserted": upserted,
        "variants_skipped_no_state": skipped,
        "last_workspace_id": ws,
        "last_product_variant_id": variant,
    }


def test_a_short_batch_ends_the_day_in_one_statement() -> None:
    ws, variant = uuid.uuid4(), uuid.uuid4()
    session = _BatchingSession([_batch(3, 3, ws=ws, variant=variant)])

    report = run_daily_rollup(
        session, target_date=date(2026, 7, 5), batch_limit=5
    )

    assert report.batches == 1
    assert report.rollups_upserted == 3
    assert report.complete is True
    assert session.checkpoints[-1]["complete"] is True


def test_a_full_batch_is_followed_by_another_carrying_the_keyset_cursor() -> None:
    ws1, v1 = uuid.uuid4(), uuid.uuid4()
    ws2, v2 = uuid.uuid4(), uuid.uuid4()
    session = _BatchingSession(
        [_batch(2, 2, ws=ws1, variant=v1), _batch(1, 1, ws=ws2, variant=v2)]
    )

    report = run_daily_rollup(session, target_date=date(2026, 7, 5), batch_limit=2)

    assert report.batches == 2
    assert report.rollups_upserted == 3
    assert report.complete is True
    # Batch 1 starts at the sentinel; batch 2 resumes from batch 1's max.
    assert session.batch_params[0]["last_workspace_id"] == MIN_UUID
    assert session.batch_params[1]["last_workspace_id"] == ws1
    assert session.batch_params[1]["last_product_variant_id"] == v1


def test_every_batch_commits_so_a_kill_keeps_finished_work() -> None:
    ws, variant = uuid.uuid4(), uuid.uuid4()
    session = _BatchingSession(
        [_batch(2, 2, ws=ws, variant=variant), _batch(0, 0)]
    )

    run_daily_rollup(session, target_date=date(2026, 7, 5), batch_limit=2)

    assert session.commits == 2
    assert len(session.checkpoints) == 2


def test_checkpoint_is_written_before_the_commit_not_after() -> None:
    """The whole no-double-count argument: the rows and the cursor
    advance are in ONE transaction, so a crash loses both or neither."""
    ws, variant = uuid.uuid4(), uuid.uuid4()
    session = _BatchingSession([_batch(1, 1, ws=ws, variant=variant)])

    run_daily_rollup(session, target_date=date(2026, 7, 5), batch_limit=5)

    order = [
        "batch" if s.strip().startswith("WITH") else "checkpoint"
        for s in session.statements
        if s.strip().startswith("WITH") or "INSERT INTO rollup_completion" in s
    ]
    assert order == ["batch", "checkpoint"]
    assert session.commits == 1


def test_max_batches_stops_the_run_and_reports_it_incomplete() -> None:
    ws, variant = uuid.uuid4(), uuid.uuid4()
    session = _BatchingSession([_batch(2, 2, ws=ws, variant=variant)])

    report = run_daily_rollup(
        session, target_date=date(2026, 7, 5), batch_limit=2, max_batches=1
    )

    assert report.complete is False
    assert report.batches == 1
    assert session.checkpoints[-1]["complete"] is False
    assert session.checkpoints[-1]["last_workspace_id"] == ws


def test_a_past_deadline_stops_the_run_between_batches() -> None:
    ws, variant = uuid.uuid4(), uuid.uuid4()
    session = _BatchingSession([_batch(2, 2, ws=ws, variant=variant)])

    report = run_daily_rollup(
        session,
        target_date=date(2026, 7, 5),
        batch_limit=2,
        deadline=datetime(2000, 1, 1, tzinfo=timezone.utc),
    )

    assert report.complete is False
    assert report.batches == 1


def test_a_run_resumes_from_the_stored_cursor() -> None:
    ws, variant = uuid.uuid4(), uuid.uuid4()
    session = _BatchingSession(
        [_batch(1, 1, ws=uuid.uuid4(), variant=uuid.uuid4())],
        existing={
            "date": date(2026, 7, 5),
            "last_key_workspace_id": ws,
            "last_key_variant_id": variant,
            "complete": False,
            "updated_at": None,
        },
    )

    report = run_daily_rollup(session, target_date=date(2026, 7, 5), batch_limit=5)

    assert report.resumed_from == (str(ws), str(variant))
    assert session.batch_params[0]["last_workspace_id"] == ws
    assert session.batch_params[0]["last_product_variant_id"] == variant


def test_a_day_already_marked_complete_is_a_no_op() -> None:
    session = _BatchingSession(
        [],
        existing={
            "date": date(2026, 7, 5),
            "last_key_workspace_id": uuid.uuid4(),
            "last_key_variant_id": uuid.uuid4(),
            "complete": True,
            "updated_at": None,
        },
    )

    report = run_daily_rollup(session, target_date=date(2026, 7, 5))

    assert report.complete is True
    assert report.batches == 0
    assert report.rollups_upserted == 0
    assert session.batch_params == []


def test_restart_clears_the_checkpoint_and_walks_the_day_again() -> None:
    """What `recompute_window` does: a deliberate repair must re-derive a
    day even when its checkpoint says complete."""
    session = _BatchingSession(
        [_batch(1, 1, ws=uuid.uuid4(), variant=uuid.uuid4())],
        existing={
            "date": date(2026, 7, 5),
            "last_key_workspace_id": uuid.uuid4(),
            "last_key_variant_id": uuid.uuid4(),
            "complete": True,
            "updated_at": None,
        },
    )

    report = run_daily_rollup(
        session, target_date=date(2026, 7, 5), batch_limit=5, restart=True
    )

    assert session.cleared == 1
    assert report.batches == 1
    assert session.batch_params[0]["last_workspace_id"] == MIN_UUID


def test_skipped_variants_are_reported_from_the_statement() -> None:
    variant = str(uuid.uuid4())
    session = _BatchingSession(
        [_batch(2, 1, ws=uuid.uuid4(), variant=uuid.uuid4(), skipped=[variant])]
    )

    report = run_daily_rollup(session, target_date=date(2026, 7, 5), batch_limit=5)

    assert report.variants_skipped_no_state == [variant]


def test_an_absent_checkpoint_table_degrades_to_a_non_resumable_run() -> None:
    session = _BatchingSession(
        [_batch(1, 1, ws=uuid.uuid4(), variant=uuid.uuid4())], completion_table=False
    )

    report = run_daily_rollup(session, target_date=date(2026, 7, 5), batch_limit=5)

    assert report.checkpoint_available is False
    assert report.complete is True
    assert session.checkpoints == []


# --- dry_run=True: SPEC-15 Task 1.4 -- counts computed, no write sent -------
#
# `scripts/backfill_daily_rollups.py`'s dry-run needs a caller-session
# that genuinely never sends a write statement (so a transaction-level
# `SET TRANSACTION READ ONLY` guard holds for the whole call) while still
# reporting accurate would-be counts.


def test_run_daily_rollup_dry_run_counts_without_ever_writing() -> None:
    session = _BatchingSession(
        [_batch(1, 1, ws=uuid.uuid4(), variant=uuid.uuid4())]
    )

    report = run_daily_rollup(
        session, target_date=date(2026, 7, 5), batch_limit=5, dry_run=True
    )

    assert report.rollups_upserted == 1
    assert session.checkpoints == []
    assert session.cleared == 0
    assert session.commits == 0
    for sql in session.statements:
        assert "INSERT" not in sql, "a dry run sent a write statement"


def test_run_daily_rollup_dry_run_never_consults_the_checkpoint() -> None:
    """"What would a full run write" is the question — a `complete` row
    must not turn a dry run into a silent no-op reporting zero."""
    session = _BatchingSession(
        [_batch(1, 1, ws=uuid.uuid4(), variant=uuid.uuid4())],
        existing={
            "date": date(2026, 7, 5),
            "last_key_workspace_id": uuid.uuid4(),
            "last_key_variant_id": uuid.uuid4(),
            "complete": True,
            "updated_at": None,
        },
    )

    report = run_daily_rollup(
        session, target_date=date(2026, 7, 5), batch_limit=5, dry_run=True
    )

    assert report.rollups_upserted == 1
    assert report.batches == 1
    assert session.batch_params[0]["last_workspace_id"] == MIN_UUID


def test_run_daily_rollup_rejects_a_non_positive_batch_limit() -> None:
    with pytest.raises(ValueError):
        run_daily_rollup(
            _BatchingSession([]), target_date=date(2026, 7, 5), batch_limit=0
        )
