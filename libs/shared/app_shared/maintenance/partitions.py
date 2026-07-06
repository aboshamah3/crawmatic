"""Shared partition primitives (SPEC-15 US1/US3, data-model.md §3, research R2/R4).

Two catalog-reading building blocks reused by BOTH the partition-creation
job (US1) and the retention/drop job (US3), plus the ``{parent}_{YYYY}_{MM}``
name/suffix helpers every partition operation keys off of:

* :func:`table_exists` — the existence gate (FR-002, R4): probes
  ``to_regclass`` so a registered-but-not-yet-created table (e.g.
  ``webhook_events``, absent until SPEC-16) is skipped cleanly instead of
  raising.
* :func:`existing_partitions` — catalog discovery (FR-018) of a
  partitioned parent's current child partitions and their half-open UTC
  bounds, read from ``pg_catalog`` (``pg_inherits`` + ``pg_get_expr``).
  Used by US3's drop-eligibility check.
* :func:`partition_suffix` / :func:`partition_name` — the
  ``{parent}_{YYYY}_{MM}`` naming convention (mirrors the migration's
  ``_month_partition_bounds``, `alembic/versions/2db33dea5e14_...py`).
* :func:`month_partition_bounds` — the half-open ``[start, end)`` UTC
  bounds for the month ``offset`` months after ``now_utc``'s own month
  (FR-004/005/007), and :func:`create_missing_partitions` (US1, T008,
  contracts/partition-creation.md) — the current + next-month
  self-healing, idempotent ``CREATE TABLE ... PARTITION OF`` runtime DDL.

Partition ``DROP`` lives in US3 (``drop_partition``, T024, not yet
implemented in this phase). Scraping-free (Constitution I/V) —
SQLAlchemy + stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from app_shared.maintenance.registry import PARTITIONED_TABLES

# `pg_get_expr(relpartbound, oid)` renders a RANGE partition's bound as
# `FOR VALUES FROM ('<literal>') TO ('<literal>')` — this matches both
# bounds' literal text regardless of the emitting session's TimeZone
# setting (normalized to UTC in `_parse_bound_literal`).
_RANGE_BOUND_RE = re.compile(
    r"FOR VALUES FROM \('(?P<start>[^']+)'\) TO \('(?P<end>[^']+)'\)"
)


def _to_regclass_stmt(name: str):
    """Build the (unexecuted) ``to_regclass`` existence-probe statement.

    Split out from :func:`table_exists` so its rendered SQL / bound
    params can be asserted in a pure unit test without a live DB or
    session (T006).
    """
    return text("SELECT to_regclass(:qualified_name) IS NOT NULL").bindparams(
        qualified_name=f"public.{name}"
    )


def table_exists(session: Session, name: str) -> bool:
    """Return ``True`` iff relation ``name`` exists in the ``public`` schema.

    Uses ``to_regclass`` (research R4), which returns ``NULL`` for a
    missing relation instead of raising — a single cheap, catalog-safe
    probe so a registered-but-absent table (``webhook_events``, FR-002)
    never fails the whole maintenance pass.
    """
    return bool(session.execute(_to_regclass_stmt(name)).scalar())


class PartitionBounds(NamedTuple):
    """One discovered child partition and its half-open UTC ``[start, end)``."""

    name: str
    start: datetime
    end: datetime


def _existing_partitions_stmt(parent: str):
    """Build the (unexecuted) child-partition discovery statement.

    Reads ``pg_inherits`` (parent/child relationship) joined to
    ``pg_class`` for both ends, rendering each child's partition bound
    via ``pg_get_expr(relpartbound, inhrelid)`` (R2/R4) — the standard
    catalog-only way to enumerate a partitioned table's current children
    without any application-side bookkeeping table.
    """
    return text(
        """
        SELECT
            child.relname AS partition_name,
            pg_get_expr(child.relpartbound, child.oid) AS bound_expr
        FROM pg_inherits
        JOIN pg_class AS parent ON parent.oid = pg_inherits.inhparent
        JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
        JOIN pg_namespace AS parent_ns ON parent_ns.oid = parent.relnamespace
        WHERE parent.relname = :parent_name
          AND parent_ns.nspname = 'public'
        ORDER BY child.relname
        """
    ).bindparams(parent_name=parent)


def _parse_bound_literal(literal: str) -> datetime:
    """Parse one ``pg_get_expr`` bound literal into a tz-aware UTC ``datetime``.

    ``pg_get_expr`` renders the literal per the *emitting session's*
    ``TimeZone`` setting, so any offset it returns is normalized to UTC
    here rather than trusted verbatim (FR-025 — TIMESTAMPTZ/UTC
    everywhere).
    """
    return datetime.fromisoformat(literal).astimezone(timezone.utc)


def existing_partitions(session: Session, parent: str) -> list[PartitionBounds]:
    """Return ``parent``'s current child partitions with parsed UTC bounds.

    Each result is a :class:`PartitionBounds` (``name``, half-open
    ``[start, end)`` in UTC), discovered from ``pg_catalog`` — never from
    application-side bookkeeping (FR-018; used by US3's drop-eligibility
    check). Only ``RANGE``-bound children matching the standard
    ``FOR VALUES FROM (...) TO (...)`` form are included; anything else
    (e.g. a `DEFAULT` partition, if one is ever added) is skipped rather
    than mis-parsed.
    """
    rows = session.execute(_existing_partitions_stmt(parent)).all()
    partitions: list[PartitionBounds] = []
    for row in rows:
        match = _RANGE_BOUND_RE.search(row.bound_expr or "")
        if match is None:
            continue
        partitions.append(
            PartitionBounds(
                name=row.partition_name,
                start=_parse_bound_literal(match.group("start")),
                end=_parse_bound_literal(match.group("end")),
            )
        )
    return partitions


def partition_suffix(year: int, month: int) -> str:
    """Return the ``YYYY_MM`` suffix for ``year``/``month`` (1-12)."""
    return f"{year:04d}_{month:02d}"


def partition_name(parent: str, suffix: str) -> str:
    """Return the child partition table name ``{parent}_{suffix}``."""
    return f"{parent}_{suffix}"


def month_partition_bounds(now_utc: datetime, offset: int) -> tuple[str, datetime, datetime]:
    """Return ``(suffix, start, end)`` for the month ``offset`` months after
    ``now_utc``'s own month, as a half-open ``[start, end)`` UTC range.

    Mirrors the migration's ``_month_partition_bounds`` convention
    (`alembic/versions/2db33dea5e14_observations_current_prices_tables.py`)
    — ``offset=0`` yields ``now_utc``'s own month (current-month
    self-heal, FR-005), ``offset=1`` the following month (FR-004/AS-1).
    Month/year arithmetic is done on a 0-based month index via floor
    division/modulo (never a naive ``+1``), so it is correct across a
    Dec->Jan year rollover and regardless of a given month's length
    (Feb, FR-007). ``now_utc`` must be tz-aware UTC (FR-025).
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (UTC)")

    zero_based = now_utc.month - 1 + offset
    year = now_utc.year + zero_based // 12
    month = zero_based % 12 + 1
    start = datetime(year, month, 1, tzinfo=timezone.utc)

    next_zero_based = zero_based + 1
    next_year = now_utc.year + next_zero_based // 12
    next_month = next_zero_based % 12 + 1
    end = datetime(next_year, next_month, 1, tzinfo=timezone.utc)

    return partition_suffix(year, month), start, end


@dataclass
class RunReport:
    """Structured summary of one ``create_missing_partitions`` run
    (FR-023, data-model.md §5) — logged by the Celery task wrapper, never
    persisted."""

    tables_skipped_absent: list[str] = field(default_factory=list)
    partitions_created: list[str] = field(default_factory=list)


def _create_partition_stmt(child_name: str, parent_name: str, start: datetime, end: datetime):
    """Build the (unexecuted) idempotent partition-creation DDL statement.

    Split out from :func:`create_missing_partitions` so its rendered SQL
    can be asserted in a pure unit test without a live DB or session
    (mirrors :func:`_to_regclass_stmt`). ``start``/``end`` are always the
    first of a month (:func:`month_partition_bounds`) — fully
    code-controlled, never user input — rendered as ``YYYY-MM-DD``
    literals exactly like the migration-time
    ``_month_partition_bounds``/``op.execute`` convention (research R2).
    ``IF NOT EXISTS`` makes re-issuing this statement for an
    already-created partition a no-op (FR-006).
    """
    start_literal = start.date().isoformat()
    end_literal = end.date().isoformat()
    return text(
        f"CREATE TABLE IF NOT EXISTS {child_name} PARTITION OF {parent_name} "
        f"FOR VALUES FROM ('{start_literal}') TO ('{end_literal}')"
    )


def create_missing_partitions(
    session: Session, *, now_utc: datetime, lookahead_months: int
) -> RunReport:
    """Ensure the current + ``lookahead_months`` months' partitions exist
    for every *existing* registered table (contracts/partition-creation.md).

    For each :data:`~app_shared.maintenance.registry.PARTITIONED_TABLES`
    entry: the ``to_regclass`` existence gate (:func:`table_exists`)
    skips a registered-but-not-yet-created table (e.g. ``webhook_events``,
    FR-002) cleanly, recording it in ``tables_skipped_absent`` rather than
    raising. Otherwise, for offsets ``0..lookahead_months`` (offset 0 =
    the current month, self-healing a missing current-month partition,
    FR-005; offset 1 = next month, FR-004), a catalog pre-check
    (:func:`table_exists` on the would-be child) plus the statement's own
    ``IF NOT EXISTS`` make re-running this a no-op — no partition is
    created twice and nothing raises (FR-006, idempotent/concurrency-safe
    even against an overlapping run, contracts/partition-creation.md
    §Concurrency). No per-partition RLS DDL is issued: RLS on the
    partitioned parent already propagates to every child, current and
    future (research R2).
    """
    report = RunReport()
    for entry in PARTITIONED_TABLES:
        if not table_exists(session, entry.name):
            report.tables_skipped_absent.append(entry.name)
            continue

        for offset in range(lookahead_months + 1):
            suffix, start, end = month_partition_bounds(now_utc, offset)
            child_name = partition_name(entry.name, suffix)
            if table_exists(session, child_name):
                continue
            session.execute(_create_partition_stmt(child_name, entry.name, start, end))
            report.partitions_created.append(child_name)

    return report
