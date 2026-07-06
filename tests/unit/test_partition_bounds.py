"""Unit tests for `app_shared.maintenance.partitions` month bounds + the
`create_missing_partitions` creation logic (SPEC-15 T012, US1,
contracts/partition-creation.md, FR-004/005/006/007).

Pure, DB-independent:

* `month_partition_bounds` — half-open UTC bounds, the Dec->Jan year
  rollover, February's length, `offset=0` (current month, self-heal) vs
  `offset=1` (next month), and tz-aware-UTC enforcement (FR-007/025).
* `create_missing_partitions` against a minimal in-memory fake
  `Session` (no live DB, no SQLAlchemy engine) that answers the
  `to_regclass` existence probe from a fixed set of "existing" relation
  names and records every executed DDL statement's compiled SQL text --
  proving the absent-table skip (FR-002) and the rendered
  `CREATE TABLE IF NOT EXISTS ... PARTITION OF ... FOR VALUES FROM (...)
  TO (...)` DDL shape (FR-004/005/006) without touching Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app_shared.maintenance.partitions import (
    create_missing_partitions,
    month_partition_bounds,
    partition_name,
)
from app_shared.maintenance.registry import PARTITIONED_TABLES


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --- month_partition_bounds: half-open bounds, rollover, tz-aware UTC -------


def test_offset_zero_is_current_month() -> None:
    now = datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)
    suffix, start, end = month_partition_bounds(now, 0)
    assert suffix == "2026_07"
    assert start == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_offset_one_is_next_month() -> None:
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    suffix, start, end = month_partition_bounds(now, 1)
    assert suffix == "2026_08"
    assert start == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_december_to_january_year_rollover() -> None:
    now = datetime(2026, 12, 10, tzinfo=timezone.utc)
    suffix, start, end = month_partition_bounds(now, 1)
    assert suffix == "2027_01"
    assert start == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert end == datetime(2027, 2, 1, tzinfo=timezone.utc)


def test_december_current_month_bounds_stay_in_same_year() -> None:
    now = datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)
    suffix, start, end = month_partition_bounds(now, 0)
    assert suffix == "2026_12"
    assert start == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_february_length_handled_via_next_month_not_fixed_day_count() -> None:
    now = datetime(2026, 2, 5, tzinfo=timezone.utc)
    suffix, start, end = month_partition_bounds(now, 0)
    assert suffix == "2026_02"
    assert start == datetime(2026, 2, 1, tzinfo=timezone.utc)
    # End is the 1st of March regardless of Feb having 28 or 29 days.
    assert end == datetime(2026, 3, 1, tzinfo=timezone.utc)


def test_january_to_february_rollover_within_same_year() -> None:
    now = datetime(2026, 1, 20, tzinfo=timezone.utc)
    suffix, start, end = month_partition_bounds(now, 1)
    assert suffix == "2026_02"
    assert start == datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 1, tzinfo=timezone.utc)


def test_bounds_are_half_open_start_inclusive_end_exclusive() -> None:
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _suffix, start, end = month_partition_bounds(now, 0)
    assert start < end
    assert (end - start).days in (28, 29, 30, 31)


def test_bounds_are_tz_aware_utc() -> None:
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    _suffix, start, end = month_partition_bounds(now, 0)
    assert start.tzinfo is timezone.utc
    assert end.tzinfo is timezone.utc


def test_naive_datetime_rejected() -> None:
    naive = datetime(2026, 7, 1)  # no tzinfo
    with pytest.raises(ValueError):
        month_partition_bounds(naive, 0)


# --- create_missing_partitions: fake session, no live DB --------------------


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class _FakeSession:
    """Minimal stand-in for a SQLAlchemy `Session`: answers the
    `to_regclass` existence probe from `existing_relations` and records
    every other executed statement's compiled (literal-bound) SQL text,
    without an engine/connection/live DB."""

    def __init__(self, existing_relations: set[str]) -> None:
        self.existing_relations = existing_relations
        self.executed_ddl: list[str] = []

    def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        sql = _compiled(stmt)
        if "to_regclass" in sql:
            params = stmt.compile().params
            qualified = params["qualified_name"]
            name = qualified.split(".", 1)[1]
            return _FakeResult(name if name in self.existing_relations else None)
        self.executed_ddl.append(sql)
        return _FakeResult(None)


def test_absent_registered_table_skipped_without_error() -> None:
    # `webhook_events` deliberately absent -- every OTHER registered
    # table + their current/next-month children exist.
    existing = {entry.name for entry in PARTITIONED_TABLES if entry.name != "webhook_events"}
    for entry in PARTITIONED_TABLES:
        if entry.name == "webhook_events":
            continue
        for offset in (0, 1):
            now = datetime(2026, 7, 15, tzinfo=timezone.utc)
            suffix, _start, _end = month_partition_bounds(now, offset)
            existing.add(partition_name(entry.name, suffix))

    session = _FakeSession(existing)
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)

    report = create_missing_partitions(session, now_utc=now, lookahead_months=1)

    assert report.tables_skipped_absent == ["webhook_events"]
    assert report.partitions_created == []
    assert session.executed_ddl == []


def test_missing_current_and_next_month_partitions_created() -> None:
    # Only the parent tables exist -- no child partitions at all yet.
    existing = {entry.name for entry in PARTITIONED_TABLES}
    session = _FakeSession(existing)
    now = datetime(2026, 12, 20, tzinfo=timezone.utc)

    report = create_missing_partitions(session, now_utc=now, lookahead_months=1)

    expected_created = set()
    for entry in PARTITIONED_TABLES:
        for offset in (0, 1):
            suffix, _start, _end = month_partition_bounds(now, offset)
            expected_created.add(partition_name(entry.name, suffix))

    assert set(report.partitions_created) == expected_created
    assert report.tables_skipped_absent == []

    # Dec->Jan rollover DDL is present with the correct FOR VALUES bounds.
    price_obs_dec = partition_name("price_observations", "2026_12")
    price_obs_jan = partition_name("price_observations", "2027_01")
    assert any(
        f"CREATE TABLE IF NOT EXISTS {price_obs_dec}" in ddl
        and "FROM ('2026-12-01')" in ddl
        and "TO ('2027-01-01')" in ddl
        for ddl in session.executed_ddl
    )
    assert any(
        f"CREATE TABLE IF NOT EXISTS {price_obs_jan}" in ddl
        and "FROM ('2027-01-01')" in ddl
        and "TO ('2027-02-01')" in ddl
        for ddl in session.executed_ddl
    )


def test_rerun_with_all_partitions_already_present_is_a_no_op() -> None:
    existing = {entry.name for entry in PARTITIONED_TABLES}
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    for entry in PARTITIONED_TABLES:
        for offset in (0, 1):
            suffix, _start, _end = month_partition_bounds(now, offset)
            existing.add(partition_name(entry.name, suffix))

    session = _FakeSession(existing)
    report = create_missing_partitions(session, now_utc=now, lookahead_months=1)

    assert report.partitions_created == []
    assert report.tables_skipped_absent == []
    assert session.executed_ddl == []


def test_only_missing_child_partition_is_created_existing_left_alone() -> None:
    # price_observations' current-month child already exists; next-month
    # child is missing -- only the missing one should be created
    # (self-heal only what's actually absent, no duplicate DDL).
    existing = {entry.name for entry in PARTITIONED_TABLES}
    now = datetime(2026, 3, 10, tzinfo=timezone.utc)
    current_suffix, _s, _e = month_partition_bounds(now, 0)
    existing.add(partition_name("price_observations", current_suffix))

    session = _FakeSession(existing)
    report = create_missing_partitions(session, now_utc=now, lookahead_months=1)

    next_suffix, _s2, _e2 = month_partition_bounds(now, 1)
    assert partition_name("price_observations", next_suffix) in report.partitions_created
    assert partition_name("price_observations", current_suffix) not in report.partitions_created
