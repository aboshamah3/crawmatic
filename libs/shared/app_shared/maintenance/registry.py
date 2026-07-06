"""Partitioned-table registry (SPEC-15 US1/US3, data-model.md §2, research R3).

A fixed, code-level constant — **not** a database table. Each entry binds
a real partitioned parent table to its declared ``postgresql_partition_by``
column, whether it feeds the daily rollup verify-before-drop gate
(``feeds_rollups`` — ``price_observations`` only, FR-016), and the
``Settings`` attribute name that resolves its retention window (Principle
IV: the *set* of tables is a code constant, but retention *durations* are
DB/env-tunable).

``variant_price_daily_rollups`` is deliberately **not** registered here —
it is not partitioned; its 2-year retention is a separate age-based row
policy (research R7) applied directly in ``maintenance.retention``.

Scraping-free (Constitution I/V) — imports nothing beyond stdlib +
``app_shared.config``.
"""

from __future__ import annotations

from dataclasses import dataclass

from app_shared.config import Settings


@dataclass(frozen=True)
class PartitionedTable:
    """One partitioned parent table and how the maintenance jobs treat it.

    Attributes:
        name: The parent table's name (e.g. ``"price_observations"``).
        partition_key: The ``RANGE``-partitioned column (e.g. ``"scraped_at"``).
        feeds_rollups: ``True`` only for ``price_observations`` — retention
            for this table must verify daily-rollup coverage before
            dropping a partition (FR-016). ``False`` entries drop by age
            alone (FR-019).
        retention_setting: The ``Settings`` attribute name whose value (an
            ``int`` day count) is this table's retention window (FR-017).
            Resolved via :func:`retention_days`, never hardcoded.
    """

    name: str
    partition_key: str
    feeds_rollups: bool
    retention_setting: str


PARTITIONED_TABLES: tuple[PartitionedTable, ...] = (
    PartitionedTable(
        name="price_observations",
        partition_key="scraped_at",
        feeds_rollups=True,
        retention_setting="RETENTION_PRICE_OBSERVATIONS_DAYS",
    ),
    PartitionedTable(
        name="request_attempts",
        partition_key="created_at",
        feeds_rollups=False,
        retention_setting="RETENTION_REQUEST_ATTEMPTS_DAYS",
    ),
    PartitionedTable(
        name="price_alert_events",
        partition_key="created_at",
        feeds_rollups=False,
        retention_setting="RETENTION_PRICE_ALERT_EVENTS_DAYS",
    ),
    PartitionedTable(
        # Registered ahead of its own migration (SPEC-16) — deliberately
        # absent in this build. `table_exists` (R4) skips it cleanly
        # (FR-002) until it lands.
        name="webhook_events",
        partition_key="created_at",
        feeds_rollups=False,
        retention_setting="RETENTION_WEBHOOK_EVENTS_DAYS",
    ),
)


def retention_days(entry: PartitionedTable, settings: Settings) -> int:
    """Resolve ``entry``'s retention window, in days, from ``settings``.

    Looks up ``entry.retention_setting`` as a ``Settings`` attribute name
    (e.g. ``"RETENTION_PRICE_OBSERVATIONS_DAYS"``) rather than hardcoding
    a duration, so the window stays DB/env-tunable (Principle IV, FR-017).
    """
    return getattr(settings, entry.retention_setting)
