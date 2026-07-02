"""String-backed enum tests (FR-006, [analyze A2]).

``enum_column`` must render to a plain ``String``/``VARCHAR`` column —
never a Postgres-native ``ENUM`` and never SQLAlchemy's ``Enum`` type —
with membership validated in the application layer.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects import postgresql

from app_shared.enums import RecordStatus, StrEnum, enum_column


def test_record_status_members_carry_their_string_value() -> None:
    assert RecordStatus.ACTIVE.value == "active"
    assert RecordStatus.ARCHIVED.value == "archived"
    assert str(RecordStatus.ACTIVE) == "active"
    assert RecordStatus.ACTIVE == "active"  # str-backed equality
    assert issubclass(RecordStatus, StrEnum)


def test_enum_column_renders_plain_string_not_native_enum() -> None:
    mapped = enum_column(RecordStatus, nullable=False)
    column = mapped.column

    # Never SQLAlchemy's Enum type ...
    assert not isinstance(column.type, SAEnum)

    # ... and never a Postgres-native ENUM at the DDL level — a plain VARCHAR.
    rendered = column.type.compile(dialect=postgresql.dialect())
    assert "ENUM" not in rendered.upper()
    assert "VARCHAR" in rendered.upper()


def test_enum_column_stores_and_reads_string_value() -> None:
    mapped = enum_column(RecordStatus)
    column_type = mapped.column.type

    bound = column_type.process_bind_param(RecordStatus.ACTIVE, dialect=None)
    assert bound == "active"
    assert isinstance(bound, str)

    result = column_type.process_result_value("archived", dialect=None)
    assert result == RecordStatus.ARCHIVED


def test_enum_column_rejects_out_of_set_value() -> None:
    mapped = enum_column(RecordStatus)
    column_type = mapped.column.type

    with pytest.raises(ValueError):
        column_type.process_bind_param("not_a_real_status", dialect=None)


def test_enum_column_allows_none_for_nullable_columns() -> None:
    mapped = enum_column(RecordStatus, nullable=True)
    column_type = mapped.column.type

    assert column_type.process_bind_param(None, dialect=None) is None
    assert column_type.process_result_value(None, dialect=None) is None
