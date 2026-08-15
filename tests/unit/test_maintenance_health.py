"""Maintenance **outcome** assertions (2026-08-15 readiness cycle).

These are the signals whose absence turned a broken maintenance path into
a silent, month-long failure that was 17 days from a total write outage.
They assert on what is actually in the database — "does the partition the
calendar will need exist?", "has the daily rollup produced a row lately?"
— rather than on whether the scheduler believes it enqueued something,
because in the real incident the scheduler was enqueueing correctly on
schedule and every task died on arrival in the worker.

No live DB: `find_missing_partitions` is `to_regclass` probes and the two
rollup probes are single scalar reads, so the same fake-session idiom as
`test_partition_bounds.py` covers them exactly.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app_shared.maintenance.health import (
    EVENT_HEALTH_OK,
    EVENT_PARTITION_MISSING,
    EVENT_ROLLUP_STALE,
    check_maintenance_health,
    find_missing_partitions,
    log_health_report,
)
from app_shared.maintenance.partitions import month_partition_bounds, partition_name
from app_shared.maintenance.registry import PARTITIONED_TABLES

UTC = timezone.utc


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class _FakeSession:
    """Answers `to_regclass`, `max(date)` and the observations EXISTS probe."""

    def __init__(
        self,
        existing_relations: set[str],
        *,
        max_rollup_date: date | None = None,
        has_old_observations: bool = False,
    ) -> None:
        self.existing_relations = existing_relations
        self.max_rollup_date = max_rollup_date
        self.has_old_observations = has_old_observations

    def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        sql = str(stmt)
        if "to_regclass" in sql:
            qualified = stmt.compile().params["qualified_name"]
            name = qualified.split(".", 1)[1]
            return _FakeResult(name if name in self.existing_relations else None)
        if "max(date)" in sql:
            return _FakeResult(self.max_rollup_date)
        if "price_observations" in sql:
            return _FakeResult(self.has_old_observations)
        raise AssertionError(f"unexpected statement: {sql}")


def _all_parents() -> set[str]:
    return {entry.name for entry in PARTITIONED_TABLES if entry.name != "webhook_events"}


def _with_partitions(now: datetime, months: int) -> set[str]:
    existing = _all_parents()
    for entry in PARTITIONED_TABLES:
        if entry.name == "webhook_events":
            continue
        for offset in range(months + 1):
            suffix, _start, _end = month_partition_bounds(now, offset)
            existing.add(partition_name(entry.name, suffix))
    return existing


# ---------------------------------------------------------------------
# Partition assertion
# ---------------------------------------------------------------------


def test_missing_next_month_partition_is_detected() -> None:
    """Exactly the production shape on 2026-08-15: the current month's
    partitions exist, September's do not, and every INSERT starts failing
    at midnight on the 1st."""
    now = datetime(2026, 8, 15, tzinfo=UTC)
    session = _FakeSession(_with_partitions(now, months=0))

    missing, skipped = find_missing_partitions(session, now_utc=now, months_ahead=1)

    assert skipped == ["webhook_events"]
    names = {m.partition for m in missing}
    assert names == {
        partition_name(entry.name, "2026_09")
        for entry in PARTITIONED_TABLES
        if entry.name != "webhook_events"
    }
    assert all(m.days_until_writes_fail(now) == 17 for m in missing)


def test_absent_parent_table_is_skipped_not_reported_missing() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    session = _FakeSession(_with_partitions(now, months=3))

    missing, skipped = find_missing_partitions(session, now_utc=now, months_ahead=3)

    assert missing == []
    assert skipped == ["webhook_events"]


def test_missing_current_month_partition_reports_zero_days() -> None:
    """A missing *current*-month partition means writes are failing right
    now, not on some future date."""
    now = datetime(2026, 8, 15, tzinfo=UTC)
    session = _FakeSession(_all_parents())

    missing, _skipped = find_missing_partitions(session, now_utc=now, months_ahead=0)

    assert missing
    assert all(m.days_until_writes_fail(now) == 0 for m in missing)


def test_partition_missing_event_is_logged_at_error(caplog) -> None:  # noqa: ANN001
    now = datetime(2026, 8, 15, tzinfo=UTC)
    session = _FakeSession(
        _with_partitions(now, months=0), max_rollup_date=date(2026, 8, 14)
    )

    report = check_maintenance_health(
        session, now_utc=now, months_ahead=1, rollup_stale_after_days=3
    )
    with caplog.at_level("ERROR"):
        log_health_report(report, now_utc=now, threshold_days=3)

    assert not report.healthy
    assert EVENT_PARTITION_MISSING in caplog.text
    assert "days_until_writes_fail=17" in caplog.text
    assert "month_start=2026-09-01" in caplog.text


# ---------------------------------------------------------------------
# Rollup staleness assertion
# ---------------------------------------------------------------------


def test_rollup_never_produced_a_row_while_observations_exist_is_stale() -> None:
    """The exact production state: 32,231 observations spanning a month,
    zero rollup rows — which also silently pins retention forever,
    because `rollups_cover()` correctly refuses to drop uncovered
    partitions."""
    now = datetime(2026, 8, 15, tzinfo=UTC)
    session = _FakeSession(
        _with_partitions(now, months=3) | {"variant_price_daily_rollups"},
        max_rollup_date=None,
        has_old_observations=True,
    )

    report = check_maintenance_health(
        session, now_utc=now, months_ahead=3, rollup_stale_after_days=3
    )

    assert report.rollup_stale
    assert report.last_rollup_date is None


def test_fresh_install_with_no_observations_is_not_stale() -> None:
    """A brand-new deployment has no rollups and nothing to roll up — it
    must not page anyone."""
    now = datetime(2026, 8, 15, tzinfo=UTC)
    session = _FakeSession(
        _with_partitions(now, months=3) | {"variant_price_daily_rollups"},
        max_rollup_date=None,
        has_old_observations=False,
    )

    report = check_maintenance_health(
        session, now_utc=now, months_ahead=3, rollup_stale_after_days=3
    )

    assert not report.rollup_stale
    assert report.healthy


def test_one_day_behind_is_healthy_but_a_week_behind_is_stale(caplog) -> None:  # noqa: ANN001
    now = datetime(2026, 8, 15, tzinfo=UTC)
    existing = _with_partitions(now, months=3) | {"variant_price_daily_rollups"}

    healthy = check_maintenance_health(
        _FakeSession(existing, max_rollup_date=date(2026, 8, 14)),
        now_utc=now,
        months_ahead=3,
        rollup_stale_after_days=3,
    )
    assert not healthy.rollup_stale
    assert healthy.healthy

    stale = check_maintenance_health(
        _FakeSession(existing, max_rollup_date=date(2026, 8, 8)),
        now_utc=now,
        months_ahead=3,
        rollup_stale_after_days=3,
    )
    assert stale.rollup_stale
    assert stale.rollup_days_stale == 7

    with caplog.at_level("ERROR"):
        log_health_report(stale, now_utc=now, threshold_days=3)
    assert EVENT_ROLLUP_STALE in caplog.text
    assert "days_stale=7" in caplog.text


def test_clean_pass_emits_an_ok_line(caplog) -> None:  # noqa: ANN001
    """Silence must be distinguishable from "the check never ran"."""
    now = datetime(2026, 8, 15, tzinfo=UTC)
    session = _FakeSession(
        _with_partitions(now, months=3) | {"variant_price_daily_rollups"},
        max_rollup_date=date(2026, 8, 14),
    )

    report = check_maintenance_health(
        session, now_utc=now, months_ahead=3, rollup_stale_after_days=3
    )
    with caplog.at_level("INFO"):
        log_health_report(report, now_utc=now, threshold_days=3)

    assert report.healthy
    assert EVENT_HEALTH_OK in caplog.text
    assert EVENT_PARTITION_MISSING not in caplog.text
