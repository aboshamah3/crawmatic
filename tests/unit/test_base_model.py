"""Base/TimestampMixin correctness tests (FR-004, SC-002 ts-part, [analyze I1]).

Covers three layers of the naive-timestamp guard:

1. Value-level: ``TZDateTime.process_bind_param`` raises on a naive
   ``datetime`` and accepts an aware one.
2. Shape: a ``Base`` + ``TimestampMixin`` model has a UUIDv7 ``id`` PK
   and ``created_at``/``updated_at`` columns that render ``TIMESTAMPTZ``.
3. Structural: defining a ``Base`` subclass with a raw naive
   ``DateTime()`` column raises ``TypeError`` at mapper-configuration
   time (the guard registered in ``app_shared.models.base``), while an
   explicit ``DateTime(timezone=True)`` column is accepted.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import DateTime
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, configure_mappers, mapped_column

from app_shared.ids import new_uuid7
from app_shared.models import Base, TimestampMixin
from app_shared.models.base import TZDateTime


# --- 1. Value-level guard -------------------------------------------------


def test_tzdatetime_rejects_naive_datetime() -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError):
        TZDateTime().process_bind_param(naive, dialect=None)


def test_tzdatetime_accepts_aware_datetime() -> None:
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert TZDateTime().process_bind_param(aware, dialect=None) == aware


def test_tzdatetime_passes_none_through() -> None:
    assert TZDateTime().process_bind_param(None, dialect=None) is None


# --- 2. Shape: Base + TimestampMixin --------------------------------------


class _SmokeTimestamped(Base, TimestampMixin):
    __tablename__ = "test_base_model_smoke_timestamped"


def test_base_model_has_uuid7_pk_and_tzaware_timestamp_columns() -> None:
    table = _SmokeTimestamped.__table__

    assert table.c.id.primary_key is True
    assert table.c.id.default is not None
    # SQLAlchemy wraps a zero-arg default callable in a context-accepting
    # lambda (sqlalchemy.sql.schema.ColumnDefault._maybe_wrap_callable),
    # so identity comparison against new_uuid7 isn't meaningful here —
    # assert it's the same function by name/module and behavior instead.
    default_arg = table.c.id.default.arg
    assert default_arg.__module__ == new_uuid7.__module__
    assert default_arg.__name__ == new_uuid7.__name__
    generated = default_arg(None)
    assert isinstance(generated, uuid.UUID)
    assert generated.version == 7

    for col_name in ("created_at", "updated_at"):
        column = table.c[col_name]
        assert column.nullable is False
        rendered = column.type.compile(dialect=postgresql.dialect())
        assert "TIMESTAMP" in rendered.upper()
        assert "TIME ZONE" in rendered.upper()

    assert table.c.updated_at.onupdate is not None


# --- 3. Structural guard ---------------------------------------------------


def test_structural_guard_rejects_naive_datetime_column_at_mapper_configuration() -> None:
    class _BadNaiveTimestamp(Base):
        __tablename__ = "test_base_model_bad_naive_timestamp"

        created_at: Mapped[datetime] = mapped_column(DateTime())

    with pytest.raises(TypeError, match="naive"):
        configure_mappers()


def test_structural_guard_allows_explicit_tzaware_datetime_column() -> None:
    class _GoodExplicitTzAware(Base):
        __tablename__ = "test_base_model_good_explicit_tzaware"

        created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Must not raise: DateTime(timezone=True) is structurally tz-aware,
    # even though it isn't the TZDateTime wrapper type.
    configure_mappers()
