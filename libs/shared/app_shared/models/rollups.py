"""Daily rollup ORM model: variant_price_daily_rollups (SPEC-15 US2).

Per ``contracts/daily-rollup.md`` / ``data-model.md`` §1 — the single new
table this feature adds. Workspace-owned, on
:class:`~app_shared.models.base.WorkspaceScopedBase` (``workspace_id
NOT NULL``, indexed) + :class:`~app_shared.models.base.TimestampMixin`
(``created_at``/``updated_at``), registered in
:data:`app_shared.repository.WORKSPACE_OWNED_MODELS` and given
:func:`app_shared.models.rls.emit_rls_policy` in the creating Alembic
migration (``alembic/versions/<rev>_variant_price_daily_rollups.py``),
not here — this module only declares ORM shape.

**Not partitioned** (research R5): a durable, 2-year, current/summary
table, small relative to the append-heavy raw ``price_observations`` it
is aggregated from — matches the ``variant_price_states``/
``match_current_prices`` current-state convention. Closest template:
:class:`~app_shared.models.alerts.VariantPriceState`
(``alerts.py:56-95``) — the field vocabulary (``cheapest/average/
highest_competitor_price``, ``comparable_competitor_count``,
``client_price``, ``currency``, ``latest_alert_type``) is reused
verbatim so the rollup is a faithful dated snapshot of that surface, not
a new vocabulary.

``unique(workspace_id, product_variant_id, date)`` is the upsert
conflict arbiter (``ON CONFLICT ... DO UPDATE``, FR-010,
``app_shared.maintenance.rollups.run_daily_rollup``). ``product_id``/
``product_variant_id`` are soft references (plain indexed/unindexed UUID
columns, no FK) — matching §22's soft-reference philosophy, same as
every other workspace-owned table that references the catalog.
"""

from __future__ import annotations

import uuid
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import CHAR, Date, ForeignKeyConstraint, Index, Integer, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import AlertType, enum_column
from app_shared.models.base import Base, TimestampMixin, WorkspaceScopedBase
from app_shared.money import Money


class VariantPriceDailyRollup(Base, WorkspaceScopedBase, TimestampMixin):
    """``variant_price_daily_rollups`` — durable per-(workspace, variant, day) summary.

    Not partitioned (research R5); single-column PK (``id``);
    ``unique(workspace_id, product_variant_id, date)`` is the upsert
    conflict arbiter. ``comparable_competitor_count=0`` with NULL
    competitor min/avg/max is a valid row (FR-013, US2 AS-3) — a variant
    that had observations that day but none comparable/same-currency.
    """

    __tablename__ = "variant_price_daily_rollups"
    __table_args__ = (
        # The naming-convention-expanded name
        # (`uq_variant_price_daily_rollups_workspace_id_product_variant_id_date`)
        # exceeds Postgres's 63-byte identifier cap, so this uses the same
        # explicit-shorthand precedent as `dsp`/`sas`/`sdr`
        # (`app_shared.models.strategy`) / `rr` (`app_shared.models.refresh_rules`)
        # / `cpm` (`app_shared.models.competitors_matches`): `vpdr` for
        # `variant_price_daily_rollups`.
        UniqueConstraint(
            "workspace_id",
            "product_variant_id",
            "date",
            name="uq_vpdr_workspace_id_product_variant_id_date",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_variant_price_daily_rollups_workspace_id_workspaces",
        ),
        # `workspace_id` alone is already indexed via `WorkspaceScopedBase`
        # (`index=True`, naming-convention name
        # `ix_variant_price_daily_rollups_workspace_id`) — only `date`
        # needs an explicit index here, supporting the retention/coverage
        # `WHERE date >= d0 AND date < dN` range scan (R7).
        Index("ix_variant_price_daily_rollups_date", "date"),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_variant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    date: Mapped[date_type] = mapped_column(Date(), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)

    client_price: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    cheapest_competitor_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    average_competitor_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    highest_competitor_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    comparable_competitor_count: Mapped[int] = mapped_column(Integer(), nullable=False)

    latest_alert_type: Mapped[AlertType] = enum_column(AlertType, nullable=False)
