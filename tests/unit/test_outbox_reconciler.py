"""`app_shared.outbox.reconciler` unit tests (2026-08-15 audit risk H1).

The safety net around the drain. What has to be true:

* backlog health is reported: how many PENDING, how many overdue past
  `stuck_after_seconds`, and the age of the oldest PENDING message (the
  one number that shows async work silently piling up, audit §H5);
* `DEAD` rows are counted and surfaced at ERROR — a dead letter means
  committed domain work never reached a worker;
* retention deletes only *terminal* rows past their cutoff, and keeps
  DEAD rows far longer than PUBLISHED ones (incident evidence);
* the table can therefore never grow unbounded.

Against an in-memory fake session, no Postgres.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Delete, Select

from app_shared.enums import OutboxStatus
from app_shared.outbox.reconciler import DEAD_RETENTION_MULTIPLIER, sweep_outbox

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


class _Row:
    def __init__(self, status: OutboxStatus, *, created_at: datetime, updated_at: datetime,
                 available_at: datetime) -> None:
        self.id = uuid.uuid4()
        self.status = status
        self.created_at = created_at
        self.updated_at = updated_at
        self.available_at = available_at


class _FakeResult:
    def __init__(self, value: Any, rowcount: int = 0) -> None:
        self._value = value
        self.rowcount = rowcount

    def scalar_one(self) -> Any:
        return self._value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """Evaluates the reconciler's four counts + two deletes over `rows`.

    Rather than parse SQL, it inspects the compiled statement's bind
    parameters, which is enough to distinguish the six statements the
    reconciler issues while still exercising the real SQLAlchemy objects.
    """

    def __init__(self, rows: list[_Row]) -> None:
        self.rows = rows
        self.deleted: list[list[_Row]] = []

    def execute(self, statement: Any) -> _FakeResult:
        params = statement.compile().params
        status = params.get("status_1")

        if isinstance(statement, Delete):
            cutoff = params["updated_at_1"]
            doomed = [r for r in self.rows if r.status == status and r.updated_at < cutoff]
            for row in doomed:
                self.rows.remove(row)
            self.deleted.append(doomed)
            return _FakeResult(None, rowcount=len(doomed))

        assert isinstance(statement, Select)
        sql = str(statement)
        if "min(" in sql:
            pending = [r for r in self.rows if r.status == OutboxStatus.PENDING]
            return _FakeResult(min((r.created_at for r in pending), default=None))
        if "available_at" in sql:
            cutoff = params["available_at_1"]
            return _FakeResult(
                sum(1 for r in self.rows if r.status == status and r.available_at <= cutoff)
            )
        return _FakeResult(sum(1 for r in self.rows if r.status == status))


def _pending(age_seconds: int = 0, overdue_seconds: int = 0) -> _Row:
    return _Row(
        OutboxStatus.PENDING,
        created_at=NOW - timedelta(seconds=age_seconds),
        updated_at=NOW - timedelta(seconds=age_seconds),
        available_at=NOW - timedelta(seconds=overdue_seconds),
    )


def _terminal(status: OutboxStatus, *, age_days: float) -> _Row:
    moment = NOW - timedelta(days=age_days)
    return _Row(status, created_at=moment, updated_at=moment, available_at=moment)


def _sweep(rows: list[_Row], *, stuck_after_seconds: int = 900, retention_days: int = 7):
    session = _FakeSession(rows)
    report = sweep_outbox(
        session,
        now=NOW,
        stuck_after_seconds=stuck_after_seconds,
        retention_days=retention_days,
    )
    return report, session


# --- backlog health ---------------------------------------------------------


def test_reports_pending_count_and_oldest_pending_age() -> None:
    report, _ = _sweep([_pending(age_seconds=30), _pending(age_seconds=600)])

    assert report.pending == 2
    assert report.oldest_pending_age_seconds == 600


def test_oldest_pending_age_is_none_on_an_empty_backlog() -> None:
    report, _ = _sweep([])

    assert report.pending == 0
    assert report.oldest_pending_age_seconds is None


def test_only_messages_overdue_past_the_threshold_count_as_stuck() -> None:
    report, _ = _sweep(
        [_pending(overdue_seconds=10), _pending(overdue_seconds=5000)],
        stuck_after_seconds=900,
    )

    assert report.pending == 2
    assert report.stuck == 1


def test_dead_letters_are_counted_and_logged_at_error(
    caplog: Any,
) -> None:
    rows = [_terminal(OutboxStatus.DEAD, age_days=0)]

    with caplog.at_level(logging.INFO, logger="app_shared.outbox.reconciler"):
        report, _ = _sweep(rows)

    assert report.dead == 1
    record = next(r for r in caplog.records if r.name == "app_shared.outbox.reconciler")
    assert record.levelno == logging.ERROR


def test_a_clean_sweep_logs_at_info(caplog: Any) -> None:
    with caplog.at_level(logging.INFO, logger="app_shared.outbox.reconciler"):
        _sweep([_pending()])

    record = next(r for r in caplog.records if r.name == "app_shared.outbox.reconciler")
    assert record.levelno == logging.INFO


# --- retention --------------------------------------------------------------


def test_published_rows_past_the_window_are_deleted() -> None:
    fresh = _terminal(OutboxStatus.PUBLISHED, age_days=1)
    stale = _terminal(OutboxStatus.PUBLISHED, age_days=30)

    report, session = _sweep([fresh, stale], retention_days=7)

    assert report.published_deleted == 1
    assert session.rows == [fresh]


def test_pending_rows_are_never_deleted_however_old() -> None:
    ancient_pending = _pending(age_seconds=86400 * 365)

    report, session = _sweep([ancient_pending], retention_days=7)

    assert report.published_deleted == 0
    assert report.dead_deleted == 0
    assert session.rows == [ancient_pending]


def test_dead_rows_are_kept_much_longer_than_published_rows() -> None:
    # Old enough for the PUBLISHED window, far too young for the DEAD one.
    dead = _terminal(OutboxStatus.DEAD, age_days=30)

    report, session = _sweep([dead], retention_days=7)

    assert report.dead_deleted == 0
    assert session.rows == [dead]


def test_dead_rows_past_their_much_longer_window_are_eventually_deleted() -> None:
    retention_days = 7
    dead = _terminal(
        OutboxStatus.DEAD, age_days=retention_days * DEAD_RETENTION_MULTIPLIER + 1
    )

    report, session = _sweep([dead], retention_days=retention_days)

    assert report.dead_deleted == 1
    assert session.rows == []
