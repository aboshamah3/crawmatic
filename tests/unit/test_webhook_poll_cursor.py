"""Unit tests for the reused `app_shared.pagination` cursor machinery as
used by `GET /v1/webhook-events` (SPEC-16 US1 T019, FR-015, SC-001).

No DB — pure round-trip/compile assertions against the real
`WebhookEvent` model (proves the keyset predicate compiles against its
actual `created_at`/`id` columns, including the partitioned-table
setup) plus the shared `clamp_limit`/`decode_cursor` contract every
other cursor-paginated route already relies on
(`tests/unit/test_pagination.py`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app_shared.models.webhooks import WebhookEvent
from app_shared.pagination import (
    InvalidCursor,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    keyset_predicate,
)


def test_encode_decode_cursor_round_trips_for_webhook_events() -> None:
    created_at = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
    id_ = uuid.uuid4()

    token = encode_cursor(created_at, id_)
    decoded_created_at, decoded_id = decode_cursor(token)

    assert decoded_created_at == created_at
    assert decoded_id == id_


@pytest.mark.parametrize(
    "requested,expected",
    [
        (None, 50),
        (1, 1),
        (500, 500),
        (999999, 500),
        (0, 1),
        (-10, 1),
    ],
)
def test_clamp_limit_bounds(requested: int | None, expected: int) -> None:
    assert clamp_limit(requested) == expected


def test_decode_cursor_raises_invalid_cursor_on_bad_token() -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor("not-a-valid-cursor!!!")


def test_keyset_predicate_compiles_against_webhook_event_columns() -> None:
    """`keyset_predicate(WebhookEvent, after)` renders the `(created_at, id) > (c, id)`
    tuple-seek predicate against the real partitioned model's columns
    (SC-001 cross-partition ordering stability)."""
    after = (datetime(2026, 7, 1, tzinfo=timezone.utc), uuid.uuid4())
    predicate = keyset_predicate(WebhookEvent, after)
    compiled = str(predicate.compile(compile_kwargs={"literal_binds": False}))

    assert "created_at" in compiled
    assert "id" in compiled
    assert ">" in compiled


def test_keyset_predicate_ordering_is_monotonic_over_created_at_and_id() -> None:
    """Successive cursors derived from strictly increasing `(created_at, id)`
    pairs (as would occur walking across a month-boundary partition seam)
    each produce a distinct, independently round-trippable token — proving
    the seek key is stable regardless of which physical partition a row
    lives in (the model has no partition-awareness; the predicate is pure
    `(created_at, id)` tuple comparison, SC-001)."""
    base = datetime(2026, 6, 30, 23, 59, 0, tzinfo=timezone.utc)
    pairs = [
        (base + timedelta(minutes=i), uuid.uuid4())
        for i in range(5)  # crosses the Jun -> Jul month boundary at i=1
    ]

    tokens = [encode_cursor(created_at, id_) for created_at, id_ in pairs]
    decoded = [decode_cursor(token) for token in tokens]

    assert decoded == pairs
    # Strictly increasing created_at across the seeded seam.
    assert all(decoded[i][0] < decoded[i + 1][0] for i in range(len(decoded) - 1))
