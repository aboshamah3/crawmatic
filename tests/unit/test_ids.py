"""UUIDv7 id-helper tests (FR-003, SC-002 id-part)."""

from __future__ import annotations

import uuid

from app_shared.ids import new_uuid7


def test_new_uuid7_returns_stdlib_uuid_version_7() -> None:
    value = new_uuid7()
    assert isinstance(value, uuid.UUID)
    assert value.version == 7


def test_new_uuid7_sequence_is_time_ordered() -> None:
    values = [new_uuid7() for _ in range(25)]
    as_strings = [str(v) for v in values]
    assert as_strings == sorted(as_strings)
