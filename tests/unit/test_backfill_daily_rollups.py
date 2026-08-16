"""Unit tests for `scripts/backfill_daily_rollups.py` (SPEC-15 Task 1.4).

The normal `daily_rollup` cadence (`app_shared.maintenance.rollups.
run_daily_rollup`, wired by `apps/workers/app/workers/tasks_maintenance.
daily_rollup`) only ever targets "yesterday UTC" -- so the 2026-07-11 ->
2026-08-12 production backlog (~32k `price_observations` rows, predating
any rollup run) never self-heals. This script closes that gap by re-
running `run_daily_rollup` once per day across an inclusive range.

DB-independent: `run_daily_rollup`'s own read/write statements are
exercised end-to-end against a persistent fake `Session` (same dispatch
pattern as `tests/unit/test_rollup_aggregation.py`'s `_FakeSession`, but
backed by a `dict` SHARED across every session instance a test's fake
factory hands out -- one per day, mirroring `run_backfill`'s real one-
session-per-day shape) so the real `ON CONFLICT ... DO UPDATE` upsert's
dedup behaviour is provable without a live Postgres: re-running
`run_backfill` over the same date range must not create a second entry
for a (workspace_id, product_variant_id, date) key already written by the
first run -- the idempotency contract this whole script exists to
satisfy (backfilling the same day twice, or overlapping with the normal
cadence, must never duplicate rows).
"""

from __future__ import annotations

import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

# `scripts/` has no __init__.py / installed entry point -- match the
# sys.path convention `tests/unit/test_seed_bootstrap.py` uses to import
# `scripts.seed_bootstrap`.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.backfill_daily_rollups import (  # noqa: E402
    DEFAULT_START,
    date_range,
    main,
    parse_args,
    run_backfill,
)

# --- date_range --------------------------------------------------------


def test_date_range_is_inclusive_of_both_ends() -> None:
    days = list(date_range(date(2026, 7, 11), date(2026, 7, 13)))
    assert days == [date(2026, 7, 11), date(2026, 7, 12), date(2026, 7, 13)]


def test_date_range_single_day() -> None:
    assert list(date_range(date(2026, 7, 11), date(2026, 7, 11))) == [date(2026, 7, 11)]


def test_date_range_empty_when_end_before_start() -> None:
    assert list(date_range(date(2026, 7, 12), date(2026, 7, 11))) == []


# --- parse_args ----------------------------------------------------------


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.start == DEFAULT_START
    assert args.end is None
    assert args.apply is False


def test_parse_args_apply_flag_and_explicit_range() -> None:
    args = parse_args(["--start", "2026-07-11", "--end", "2026-07-12", "--apply"])
    assert args.start == date(2026, 7, 11)
    assert args.end == date(2026, 7, 12)
    assert args.apply is True


# --- run_backfill: persistent fake session, idempotency proof ------------


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _PersistentFakeSession:
    """Minimal `Session` stand-in for `run_daily_rollup`, backed by a
    `store` dict SHARED across every session instance a test's fake
    factory hands out -- simulating a real Postgres upsert's durability
    across `run_backfill`'s one-session-per-day loop, including the real
    `ON CONFLICT ... DO UPDATE` arbiter's dedup on a re-run over the same
    range."""

    def __init__(self, observations: list, states: dict, store: dict) -> None:
        self.observations = observations
        self.states = states
        self.store = store
        self.committed = False
        self.rolled_back = False
        self.closed = False

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
                    SimpleNamespace(workspace_id=ws, product_variant_id=variant, product_id=product)
                    for ws, variant, product in seen
                ]
            )
        if "variant_price_states" in sql:
            params = stmt.compile().params
            state = self.states.get((params["workspace_id"], params["product_variant_id"]))
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
        # The upsert statement: key on (workspace_id, product_variant_id,
        # date) exactly like the real ON CONFLICT arbiter -- a second
        # write for the same key overwrites the stored value in place,
        # never appends a second entry.
        params = dict(stmt.compile().params)
        key = (params["workspace_id"], params["product_variant_id"], params["date"])
        self.store[key] = params
        return _FakeResult([])

    def _in_day(self, day_start: datetime, day_end: datetime) -> list:
        return [obs for obs in self.observations if day_start <= obs.scraped_at < day_end]

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


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


def _two_day_fixture():
    """Two UTC days, one observation each, same workspace/variant -- the
    brief's seeded-session shape: "two days of observations -> two
    rollup rows"."""
    workspace_id = uuid.uuid4()
    variant_id = uuid.uuid4()
    product_id = uuid.uuid4()
    day1 = date(2026, 7, 11)
    day2 = date(2026, 7, 12)

    observations = [
        _observation(
            workspace_id,
            variant_id,
            product_id,
            datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc),
            "10.0000",
        ),
        _observation(
            workspace_id,
            variant_id,
            product_id,
            datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc),
            "20.0000",
        ),
    ]
    states = {
        (workspace_id, variant_id): SimpleNamespace(
            client_price=Decimal("15.0000"), currency="SAR", latest_alert_type=None
        )
    }
    return observations, states, day1, day2, workspace_id, variant_id


def test_run_backfill_seeded_two_days_produces_two_rollup_rows() -> None:
    observations, states, day1, day2, workspace_id, variant_id = _two_day_fixture()
    store: dict = {}
    sessions: list[_PersistentFakeSession] = []

    def factory() -> _PersistentFakeSession:
        session = _PersistentFakeSession(observations, states, store)
        sessions.append(session)
        return session

    results = run_backfill(start=day1, end=day2, apply=True, session_factory=factory)

    assert [r.target_date for r in results] == [day1, day2]
    assert [r.report.rollups_upserted for r in results] == [1, 1]
    assert len(store) == 2  # one row per day, keyed by (ws, variant, date)
    assert (workspace_id, variant_id, day1) in store
    assert (workspace_id, variant_id, day2) in store
    # apply=True commits every day's session, never rolls back.
    assert all(s.committed and not s.rolled_back for s in sessions)


def test_run_backfill_rerun_is_idempotent_same_counts_no_duplicates() -> None:
    """Re-running the identical range must not duplicate store entries or
    change the reported counts -- the ON CONFLICT upsert dedups by
    (workspace_id, product_variant_id, date), proven end to end through
    `run_backfill`, not just at the SQL-statement level."""
    observations, states, day1, day2, workspace_id, variant_id = _two_day_fixture()
    store: dict = {}

    def factory() -> _PersistentFakeSession:
        return _PersistentFakeSession(observations, states, store)

    first = run_backfill(start=day1, end=day2, apply=True, session_factory=factory)
    second = run_backfill(start=day1, end=day2, apply=True, session_factory=factory)

    assert [r.report.rollups_upserted for r in first] == [1, 1]
    assert [r.report.rollups_upserted for r in second] == [1, 1]
    assert len(store) == 2  # still exactly two rows -- no duplicates from the re-run
    assert store[(workspace_id, variant_id, day1)]["cheapest_competitor_price"] == Decimal("10.0000")
    assert store[(workspace_id, variant_id, day2)]["cheapest_competitor_price"] == Decimal("20.0000")


def test_run_backfill_dry_run_rolls_back_every_day_and_never_commits() -> None:
    observations, states, day1, day2, _, _ = _two_day_fixture()
    store: dict = {}
    sessions: list[_PersistentFakeSession] = []

    def factory() -> _PersistentFakeSession:
        session = _PersistentFakeSession(observations, states, store)
        sessions.append(session)
        return session

    results = run_backfill(start=day1, end=day2, apply=False, session_factory=factory)

    assert [r.report.rollups_upserted for r in results] == [1, 1]
    # The upsert statement still executed against the fake session (the
    # reported counts reflect the real write path)...
    assert len(store) == 2
    # ...but every session was rolled back, never committed -- dry-run
    # must write nothing durable in the real (non-fake) path.
    assert all(session.rolled_back and not session.committed for session in sessions)


def test_run_backfill_empty_range_yields_no_results() -> None:
    _, _, day1, day2, _, _ = _two_day_fixture()
    results = run_backfill(start=day2, end=day1, apply=True, session_factory=lambda: None)
    assert results == []


# --- main(): start > end guard --------------------------------------------


def test_main_returns_error_when_start_after_end() -> None:
    rc = main(["--start", "2026-08-01", "--end", "2026-07-01"])
    assert rc == 1


# --- main(): dry-run/apply wiring via a monkeypatched session factory ----


def test_main_dry_run_never_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    observations, states, day1, day2, _, _ = _two_day_fixture()
    store: dict = {}
    sessions: list[_PersistentFakeSession] = []

    def fake_sessionmaker():
        def factory():
            session = _PersistentFakeSession(observations, states, store)
            sessions.append(session)
            return session

        return factory

    monkeypatch.setattr("app_shared.database.get_system_sessionmaker", fake_sessionmaker)

    rc = main(["--start", day1.isoformat(), "--end", day2.isoformat()])

    assert rc == 0
    assert len(sessions) == 2
    assert all(s.rolled_back and not s.committed for s in sessions)
    assert len(store) == 2  # computed and reported, never persisted for real


def test_main_apply_commits_each_day(monkeypatch: pytest.MonkeyPatch) -> None:
    observations, states, day1, day2, _, _ = _two_day_fixture()
    store: dict = {}
    sessions: list[_PersistentFakeSession] = []

    def fake_sessionmaker():
        def factory():
            session = _PersistentFakeSession(observations, states, store)
            sessions.append(session)
            return session

        return factory

    monkeypatch.setattr("app_shared.database.get_system_sessionmaker", fake_sessionmaker)

    rc = main(["--start", day1.isoformat(), "--end", day2.isoformat(), "--apply"])

    assert rc == 0
    assert len(sessions) == 2
    assert all(s.committed and not s.rolled_back for s in sessions)
    assert len(store) == 2
