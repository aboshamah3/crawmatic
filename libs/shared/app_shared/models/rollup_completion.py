"""``rollup_completion`` ORM model -- the per-day checkpoint that makes a
daily rollup resumable and tells retention whether a day is finished
(EPA C7, plan F12).

See :mod:`app_shared.maintenance.rollup_sql` for the rationale and for
every actual read/write, which go through raw :func:`sqlalchemy.text`
statements guarded by a ``to_regclass`` capability probe -- **not**
through this model. This module exists so
:data:`app_shared.models.metadata` sees the table for Alembic
autogenerate/offline-render (``target_metadata``); nothing in the
runtime path imports or instantiates :class:`RollupCompletion`.

## Two readers, two different questions

* :func:`app_shared.maintenance.rollups.run_daily_rollup` reads
  ``last_key_workspace_id``/``last_key_variant_id`` to resume a day's
  keyset walk where the last committed batch stopped.
* The C8 retention gate reads ``complete``: a day whose rollup is not
  finished must keep its ``price_observations`` partition, because
  dropping it would destroy the only source the remaining rollup rows
  could ever be derived from.

## Shape: global, no RLS, keyed by the day itself

Deliberately **no** ``workspace_id`` and **no** RLS -- one rollup day is
one cross-tenant window for the whole deployment, exactly like
``rollup_watermarks`` and ``maintenance_cadences``. And like
``rollup_watermarks`` (and unlike ``maintenance_cadences``), the natural
key IS the identity: ``date`` is the primary key and there is no
surrogate ``id`` column at all, so :class:`~app_shared.models.base.Base`'s
inherited UUIDv7 ``id`` is suppressed below rather than left in place as
a second, physically-nonexistent PK member.

``rollup_completion`` is a distinct table from ``rollup_watermarks``
rather than more columns on it because the two say different things:
the watermark is ONE row naming how far the cadence has swept, while
this is ONE ROW PER DAY recording that day's own completeness -- which
is what a retention gate must ask about a specific day it is considering
dropping, possibly long after the sweep moved past it.
"""

from __future__ import annotations

import uuid
from datetime import date as date_type

from sqlalchemy import Boolean, Date, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.models.base import Base, TimestampMixin


class RollupCompletion(Base, TimestampMixin):
    """``rollup_completion`` -- one row per rolled-up UTC day.

    Global (no ``workspace_id``, no RLS) -- see the module docstring.
    Not workspace-owned: **must not** be added to
    ``app_shared.repository.WORKSPACE_OWNED_MODELS``.
    """

    __tablename__ = "rollup_completion"

    # This table's natural key IS its identity -- see the module
    # docstring. Suppress Base's inherited UUIDv7 `id`: the physical
    # table (migration b3f0c95a7d21) has no `id` column at all.
    id = None  # type: ignore[assignment]

    #: The UTC calendar day this row describes -- the same day
    #: ``variant_price_daily_rollups.date`` carries.
    date: Mapped[date_type] = mapped_column(Date(), primary_key=True)

    #: Keyset cursor: the highest ``(workspace_id, product_variant_id)``
    #: pair the last COMMITTED batch covered. ``NULL`` means "start from
    #: the beginning" -- either the day has not begun or it was reset by
    #: a deliberate recompute. Soft references (plain UUID columns, no
    #: FK): the cursor is a position in a scan, not a relationship, and
    #: deleting a workspace must not delete the evidence of how far a
    #: past day's rollup got.
    last_key_workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    last_key_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )

    #: ``True`` iff every (workspace, variant) pair with an observation
    #: on ``date`` has a ``variant_price_daily_rollups`` row. The C8
    #: retention gate's input. ``server_default='false'`` so a row can
    #: never be born accidentally claiming a day is finished.
    complete: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)


__all__ = ["RollupCompletion"]
