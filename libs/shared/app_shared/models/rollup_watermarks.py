"""``rollup_watermarks`` ORM model -- the durable cursor that makes the
daily rollup crash-safe and catch-up-able (EPA W5.5-L2, Item A).

See :mod:`app_shared.maintenance.rollup_watermark` for the full
rationale (the gap this closes, the crash-safety ordering contract) and
for every actual read/write, which go through raw
:func:`sqlalchemy.text` statements guarded by a ``to_regclass``
capability probe -- **not** through this model. This module exists so
:data:`app_shared.models.metadata` sees the table for Alembic
autogenerate/offline-render (``target_metadata``); nothing in the
runtime path imports or instantiates :class:`RollupWatermark`.

## Shape: global, no RLS, keyed by its own natural key

Deliberately **no** ``workspace_id`` and **no** RLS -- the same shape
and rationale as ``maintenance_cadences``
(:class:`app_shared.models.maintenance_cadence.MaintenanceCadence`): a
daily rollup is one cross-tenant window for the whole deployment.

Unlike ``MaintenanceCadence`` (which keeps :class:`~app_shared.models.
base.Base`'s inherited UUIDv7 ``id`` and adds a separately-unique
``cadence_key``), this table's migration
(``05dfda7cdbb7_rollup_watermark_and_strategy_switch_audit``) makes the
cursor's own natural key -- ``key`` -- the primary key, with no
surrogate ``id`` column at all: it is a small, deployment-wide KV store
of named cursors, not an entity with its own identity separate from
what it names. ``Base.id`` is therefore suppressed below (``id =
None``) rather than left in place as a second, physically-nonexistent
PK member.
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime

from sqlalchemy import Date, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.models.base import Base, TZDateTime, TimestampMixin


class RollupWatermark(Base, TimestampMixin):
    """``rollup_watermarks`` -- one durable cursor per rollup ``key``.

    Global (no ``workspace_id``, no RLS) -- see the module docstring.
    Not workspace-owned: **must not** be added to
    ``app_shared.repository.WORKSPACE_OWNED_MODELS``.
    """

    __tablename__ = "rollup_watermarks"

    # This table's natural key IS its identity -- see the module
    # docstring. Suppress Base's inherited UUIDv7 `id`: the physical
    # table (migration 05dfda7cdbb7) has no `id` column at all.
    id = None  # type: ignore[assignment]

    #: Stable cursor identity (e.g. ``"daily_rollup"``). Renaming a key
    #: restarts that cursor from its seed exactly once.
    key: Mapped[str] = mapped_column(Text(), primary_key=True)
    #: Highest UTC day known to be fully rolled up and committed. The
    #: next window to process is always ``last_complete_date + 1 day``.
    last_complete_date: Mapped[date_type] = mapped_column(Date(), nullable=False)
    #: When the cursor last advanced (``NULL`` = seeded but never
    #: advanced).
    last_advanced_at: Mapped[datetime | None] = mapped_column(
        TZDateTime(), nullable=True
    )
    #: Monotonic count of successful advances, for "is this cursor alive".
    advance_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)


__all__ = ["RollupWatermark"]
