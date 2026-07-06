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

This module issues **no DDL** — partition ``CREATE`` lives in US1
(``create_missing_partitions``, T008) and partition ``DROP`` in US3
(``drop_partition``, T024). Scraping-free (Constitution I/V) — SQLAlchemy
+ stdlib only.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.orm import Session

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
