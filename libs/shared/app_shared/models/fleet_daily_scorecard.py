"""``fleet_daily_scorecard`` ORM model — one row per UTC day summarising
cost and freshness for the whole fleet (EPA D5, deep dive §12 item 9).

Re-exported so :data:`app_shared.models.metadata`/``target_metadata`` sees
the table for Alembic autogenerate/offline-render — the same reason
``rollup_completion``/``rollup_watermarks`` are declared here. Runtime
reads/writes go through :mod:`app_shared.maintenance.scorecard`, which is
free to use the ORM directly (unlike ``rollup_completion``, this table has
no keyset-batch write path to protect — one UPSERT per day is the whole
write pattern).

## Shape: global, no RLS, keyed by the day itself

Every field measures the WHOLE FLEET for one UTC day, not one workspace —
the same shape and rationale as ``rollup_watermarks``/``maintenance_cadences``/
``rollup_completion``: there is no tenant-identifying column, and a
tenant-scoped session must never see this table (it answers "how is the
business doing today", not "what did workspace X see today"). ``date`` is
the primary key and there is no surrogate ``id`` column, exactly like
``rollup_completion``.

## Every numeric column is NULLABLE, and NULL means "unmeasured"

Per the D5 acceptance criterion, a missing input is written as ``NULL``,
never ``0`` — a scorecard that reports ``0`` for a metric nobody actually
measured that day would read as "confirmed zero" when the honest answer is
"we don't know". ``app_shared.maintenance.scorecard.compute_scorecard``
enforces this discipline on write; this model only need not get in the
way of it (every column nullable, no server default that could paper over
an unmeasured metric with a magic number).
"""

from __future__ import annotations

from datetime import date as date_type

from sqlalchemy import BigInteger, Date, Float, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.models.base import Base, TimestampMixin


class FleetDailyScorecard(Base, TimestampMixin):
    """``fleet_daily_scorecard`` — one row per UTC day, fleet-wide.

    Global (no ``workspace_id``, no RLS) — see the module docstring.
    Not workspace-owned: **must not** be added to
    ``app_shared.repository.WORKSPACE_OWNED_MODELS``.
    """

    __tablename__ = "fleet_daily_scorecard"

    # This table's natural key IS its identity, same as
    # `rollup_completion` — suppress Base's inherited UUIDv7 `id`: the
    # physical table has no `id` column at all.
    id = None  # type: ignore[assignment]

    #: The UTC calendar day this row summarises.
    date: Mapped[date_type] = mapped_column(Date(), primary_key=True)

    #: Bytes the fleet's providers say we moved that day
    #: (``provider_usage_records.total_bytes``, ``occurred_at`` in the
    #: day). NULL when no provider usage export covers the day.
    provider_bytes: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    #: Railway platform CPU-seconds for the day. NULL until a service
    #: durably records the Railway usage-API figure somewhere this task
    #: can read it — see `app_shared.maintenance.scorecard` module
    #: docstring for why this is NULL on every deployment today.
    railway_cpu_seconds: Mapped[float | None] = mapped_column(Float(), nullable=True)
    #: Railway platform RAM, in GB-hours, for the day. Same caveat as
    #: `railway_cpu_seconds`.
    railway_ram_gb_hours: Mapped[float | None] = mapped_column(Float(), nullable=True)
    #: Railway platform network egress, in GB, for the day. Same caveat
    #: as `railway_cpu_seconds`.
    railway_egress_gb: Mapped[float | None] = mapped_column(Float(), nullable=True)
    #: Distinct matches whose current price actually moved forward that
    #: day (`match_current_prices.updated_at` in the day) — the
    #: product-outcome number, not attempts made.
    valid_fresh_matches: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    #: `request_attempts` made that day, per `valid_fresh_matches` that
    #: day — the amplification factor. NULL (never a fabricated ratio)
    #: when `valid_fresh_matches` is zero or unmeasured.
    attempts_per_valid_fresh: Mapped[float | None] = mapped_column(
        Float(), nullable=True
    )
    #: Fraction (0..1) of that day's top-level physical network
    #: operations whose transport was `BROWSER`.
    browser_share: Mapped[float | None] = mapped_column(Float(), nullable=True)
    #: Fraction (0..1) of that day's top-level physical network
    #: operations that were paid proxy traffic (the C6 predicate:
    #: `provider <> 'direct' AND proxy_provider_id IS NOT NULL`).
    proxied_share: Mapped[float | None] = mapped_column(Float(), nullable=True)
    #: p95 seconds a `scrape_job_targets` row spent queued before
    #: dispatch, over targets created that day.
    queue_oldest_seconds_p95: Mapped[float | None] = mapped_column(
        Float(), nullable=True
    )
    #: p95 seconds from first network contact to persisted, over targets
    #: created that day.
    persistence_lag_seconds_p95: Mapped[float | None] = mapped_column(
        Float(), nullable=True
    )
    #: Fraction (0..1) of this row's OTHER 13 metrics that came back
    #: NULL — the scorecard's own self-reported completeness. Always
    #: computed (never itself NULL) once the row is written at all.
    missing_metric_fraction: Mapped[float | None] = mapped_column(
        Float(), nullable=True
    )
    #: `cost_reservations.reserved_cost_micro_units` created that day,
    #: summed and converted to USD.
    budget_reserved_usd: Mapped[float | None] = mapped_column(Float(), nullable=True)
    #: `cost_reservations.settled_cost_micro_units` for reservations that
    #: reached `SETTLED` that day, summed and converted to USD.
    budget_settled_usd: Mapped[float | None] = mapped_column(Float(), nullable=True)
    #: GB moved on the wire by a NON-private-network backup leg that day
    #: (C10's `POST /admin/ops/backup-report`, `private_network: false`
    #: reports only — a private-network leg costs no egress by
    #: construction). NULL when no backup report was received for the
    #: day, never 0 — an absent report says nothing about egress, it
    #: says the day's freshness input is missing.
    backup_egress_gb: Mapped[float | None] = mapped_column(Float(), nullable=True)
    #: `budget_settled_usd` (in micro-USD, unrounded) divided by
    #: `valid_fresh_matches` for the day — the fleet's own unit
    #: economics. NULL when either side is zero or unmeasured.
    cost_per_valid_fresh_micro_usd: Mapped[float | None] = mapped_column(
        Float(), nullable=True
    )
