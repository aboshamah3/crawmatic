"""Pure cadence-math tests (SPEC-13 T005, contracts/scheduler-loop.md
"Cadence computation", research R1).

No DB, no framework — ``app_shared.scheduling.cadence`` is duck-typed on a
minimal ``rule`` stand-in (``.cron_expression`` / ``.interval_minutes``)
so it is importable and testable with no ORM/session in the process.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app_shared.scheduling.cadence import compute_next_run_at, validate_cron


@dataclass
class _Rule:
    cron_expression: str | None = None
    interval_minutes: int | None = None


UTC = timezone.utc


def test_cron_next_occurrence_is_correct_and_strictly_future() -> None:
    """Daily-at-06:00 cron from a run_time before 06:00 lands at 06:00 same day."""
    rule = _Rule(cron_expression="0 6 * * *")
    run_time = datetime(2026, 7, 5, 3, 0, 0, tzinfo=UTC)

    next_run = compute_next_run_at(rule, run_time)

    assert next_run == datetime(2026, 7, 5, 6, 0, 0, tzinfo=UTC)
    assert next_run > run_time
    assert next_run.tzinfo is not None


def test_cron_next_occurrence_rolls_to_next_day_when_already_past() -> None:
    rule = _Rule(cron_expression="0 6 * * *")
    run_time = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)

    next_run = compute_next_run_at(rule, run_time)

    assert next_run == datetime(2026, 7, 6, 6, 0, 0, tzinfo=UTC)


def test_interval_next_run_adds_minutes_to_run_time() -> None:
    rule = _Rule(interval_minutes=15)
    run_time = datetime(2026, 7, 5, 3, 0, 0, tzinfo=UTC)

    next_run = compute_next_run_at(rule, run_time)

    assert next_run == run_time + timedelta(minutes=15)
    assert next_run.tzinfo is not None


def test_naive_run_time_is_treated_as_utc_and_result_is_tz_aware() -> None:
    rule = _Rule(interval_minutes=30)
    naive_run_time = datetime(2026, 7, 5, 3, 0, 0)  # no tzinfo

    next_run = compute_next_run_at(rule, naive_run_time)

    assert next_run.tzinfo is not None
    assert next_run == datetime(2026, 7, 5, 3, 30, 0, tzinfo=UTC)


def test_far_past_next_run_at_backlog_fires_once_to_single_future_result() -> None:
    """A rule whose stored next_run_at is far in the past must recompute off
    the actual run_time/now (never the stale next_run_at), yielding exactly
    one strictly-future result — no per-missed-interval catch-up.
    """
    rule = _Rule(cron_expression="*/5 * * * *")
    stale_next_run_at = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 7, 5, 10, 3, 0, tzinfo=UTC)

    # Cadence math must be called with `now`, not the stale stored value.
    next_run = compute_next_run_at(rule, now)

    assert next_run > now
    assert next_run == datetime(2026, 7, 5, 10, 5, 0, tzinfo=UTC)
    # Confirms it did not anchor on the far-past stale value.
    assert next_run != stale_next_run_at


def test_far_past_next_run_at_backlog_fires_once_for_interval_rule() -> None:
    rule = _Rule(interval_minutes=60)
    now = datetime(2026, 7, 5, 10, 3, 0, tzinfo=UTC)

    next_run = compute_next_run_at(rule, now)

    assert next_run == now + timedelta(minutes=60)
    assert next_run > now


def test_validate_cron_accepts_valid_five_field_expression() -> None:
    assert validate_cron("0 6 * * *") is None
    assert validate_cron("*/5 * * * *") is None


def test_validate_cron_rejects_unparseable_expression() -> None:
    with pytest.raises(ValueError):
        validate_cron("not a cron")


def test_validate_cron_rejects_out_of_range_field() -> None:
    with pytest.raises(ValueError):
        validate_cron("60 * * * *")


def test_validate_cron_rejects_empty_string() -> None:
    with pytest.raises(ValueError):
        validate_cron("")


def test_compute_next_run_at_rejects_non_positive_interval() -> None:
    rule = _Rule(interval_minutes=0)
    run_time = datetime(2026, 7, 5, 3, 0, 0, tzinfo=UTC)

    with pytest.raises(ValueError):
        compute_next_run_at(rule, run_time)


def test_compute_next_run_at_rejects_negative_interval() -> None:
    rule = _Rule(interval_minutes=-5)
    run_time = datetime(2026, 7, 5, 3, 0, 0, tzinfo=UTC)

    with pytest.raises(ValueError):
        compute_next_run_at(rule, run_time)


def test_compute_next_run_at_rejects_invalid_cron_expression() -> None:
    rule = _Rule(cron_expression="not a cron")
    run_time = datetime(2026, 7, 5, 3, 0, 0, tzinfo=UTC)

    with pytest.raises(ValueError):
        compute_next_run_at(rule, run_time)


def test_compute_next_run_at_rejects_neither_cadence_field_set() -> None:
    rule = _Rule()
    run_time = datetime(2026, 7, 5, 3, 0, 0, tzinfo=UTC)

    with pytest.raises(ValueError):
        compute_next_run_at(rule, run_time)
