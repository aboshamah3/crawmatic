"""Cursor pagination tests (SPEC-04 T011, FR-015/SC-009).

Pure stdlib/SQLAlchemy-expression assertions — no database. Covers
`encode_cursor`/`decode_cursor` round-trip, `InvalidCursor` on malformed
input, `clamp_limit` clamping, `keyset_predicate` rendering, and the
`paginate` envelope builder's more/none branches ([analyze F3], SC-009).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app_shared.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidCursor,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    keyset_predicate,
    paginate,
)


@dataclass
class _Row:
    created_at: datetime
    id: uuid.UUID


def _row(seconds: int) -> _Row:
    return _Row(
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds),
        id=uuid.uuid4(),
    )


# --- encode/decode round-trip --------------------------------------------


def test_encode_decode_round_trip_preserves_created_at_and_id() -> None:
    created_at = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    id_ = uuid.uuid4()

    token = encode_cursor(created_at, id_)
    decoded_created_at, decoded_id = decode_cursor(token)

    assert decoded_created_at == created_at
    assert decoded_id == id_


def test_encode_cursor_is_opaque_base64url() -> None:
    token = encode_cursor(datetime.now(timezone.utc), uuid.uuid4())
    # base64url alphabet only (no '+', '/'; padding stripped).
    assert "+" not in token
    assert "/" not in token
    assert "=" not in token


@pytest.mark.parametrize(
    "garbage",
    [
        "not-base64!!!",
        "",
        "aGVsbG8",  # valid base64 ("hello") but not our JSON shape
        "e30",  # base64 for "{}" — valid JSON, missing keys
    ],
)
def test_decode_cursor_raises_invalid_cursor_on_malformed_input(garbage: str) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(garbage)


def test_invalid_cursor_is_a_value_error_subclass() -> None:
    assert issubclass(InvalidCursor, ValueError)


# --- clamp_limit -----------------------------------------------------------


def test_clamp_limit_none_defaults_to_50() -> None:
    assert clamp_limit(None) == DEFAULT_LIMIT == 50


def test_clamp_limit_passthrough_within_bounds() -> None:
    assert clamp_limit(10) == 10


def test_clamp_limit_caps_at_500() -> None:
    assert clamp_limit(9999) == MAX_LIMIT == 500


def test_clamp_limit_floors_at_1() -> None:
    assert clamp_limit(0) == 1


def test_clamp_limit_floors_negative_at_1() -> None:
    assert clamp_limit(-5) == 1


# --- keyset_predicate --------------------------------------------------


def test_keyset_predicate_renders_tuple_comparison() -> None:
    # Plain Core Table (not ORM declarative mapping) — keyset_predicate
    # only needs `model.created_at`/`model.id` to be SQLAlchemy column
    # expressions, so a lightweight namespace over Table.c is sufficient
    # and avoids ORM mapper-configuration machinery entirely.
    from sqlalchemy import Column, DateTime, MetaData, Table, Uuid

    metadata = MetaData()
    fake_table = Table(
        "fake_model",
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("created_at", DateTime(timezone=True)),
    )

    class _Model:
        id = fake_table.c.id
        created_at = fake_table.c.created_at

    after = (datetime(2026, 1, 1, tzinfo=timezone.utc), uuid.uuid4())
    predicate = keyset_predicate(_Model, after)
    compiled = str(predicate.compile(compile_kwargs={"literal_binds": False}))

    assert "created_at" in compiled
    assert "id" in compiled
    assert ">" in compiled


# --- paginate envelope: more/none branches ([analyze F3], SC-009) -------


def test_paginate_more_rows_sets_next_cursor_and_trims_to_limit() -> None:
    rows = [_row(i) for i in range(6)]  # limit + 1 = 6, limit = 5
    result = paginate(rows, limit=5)

    assert len(result["items"]) == 5
    assert result["items"] == rows[:5]
    assert result["next_cursor"] is not None

    decoded_created_at, decoded_id = decode_cursor(result["next_cursor"])
    assert decoded_created_at == rows[4].created_at
    assert decoded_id == rows[4].id


def test_paginate_exactly_limit_rows_sets_next_cursor_none() -> None:
    rows = [_row(i) for i in range(5)]
    result = paginate(rows, limit=5)

    assert len(result["items"]) == 5
    assert result["next_cursor"] is None


def test_paginate_fewer_than_limit_rows_sets_next_cursor_none() -> None:
    rows = [_row(i) for i in range(2)]
    result = paginate(rows, limit=5)

    assert len(result["items"]) == 2
    assert result["next_cursor"] is None


def test_paginate_empty_rows() -> None:
    result = paginate([], limit=5)
    assert result["items"] == []
    assert result["next_cursor"] is None
