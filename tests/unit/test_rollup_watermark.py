"""Durable rollup watermark: crash safety, bounded backfill, recompute.

EPA W5.5-L2 Item A. Covers `app_shared.maintenance.rollup_watermark` and
the three new entry points in `app_shared.maintenance.rollups`
(`plan_backfill_days`, `run_rollup_catchup`, `recompute_window`).

Pure / DB-independent throughout: a fake session dispatches on the
rendered SQL (the `tests/unit/test_rollup_aggregation.py::_FakeSession`
precedent) and keeps an in-memory `variant_price_daily_rollups` keyed on
the real upsert arbiter `(workspace_id, product_variant_id, date)`, so
"idempotent" and "no double count" are observed properties of the
statements the code actually issues, not restatements of them.

The three requirements each have a named test:

1. **crash between flush and watermark advance -> no loss, no
   double-count on resume** — `test_crash_before_commit_*`.
2. **bounded batch stepping** — `test_plan_backfill_days_*`,
   `test_catchup_processes_at_most_max_days_*`.
3. **recompute idempotence** — `test_recompute_window_is_idempotent_*`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app_shared.maintenance import rollup_watermark as wm
from app_shared.maintenance.rollups import (
    plan_backfill_days,
    recompute_window,
    run_rollup_catchup,
)

from unit._rollup_batch_fake import evaluate_batch, is_batch_statement

_NOW = datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)
#: `default_target_date(_NOW)` — the most recently completed UTC day.
_YESTERDAY = date(2026, 8, 25)


# ---------------------------------------------------------------------------
# Statement builders (rendered SQL, no session at all)
# ---------------------------------------------------------------------------


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_capability_probe_uses_to_regclass_on_the_watermark_table() -> None:
    stmt = wm._exists_stmt()
    sql = _compiled(stmt)
    assert "to_regclass" in sql
    assert stmt.compile().params["qualified_name"] == "public.rollup_watermarks"


def test_advance_is_monotonic_via_greatest() -> None:
    """The cursor may never rewind: a duplicated or out-of-order advance
    for an older window must be a no-op server-side, or a recompute of an
    old day would make the catch-up sweep re-walk everything after it."""
    sql = _compiled(wm._advance_stmt("daily_rollup", date(2026, 8, 20), _NOW))
    assert "GREATEST" in sql
    assert "ON CONFLICT (key) DO UPDATE" in sql


def test_seed_never_overwrites_an_existing_cursor() -> None:
    """Two workers racing the first run must not have one reset the
    other's progress -- the seed is `DO NOTHING`, not `DO UPDATE`."""
    sql = _compiled(wm._seed_stmt("daily_rollup", date(2026, 8, 24), _NOW))
    assert "ON CONFLICT (key) DO NOTHING" in sql


# ---------------------------------------------------------------------------
# 2. Bounded backfill: the pure stepping policy
# ---------------------------------------------------------------------------


def test_plan_backfill_days_is_empty_in_steady_state() -> None:
    assert plan_backfill_days(_YESTERDAY, _YESTERDAY, 7) == []


def test_plan_backfill_days_returns_owed_days_oldest_first() -> None:
    assert plan_backfill_days(date(2026, 8, 22), date(2026, 8, 25), 7) == [
        date(2026, 8, 23),
        date(2026, 8, 24),
        date(2026, 8, 25),
    ]


def test_plan_backfill_days_is_bounded_by_max_days() -> None:
    """THE bounded-scan requirement: a 400-day outage must not produce a
    400-day batch. The cap holds regardless of how far behind the cursor
    is."""
    days = plan_backfill_days(date(2025, 8, 25), date(2026, 8, 25), 7)
    assert len(days) == 7
    assert days[0] == date(2025, 8, 26)
    assert days[-1] == date(2025, 9, 1)


def test_plan_backfill_days_zero_cap_freezes_catchup() -> None:
    """`ROLLUP_BACKFILL_MAX_DAYS=0` is an operator freeze, not a crash --
    and it must not silently advance the cursor past the frozen days."""
    assert plan_backfill_days(date(2026, 8, 1), _YESTERDAY, 0) == []


def test_plan_backfill_days_never_returns_a_future_day() -> None:
    assert plan_backfill_days(_YESTERDAY, _YESTERDAY - timedelta(days=3), 7) == []


# ---------------------------------------------------------------------------
# Fake session: real statements, in-memory rollup table + cursor
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _CrashNow(Exception):
    """Simulated process death mid-transaction."""


class _FakeSession:
    """Dispatches on rendered SQL; models Postgres transaction semantics.

    Writes land in `self._pending` and only merge into the durable
    `self.rollups` / `self.watermark` on `commit()` — so a raised
    exception before a commit discards exactly what a real rollback (or a
    killed process) would discard. That is what makes the crash test a
    test and not an assertion about the code's own comments.
    """

    def __init__(
        self,
        observations: list,
        states: dict,
        *,
        watermark_table_exists: bool = True,
    ) -> None:
        self.observations = observations
        self.states = states
        self.watermark_table_exists = watermark_table_exists
        # Durable state.
        self.rollups: dict[tuple, dict] = {}
        self.watermark: dict | None = None
        self.completion: dict = {}
        # Uncommitted state.
        self._pending_rollups: dict[tuple, dict] = {}
        self._pending_watermark: dict | None = None
        self._pending_completion: dict = {}
        # Observability for the tests.
        self.upsert_calls = 0
        self.commits = 0
        self.crash_before_commit_on: set[str] = set()

    # -- transaction ------------------------------------------------------
    def commit(self) -> None:
        self.rollups.update(self._pending_rollups)
        if self._pending_watermark is not None:
            self.watermark = self._pending_watermark
        self.completion.update(self._pending_completion)
        self._pending_rollups = {}
        self._pending_watermark = None
        self._pending_completion = {}
        self.commits += 1

    # -- statements -------------------------------------------------------
    def execute(self, stmt):
        sql = str(stmt)

        if "to_regclass" in sql:
            return _FakeResult([self.watermark_table_exists])

        if "rollup_watermarks" in sql:
            return self._watermark_stmt(sql, stmt)

        if "rollup_completion" in sql:
            return self._completion_stmt(sql, stmt)

        # The EPA C7 set-based batch statement: one statement that does
        # the driver scan, the latest-eligible-per-match collapse, the
        # aggregate, the client-state join and the upsert. Evaluated over
        # the in-memory rows using the statement's OWN bind parameters.
        if is_batch_statement(sql):
            params = dict(stmt.compile().params)
            dry_run = "INSERT" not in sql

            def _upsert(values: dict) -> None:
                # Keyed on the real conflict arbiter, and ABSOLUTE (not
                # `count + delta`) — which is exactly why re-running a
                # day cannot double-count.
                key = (
                    values["workspace_id"],
                    values["product_variant_id"],
                    values["date"],
                )
                self._pending_rollups[key] = values
                self.upsert_calls += 1

            outcome = evaluate_batch(
                params,
                dry_run=dry_run,
                observations=self.observations,
                states=self.states,
                upsert=_upsert,
            )
            if not dry_run:
                day = params["target_date"].isoformat()
                if day in self.crash_before_commit_on:
                    raise _CrashNow(f"simulated crash while rolling up {day}")
            return _FakeResult([outcome])

        raise AssertionError(f"unexpected statement: {sql[:120]}")

    def _completion_stmt(self, sql: str, stmt):
        """The per-day checkpoint. Uncommitted like everything else."""
        params = stmt.compile().params
        if sql.strip().upper().startswith("SELECT"):
            current = {
                **self.completion,
                **self._pending_completion,
            }.get(params["day"])
            if current is None:
                return _FakeResult([])
            return _FakeResult([SimpleNamespace(**current)])
        if "complete = FALSE" in sql:
            self._pending_completion[params["day"]] = {
                "date": params["day"],
                "last_key_workspace_id": None,
                "last_key_variant_id": None,
                "complete": False,
                "updated_at": params["now"],
            }
            return _FakeResult([])
        self._pending_completion[params["day"]] = {
            "date": params["day"],
            "last_key_workspace_id": params["last_workspace_id"],
            "last_key_variant_id": params["last_variant_id"],
            "complete": params["complete"],
            "updated_at": params["now"],
        }
        return _FakeResult([])

    def _watermark_stmt(self, sql: str, stmt):
        params = stmt.compile().params
        current = (
            self._pending_watermark if self._pending_watermark is not None else self.watermark
        )
        if sql.strip().upper().startswith("SELECT") or "SELECT key" in sql:
            if current is None:
                return _FakeResult([])
            return _FakeResult([SimpleNamespace(**current)])
        if "DO NOTHING" in sql:
            if current is None:
                self._pending_watermark = {
                    "key": params["key"],
                    "last_complete_date": params["seed_date"],
                    "last_advanced_at": None,
                    "advance_count": 0,
                }
            return _FakeResult([])
        # The monotonic advance.
        if current is None:
            self._pending_watermark = {
                "key": params["key"],
                "last_complete_date": params["completed_date"],
                "last_advanced_at": params["now"],
                "advance_count": 1,
            }
        else:
            self._pending_watermark = {
                "key": current["key"],
                "last_complete_date": max(
                    current["last_complete_date"], params["completed_date"]
                ),
                "last_advanced_at": params["now"],
                "advance_count": current["advance_count"] + 1,
            }
        return _FakeResult([])

    def _in_day(self, day_start: datetime, day_end: datetime) -> list:
        return [obs for obs in self.observations if day_start <= obs.scraped_at < day_end]


def _observation(workspace_id, variant_id, product_id, scraped_at, price):
    return SimpleNamespace(
        workspace_id=workspace_id,
        product_variant_id=variant_id,
        product_id=product_id,
        scraped_at=scraped_at,
        price=price,
        currency="SAR",
        success=True,
        comparable=True,
    )


@pytest.fixture()
def three_day_session() -> _FakeSession:
    """Observations on 2026-08-23, -24 and -25 for one variant."""
    ws = uuid.uuid4()
    variant = uuid.uuid4()
    product = uuid.uuid4()
    observations = [
        _observation(
            ws,
            variant,
            product,
            datetime(2026, 8, day, 12, 0, tzinfo=timezone.utc),
            Decimal(f"{100 + day}.0000"),
        )
        for day in (23, 24, 25)
    ]
    states = {
        (ws, variant): SimpleNamespace(
            client_price=Decimal("99.0000"), currency="SAR", latest_alert_type="NONE"
        )
    }
    return _FakeSession(observations, states)


# ---------------------------------------------------------------------------
# Degraded mode + first-use seeding
# ---------------------------------------------------------------------------


def test_absent_watermark_table_degrades_to_the_previous_single_day_behaviour(
    three_day_session: _FakeSession, caplog: pytest.LogCaptureFixture
) -> None:
    """The pending-migration contract: an unmigrated database must keep
    working EXACTLY as it does today (roll up yesterday, once) and say so
    once, rather than failing or silently doing nothing."""
    session = three_day_session
    session.watermark_table_exists = False

    with caplog.at_level("WARNING"):
        report = run_rollup_catchup(session, now_utc=_NOW)

    assert report.watermark_available is False
    assert report.days_processed == [_YESTERDAY.isoformat()]
    assert session.watermark is None
    assert wm.EVENT_WATERMARK_STORE_ABSENT in caplog.text


def test_first_run_seeds_the_cursor_one_day_back_and_does_exactly_one_day(
    three_day_session: _FakeSession,
) -> None:
    """Turning the cursor on must NOT itself become a historical
    backfill: a cursor born at `yesterday - seed_lag` makes the first run
    do the same single day the pre-watermark code did."""
    report = run_rollup_catchup(three_day_session, now_utc=_NOW, seed_lag_days=1)

    assert report.seeded is True
    assert report.days_processed == [_YESTERDAY.isoformat()]
    assert three_day_session.watermark["last_complete_date"] == _YESTERDAY


# ---------------------------------------------------------------------------
# 1. Crash between the flush and the watermark advance
# ---------------------------------------------------------------------------


def test_crash_before_commit_leaves_the_cursor_unmoved_so_the_day_is_retried(
    three_day_session: _FakeSession,
) -> None:
    """THE crash-safety requirement, first half (no LOSS).

    The day's rollup upsert lands, then the process dies before the
    transaction commits. Neither the rollup rows nor the watermark
    advance are durable, so the very next run must plan that same day
    again -- it must not have been silently skipped.
    """
    session = three_day_session
    # Seed the cursor three days back so three days are owed.
    session.watermark = {
        "key": wm.WATERMARK_DAILY_ROLLUP,
        "last_complete_date": date(2026, 8, 22),
        "last_advanced_at": None,
        "advance_count": 0,
    }
    session.crash_before_commit_on = {"2026-08-24"}

    with pytest.raises(_CrashNow):
        run_rollup_catchup(session, now_utc=_NOW, max_days=7)

    # 08-23 committed; 08-24 died mid-transaction.
    assert session.watermark["last_complete_date"] == date(2026, 8, 23)
    assert {key[2] for key in session.rollups} == {date(2026, 8, 23)}

    # Resume: 08-24 is re-planned, not skipped.
    session.crash_before_commit_on = set()
    resumed = run_rollup_catchup(session, now_utc=_NOW, max_days=7)

    assert resumed.days_processed == ["2026-08-24", "2026-08-25"]
    assert session.watermark["last_complete_date"] == _YESTERDAY
    assert {key[2] for key in session.rollups} == {
        date(2026, 8, 23),
        date(2026, 8, 24),
        date(2026, 8, 25),
    }


def test_crash_after_commit_does_not_redo_the_day_and_a_redo_would_not_double_count(
    three_day_session: _FakeSession,
) -> None:
    """THE crash-safety requirement, second half (no DOUBLE-COUNT).

    Two independent guarantees, both asserted:

    * the committed day is not re-planned (the cursor advanced with it);
    * and even if it were, the write is an ABSOLUTE upsert on
      `(workspace_id, product_variant_id, date)` — re-deriving the day
      overwrites the row in place. There is no counter to double.
    """
    session = three_day_session
    session.watermark = {
        "key": wm.WATERMARK_DAILY_ROLLUP,
        "last_complete_date": date(2026, 8, 24),
        "last_advanced_at": None,
        "advance_count": 0,
    }

    first = run_rollup_catchup(session, now_utc=_NOW, max_days=7)
    assert first.days_processed == [_YESTERDAY.isoformat()]
    rows_after_first = dict(session.rollups)

    # A resume that follows the cursor does nothing at all.
    second = run_rollup_catchup(session, now_utc=_NOW, max_days=7)
    assert second.days_processed == []
    assert session.rollups == rows_after_first

    # And a forced redo of the same day converges on identical rows.
    recompute_window(session, _YESTERDAY, _YESTERDAY)
    assert session.rollups == rows_after_first
    assert len(session.rollups) == 1


# ---------------------------------------------------------------------------
# 2. Bounded batch stepping, end to end
# ---------------------------------------------------------------------------


def test_catchup_processes_at_most_max_days_and_reports_the_remainder(
    three_day_session: _FakeSession,
) -> None:
    """A long outage is caught up in bounded steps, each of which commits
    and moves the durable cursor -- never one unbounded scan."""
    session = three_day_session
    session.watermark = {
        "key": wm.WATERMARK_DAILY_ROLLUP,
        "last_complete_date": date(2026, 8, 20),
        "last_advanced_at": None,
        "advance_count": 0,
    }

    first = run_rollup_catchup(session, now_utc=_NOW, max_days=2)
    assert first.days_processed == ["2026-08-21", "2026-08-22"]
    assert first.days_remaining == 3
    assert session.watermark["last_complete_date"] == date(2026, 8, 22)

    second = run_rollup_catchup(session, now_utc=_NOW, max_days=2)
    assert second.days_processed == ["2026-08-23", "2026-08-24"]
    assert second.days_remaining == 1
    assert session.watermark["last_complete_date"] == date(2026, 8, 24)

    third = run_rollup_catchup(session, now_utc=_NOW, max_days=2)
    assert third.days_processed == ["2026-08-25"]
    assert third.days_remaining == 0
    assert session.watermark["last_complete_date"] == _YESTERDAY


def test_each_day_commits_separately_so_progress_is_durable_mid_batch(
    three_day_session: _FakeSession,
) -> None:
    """Progress is durable at a granularity FINER than the day.

    Since EPA C7 each day is walked in keyset batches that each commit,
    so a three-day catch-up commits twice per day (the day's single
    batch, then its watermark advance) rather than once. The property
    that matters is unchanged and is asserted directly: after the run,
    every day's rows AND the cursor are durable, and nothing is left
    pending.
    """
    session = three_day_session
    session.watermark = {
        "key": wm.WATERMARK_DAILY_ROLLUP,
        "last_complete_date": date(2026, 8, 22),
        "last_advanced_at": None,
        "advance_count": 0,
    }
    report = run_rollup_catchup(session, now_utc=_NOW, max_days=7)

    assert report.days_processed == ["2026-08-23", "2026-08-24", "2026-08-25"]
    # At least one commit per day, and strictly more than one transaction
    # in total -- a single all-or-nothing transaction for the batch would
    # lose every day on any failure.
    assert session.commits >= len(report.days_processed)
    assert session.watermark["last_complete_date"] == _YESTERDAY
    assert session._pending_rollups == {}
    assert session._pending_watermark is None
    # Every processed day is marked complete in the durable checkpoint --
    # what C8's retention gate reads.
    for day in (date(2026, 8, 23), date(2026, 8, 24), date(2026, 8, 25)):
        assert session.completion[day]["complete"] is True


def test_a_day_with_no_observations_still_advances_the_cursor(
    three_day_session: _FakeSession,
) -> None:
    """The liveness property that a watermark DERIVED from the rollup
    table could not provide: a day with zero observations produces zero
    rows, so 'highest date present in variant_price_daily_rollups' would
    stick there forever and the sweep would never reach the present."""
    session = three_day_session
    session.observations = []  # no activity at all in the window
    session.watermark = {
        "key": wm.WATERMARK_DAILY_ROLLUP,
        "last_complete_date": date(2026, 8, 23),
        "last_advanced_at": None,
        "advance_count": 0,
    }

    report = run_rollup_catchup(session, now_utc=_NOW, max_days=7)

    assert report.days_processed == ["2026-08-24", "2026-08-25"]
    assert report.rollups_upserted == 0
    assert session.rollups == {}
    assert session.watermark["last_complete_date"] == _YESTERDAY


# ---------------------------------------------------------------------------
# 3. Recompute
# ---------------------------------------------------------------------------


def test_recompute_window_is_idempotent_and_does_not_double_count(
    three_day_session: _FakeSession,
) -> None:
    """Running the documented repair twice over the same range must leave
    byte-identical rows -- safe against live rollups."""
    session = three_day_session

    first = recompute_window(session, date(2026, 8, 23), date(2026, 8, 25))
    assert first.days_recomputed == ["2026-08-23", "2026-08-24", "2026-08-25"]
    snapshot = dict(session.rollups)
    assert len(snapshot) == 3

    second = recompute_window(session, date(2026, 8, 23), date(2026, 8, 25))
    assert second.days_recomputed == first.days_recomputed
    assert session.rollups == snapshot
    assert len(session.rollups) == 3


def test_recompute_window_never_moves_the_cadence_watermark(
    three_day_session: _FakeSession,
) -> None:
    """A repair is not progress: recomputing an old day must not tell the
    cadence it has reached that day (nor rewind it from where it is)."""
    session = three_day_session
    session.watermark = {
        "key": wm.WATERMARK_DAILY_ROLLUP,
        "last_complete_date": date(2026, 8, 25),
        "last_advanced_at": None,
        "advance_count": 0,
    }

    recompute_window(session, date(2026, 8, 23), date(2026, 8, 23))

    assert session.watermark["last_complete_date"] == date(2026, 8, 25)
    assert session.watermark["advance_count"] == 0


def test_recompute_window_rejects_an_inverted_range(
    three_day_session: _FakeSession,
) -> None:
    with pytest.raises(ValueError, match="precedes"):
        recompute_window(three_day_session, date(2026, 8, 25), date(2026, 8, 23))


def test_recompute_window_dry_run_sends_no_write_statement(
    three_day_session: _FakeSession,
) -> None:
    report = recompute_window(
        three_day_session, date(2026, 8, 23), date(2026, 8, 25), dry_run=True
    )
    assert report.dry_run is True
    assert report.rollups_upserted == 3, "counts are real, not simulated"
    assert three_day_session.upsert_calls == 0
    assert three_day_session.rollups == {}
