"""Observation/current-price ORM models: price_observations, request_attempts,
match_current_prices (SPEC-07).

Per ``contracts/models-observations.md`` / ``data-model.md`` — three
workspace-owned tables, all on
:class:`~app_shared.models.base.WorkspaceScopedBase`
(``workspace_id NOT NULL``, indexed), each registered in
:data:`app_shared.repository.WORKSPACE_OWNED_MODELS` and given
:func:`app_shared.models.rls.emit_rls_policy` in the creating Alembic
migration
(``alembic/versions/<rev>_observations_current_prices_tables.py``), not
here — this module only declares ORM shape.

* :class:`PriceObservation` — immutable record of one extraction-attempt
  result. **First partitioned table in the repo** (research D3,
  Constitution §22/§29): monthly-partitioned by ``scraped_at`` via
  ``__table_args__``'s ``postgresql_partition_by``, with ``scraped_at``
  declared ``primary_key=True`` alongside the inherited ``id`` so the
  composite ``PRIMARY KEY (id, scraped_at)`` satisfies Postgres's rule
  that a partitioned table's primary key must include the partition
  key.
* :class:`RequestAttempt` — audit record of one HTTP fetch attempt.
  Same partitioning shape, partitioned by ``created_at``.
* :class:`MatchCurrentPrice` — current-state (not partitioned) latest
  price snapshot per match, ``unique(workspace_id, match_id)`` as the
  upsert conflict arbiter.

All three carry only a real FK on ``workspace_id`` (the RLS anchor);
``match_id``/``product_id``/``product_variant_id``/``competitor_id``/
``scrape_job_id``/``observation_id`` are **soft** references (plain
indexed UUID columns, no FK) — matching §22's soft-reference philosophy
and avoiding FK-into/among-partitioned-table complications with
retention-by-drop (later spec).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    Boolean,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import (
    AccessMethod,
    ExtractionMethod,
    ScrapeErrorCode,
    StockStatus,
    enum_column,
)
from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase
from app_shared.money import Money


class PriceObservation(Base, WorkspaceScopedBase):
    """``price_observations`` — immutable extraction-attempt result. PARTITIONED.

    Monthly-partitioned by ``scraped_at``; composite
    ``PRIMARY KEY (id, scraped_at)``. ``success=False`` on a failure/
    rejection observation (``price``/``currency``/etc. left ``NULL``);
    ``comparable=False`` iff ``error_code=CURRENCY_MISMATCH`` (still
    saved, excluded from comparison, no FX — Principle VII).
    """

    __tablename__ = "price_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_price_observations_workspace_id_workspaces",
        ),
        {"postgresql_partition_by": "RANGE (scraped_at)"},
    )

    # PK part 2 = partition key (Postgres requires the partition key be
    # part of the primary key on a partitioned table).
    scraped_at: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True)

    match_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_variant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    scrape_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    old_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    stock_status: Mapped[StockStatus | None] = enum_column(StockStatus, nullable=True)
    raw_title: Mapped[str | None] = mapped_column(Text(), nullable=True)

    success: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    comparable: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    error_code: Mapped[ScrapeErrorCode | None] = enum_column(ScrapeErrorCode, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)

    extraction_method: Mapped[ExtractionMethod | None] = enum_column(
        ExtractionMethod, nullable=True
    )
    # A confidence score in [0, 1] — plain Numeric(5,4), never Money.
    extraction_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    selector_used: Mapped[str | None] = mapped_column(Text(), nullable=True)


class RequestAttempt(Base, WorkspaceScopedBase):
    """``request_attempts`` — audit record of one HTTP fetch attempt. PARTITIONED.

    Monthly-partitioned by ``created_at``; composite
    ``PRIMARY KEY (id, created_at)``. Exactly one row is written per
    attempted target (FR-013).
    """

    __tablename__ = "request_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_request_attempts_workspace_id_workspaces",
        ),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    # PK part 2 = partition key.
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True)

    scrape_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    match_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    url: Mapped[str] = mapped_column(Text(), nullable=False)
    access_method: Mapped[AccessMethod] = enum_column(AccessMethod, nullable=False)
    proxy_provider_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    proxy_country: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    response_time_ms: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    error_code: Mapped[ScrapeErrorCode | None] = enum_column(ScrapeErrorCode, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)


class MatchCurrentPrice(Base, WorkspaceScopedBase, TimestampMixin):
    """``match_current_prices`` — latest-known price snapshot per match.

    Current-state (not partitioned); single-column PK (``id``);
    ``unique(workspace_id, match_id)`` is the upsert conflict arbiter
    (``insert(...).on_conflict_do_update``, the scraping-side batched
    persistence pipeline). A failure observation never overwrites the
    current price (FR-014).
    """

    __tablename__ = "match_current_prices"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "match_id",
            name="uq_match_current_prices_workspace_id_match_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_match_current_prices_workspace_id_workspaces",
        ),
    )

    match_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_variant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    competitor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    old_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    stock_status: Mapped[StockStatus | None] = enum_column(StockStatus, nullable=True)
    comparable: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    # Soft ref to the winning price_observations row — no FK (may dangle
    # after a retention-by-drop partition removal, §22).
    observation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    error_code: Mapped[ScrapeErrorCode | None] = enum_column(ScrapeErrorCode, nullable=True)
    extraction_method: Mapped[ExtractionMethod | None] = enum_column(
        ExtractionMethod, nullable=True
    )
    extraction_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    scraped_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
