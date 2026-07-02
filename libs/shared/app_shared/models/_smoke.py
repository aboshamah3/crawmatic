"""Demonstration / smoke table: proves the machinery end-to-end.

Per ``data-model.md`` "Entity: Demonstration / smoke table (NON-domain)"
and research.md D7: a tiny, permanent, non-domain bookkeeping table that
exercises every SPEC-02 primitive in one place — a UUIDv7 primary key
(via :class:`~app_shared.models.base.Base`), ``TIMESTAMPTZ`` columns
(via :class:`~app_shared.models.base.TimestampMixin`), the ``Money``
boundary type, a string-backed app-validated enum, and two multi-column
``UniqueConstraint``s that share a leading column — the exact case the
naming convention's ``column_0_N_name`` token exists to disambiguate
(FR-002, SC-003).

Deliberately extends ``Base`` + ``TimestampMixin`` only — **not**
:class:`~app_shared.models.base.WorkspaceScopedBase`. This table is not
workspace-owned and carries no RLS policy; it exists purely to prove the
shared machinery, not as a real domain/isolation surface (SPEC-02 stays
strictly free of real workspace tables — the first one arrives in
SPEC-03).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import RecordStatus, enum_column
from app_shared.models.base import Base, TimestampMixin
from app_shared.money import Money


class SmokeFoundation(Base, TimestampMixin):
    """Non-domain demonstration table created by the first migration."""

    __tablename__ = "_smoke_foundation"
    __table_args__ = (
        # Two multi-column uniques sharing the leading `group_key` column —
        # the disambiguation case the naming convention must render with
        # distinct names (uq__smoke_foundation_group_key_code_a vs.
        # uq__smoke_foundation_group_key_code_b), never colliding.
        UniqueConstraint("group_key", "code_a"),
        UniqueConstraint("group_key", "code_b"),
    )

    group_key: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    code_a: Mapped[str | None] = mapped_column(Text(), nullable=True)
    code_b: Mapped[str | None] = mapped_column(Text(), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    status: Mapped[RecordStatus] = enum_column(RecordStatus, nullable=False)
