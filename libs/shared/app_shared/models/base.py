"""Shared declarative base, naming convention, and correctness mixins.

Per ``contracts/models-base.md`` / research.md D2/D3/D5 (§21/§22/§32):
a single shared metadata + naming convention so every constraint name is
deterministic and collision-free (FR-002); a UUIDv7 primary key on every
table (FR-003); a ``TIMESTAMPTZ``-only timestamp mixin that forbids
naive datetimes both at the value level (bind-param guard) and
structurally (mapper-configuration guard) (FR-004); and an
RLS-ready ``WorkspaceScopedBase`` mixin (FR-007, paired with
``app_shared.models.rls.emit_rls_policy``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, MetaData, Uuid, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Mapper, mapped_column
from sqlalchemy.types import TypeDecorator

from app_shared.ids import new_uuid7

# --- Naming convention (FR-002, SC-003) -------------------------------
#
# ix/uq/fk use the built-in `column_0_N_name` token, which expands to
# *every* column in the constraint, underscore-joined — so two
# multi-column unique constraints sharing a leading column (e.g.
# uq(group_key, code_a) and uq(group_key, code_b)) get distinct names
# instead of colliding on the default `column_0_name` (first-column-only)
# token. `ck` requires an explicit CheckConstraint(name=...) — an
# intentional forcing function. Verified in research.md D2.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TZDateTime(TypeDecorator[datetime]):
    """``DateTime(timezone=True)`` that refuses naive datetimes at bind time.

    Value-level guard (research.md D3): assigning a ``datetime`` with
    ``tzinfo is None`` raises ``ValueError`` before it ever reaches the
    driver/DB, so a naive timestamp can never be stored through this
    type. Postgres renders the underlying column as ``TIMESTAMPTZ``.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError(
                "naive datetime not allowed for a TZDateTime column; "
                "pass a timezone-aware datetime (e.g. datetime.now(timezone.utc))"
            )
        return value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value


def _is_naive_datetime_type(sa_type: Any) -> bool:
    """True if ``sa_type`` is a naive (timezone-unaware) DateTime/TIMESTAMP.

    ``TZDateTime`` (and any ``DateTime(timezone=True)``) is explicitly
    NOT naive — only a bare ``DateTime()``/``DateTime(timezone=False)``
    (the default) trips this, whether declared directly or reached by
    unwrapping a ``TypeDecorator`` chain.
    """
    if isinstance(sa_type, TZDateTime):
        return False
    if isinstance(sa_type, TypeDecorator):
        return _is_naive_datetime_type(sa_type.impl)
    if isinstance(sa_type, DateTime):
        return sa_type.timezone is not True
    return False


class Base(DeclarativeBase):
    """Shared declarative base: single metadata + UUIDv7 primary key.

    Every subclass inherits an ``id`` primary key defaulting to an
    application-generated UUIDv7 (:func:`app_shared.ids.new_uuid7`),
    stored as a Postgres native ``UUID`` (FR-001, FR-003).
    """

    metadata = metadata

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid7
    )


@event.listens_for(Base, "mapper_configured", propagate=True)
def _reject_naive_datetime_columns(mapper: Mapper[Any], class_: type[Any]) -> None:
    """Structural naive-timestamp guard (FR-004, [analyze I1]).

    Fires once per mapped subclass of :class:`Base` when its mapper is
    configured (i.e. no later than the first ``configure_mappers()``
    call, which SQLAlchemy triggers automatically before the first
    query/flush that needs it). Raises ``TypeError`` if any mapped
    column's type is a naive ``DateTime``/``TIMESTAMP`` — so declaring
    a raw ``DateTime()`` column on a ``Base`` subclass fails at
    class/mapper-configuration time, not silently at runtime. Columns
    using :class:`TZDateTime` (or an explicit ``DateTime(timezone=True)``)
    are unaffected.
    """
    for column in mapper.columns:
        if _is_naive_datetime_type(column.type):
            raise TypeError(
                f"{class_.__name__}.{column.name} uses a naive (timezone-unaware) "
                "DateTime/TIMESTAMP column; use app_shared.models.base.TZDateTime "
                "(or DateTime(timezone=True)) instead — naive timestamp columns are "
                "forbidden on Base subclasses (FR-004)."
            )


class TimestampMixin:
    """``created_at`` / ``updated_at`` as non-null, UTC-aware ``TIMESTAMPTZ``.

    Both default to ``datetime.now(timezone.utc)``; ``updated_at`` also
    carries an ``onupdate`` refreshing it on every UPDATE (FR-004).
    """

    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), nullable=False, default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime(), nullable=False, default=_utc_now, onupdate=_utc_now
    )


class WorkspaceScopedBase:
    """Mixin adding an indexed, non-null ``workspace_id`` column (RLS-ready).

    No foreign key to ``workspaces`` yet — that table does not exist
    until SPEC-03, which adds the FK. Pairs with
    :func:`app_shared.models.rls.emit_rls_policy` in the same migration
    that creates a workspace-owned table (FR-007). No table in SPEC-02
    uses this mixin — the first real use is SPEC-03.
    """

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
