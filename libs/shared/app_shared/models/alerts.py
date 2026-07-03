"""Alert/price-comparison ORM models: variant_price_states,
variant_alert_states, price_alert_events (SPEC-09).

Per ``contracts/models-alerts.md`` / data-model.md — three
workspace-owned tables, all on
:class:`~app_shared.models.base.WorkspaceScopedBase`
(``workspace_id NOT NULL``, indexed), each registered in
:data:`app_shared.repository.WORKSPACE_OWNED_MODELS` and given
:func:`app_shared.models.rls.emit_rls_policy` in the creating Alembic
migration (``alembic/versions/e4a75b48360c_alerts_price_states_tables.py``),
not here — this module only declares ORM shape.

* :class:`VariantPriceState` — current price-comparison snapshot per
  variant (benchmarks, comparable count, latest alert type/severity).
  Current-state (not partitioned); ``unique(workspace_id,
  product_variant_id)`` is the upsert conflict arbiter.
* :class:`VariantAlertState` — current alert row per variant (type,
  severity, status, lifecycle timestamps). Same shape/upsert pattern as
  ``VariantPriceState``.
* :class:`PriceAlertEvent` — append-only alert-transition history.
  **Monthly-partitioned by ``created_at`` from birth** (mirrors
  ``PriceObservation.scraped_at`` — research D3, Constitution §22/§29):
  composite ``PRIMARY KEY (id, created_at)`` since Postgres requires a
  partitioned table's primary key to include the partition key. No
  ``TimestampMixin`` — ``created_at`` is declared explicitly as the PK/
  partition column.

All three carry only a real FK on ``workspace_id`` (the RLS anchor);
``product_id``/``product_variant_id``/``alert_state_id`` are **soft**
references (plain indexed/unindexed UUID columns, no FK) — matching
§22's soft-reference philosophy.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CHAR, ForeignKeyConstraint, Integer, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import (
    AlertEventType,
    AlertSeverity,
    AlertStatus,
    AlertType,
    enum_column,
)
from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase
from app_shared.money import Money


class VariantPriceState(Base, WorkspaceScopedBase, TimestampMixin):
    """``variant_price_states`` — current price-comparison snapshot per variant.

    Current-state (not partitioned); single-column PK (``id``);
    ``unique(workspace_id, product_variant_id)`` is the upsert conflict
    arbiter (``insert(...).on_conflict_do_update``, ``recompute_variant``).
    """

    __tablename__ = "variant_price_states"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "product_variant_id",
            name="uq_variant_price_states_workspace_id_product_variant_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_variant_price_states_workspace_id_workspaces",
        ),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_variant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    client_price: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)

    cheapest_competitor_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    average_competitor_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    highest_competitor_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    comparable_competitor_count: Mapped[int] = mapped_column(Integer(), nullable=False)

    latest_alert_type: Mapped[AlertType] = enum_column(AlertType, nullable=False)
    latest_alert_severity: Mapped[AlertSeverity] = enum_column(AlertSeverity, nullable=False)
    latest_alert_state_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )

    calculated_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)


class VariantAlertState(Base, WorkspaceScopedBase, TimestampMixin):
    """``variant_alert_states`` — current alert row per variant.

    Current-state (not partitioned); single-column PK (``id``);
    ``unique(workspace_id, product_variant_id)`` is the upsert conflict
    arbiter. ``status`` is ``ACTIVE`` while ``type`` is non-``NORMAL``,
    ``RESOLVED`` (with ``resolved_at`` stamped) once it returns to
    ``NORMAL``.
    """

    __tablename__ = "variant_alert_states"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "product_variant_id",
            name="uq_variant_alert_states_workspace_id_product_variant_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_variant_alert_states_workspace_id_workspaces",
        ),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_variant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    type: Mapped[AlertType] = enum_column(AlertType, nullable=False)
    severity: Mapped[AlertSeverity] = enum_column(AlertSeverity, nullable=False)
    status: Mapped[AlertStatus] = enum_column(AlertStatus, nullable=False)

    client_price: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    benchmark_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    cheapest_competitor_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    average_competitor_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)

    message: Mapped[str] = mapped_column(Text(), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class PriceAlertEvent(Base, WorkspaceScopedBase):
    """``price_alert_events`` — append-only alert-transition history. PARTITIONED.

    Monthly-partitioned by ``created_at``; composite
    ``PRIMARY KEY (id, created_at)``. A row is written **only** on a
    type/severity change (CREATED/UPDATED/RESOLVED/REOPENED) —
    ``recompute_variant`` never writes one on an UNCHANGED run (D5/D6).
    """

    __tablename__ = "price_alert_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_price_alert_events_workspace_id_workspaces",
        ),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    # PK part 2 = partition key.
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True)

    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_variant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    alert_state_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    event_type: Mapped[AlertEventType] = enum_column(AlertEventType, nullable=False)
    previous_type: Mapped[AlertType | None] = enum_column(AlertType, nullable=True)
    new_type: Mapped[AlertType] = enum_column(AlertType, nullable=False)
    previous_severity: Mapped[AlertSeverity | None] = enum_column(AlertSeverity, nullable=True)
    new_severity: Mapped[AlertSeverity] = enum_column(AlertSeverity, nullable=False)

    message: Mapped[str] = mapped_column(Text(), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), nullable=True)
