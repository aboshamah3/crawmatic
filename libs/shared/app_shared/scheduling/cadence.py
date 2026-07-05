"""Cadence math for SPEC-13 scheduler (research R1; contracts/scheduler-loop.md
"Cadence computation").

Shared by **both** the ``/v1/refresh-rules`` API (computes the first
``next_run_at`` on create/update, validates the cron string) and the
scheduler pass (recomputes ``next_run_at`` after each run). Deliberately
duck-typed on ``rule`` (only ``.cron_expression`` / ``.interval_minutes``
are read) so this module stays decoupled from the ``RefreshRule`` ORM model
and is importable with no DB/SQLAlchemy session in the process — see
``tests/unit/test_cadence.py``.

Scraping-free: this module MUST NOT import Scrapy/Twisted/Playwright or
FastAPI/SQLAlchemy-session machinery (``tests/unit/test_import_boundaries.py``).

Cadence semantics (settled in research.md R1): both branches base the
computation on the **actual run time / now** passed in by the caller, never
on the stale ``next_run_at`` column. This gives FR-016 backlog tolerance for
free — a rule whose ``next_run_at`` is far in the past fires once on the next
pass and its recomputed ``next_run_at`` lands strictly in the future (the
next cron occurrence after ``run_time``, or ``run_time + interval``); there
is no per-missed-interval catch-up loop.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from croniter import CroniterBadCronError, CroniterBadDateError, croniter


class CadenceRule(Protocol):
    """Structural type for anything cadence math can be computed over.

    Any object exposing these two attributes works — the ``RefreshRule``
    ORM model satisfies this Protocol without either module depending on
    the other.
    """

    cron_expression: str | None
    interval_minutes: int | None


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as a tz-aware UTC datetime.

    Naive datetimes are treated as already-UTC (attached, not converted);
    tz-aware datetimes in another zone are converted to UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_cron(expr: str) -> None:
    """Raise ``ValueError`` with a clear message if ``expr`` is not a valid
    5-field cron expression; otherwise return ``None``.
    """
    if not expr or not expr.strip():
        raise ValueError("cron_expression must be a non-empty 5-field cron string")
    try:
        croniter(expr)
    except (CroniterBadCronError, CroniterBadDateError, ValueError, KeyError) as exc:
        raise ValueError(f"invalid cron expression {expr!r}: {exc}") from exc


def _validate_interval(interval_minutes: int) -> None:
    if interval_minutes <= 0:
        raise ValueError(
            f"interval_minutes must be > 0, got {interval_minutes!r}"
        )


def compute_next_run_at(rule: CadenceRule, run_time: datetime) -> datetime:
    """Compute the next ``next_run_at`` for ``rule``, based on ``run_time``.

    - cron rules: ``croniter(rule.cron_expression, run_time_utc).get_next(datetime)``.
    - interval rules: ``run_time_utc + timedelta(minutes=rule.interval_minutes)``.

    Always anchors on the passed-in ``run_time`` (never a stored/stale
    ``next_run_at``) so a far-past due rule advances to a single
    strictly-future result in one call (backlog fire-once, FR-016).

    Exactly one of ``rule.cron_expression`` / ``rule.interval_minutes`` is
    expected to be set (enforced upstream by the DB CHECK constraint /
    Pydantic validators); this function does not itself enforce
    exactly-one, it simply prefers ``cron_expression`` when both/neither
    are inspected by a caller that has already validated cadence shape.

    Returns a tz-aware UTC datetime.
    """
    run_time_utc = _as_utc(run_time)

    if rule.cron_expression:
        validate_cron(rule.cron_expression)
        next_run = croniter(rule.cron_expression, run_time_utc).get_next(datetime)
        return _as_utc(next_run)

    if rule.interval_minutes is not None:
        _validate_interval(rule.interval_minutes)
        return run_time_utc + timedelta(minutes=rule.interval_minutes)

    raise ValueError(
        "rule has neither cron_expression nor interval_minutes set; "
        "exactly one cadence field is required"
    )
