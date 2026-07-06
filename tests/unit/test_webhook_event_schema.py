"""Unit tests for `WebhookEventResponse`/`WebhookEventListResponse`
(SPEC-16 US1 T018, `contracts/rest-api.md`).

Builds the response schema from a stub ORM-like object (no DB) —
mirrors `apps/api/app/schemas/alerts.py::AlertEventResponse`'s
`from_attributes=True` convention. Asserts `delivered_at` always
serializes as `null` in v1 (FR-010/SC-007), `status` round-trips as its
string value, and the `{items, next_cursor}` list envelope shape.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app_shared.enums import WebhookEventStatus, WebhookEventType

from app.schemas.webhooks import WebhookEventListResponse, WebhookEventResponse


@dataclass
class _StubWebhookEvent:
    """A stand-in for an ORM `WebhookEvent` row (no DB needed)."""

    id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    status: str
    created_at: datetime
    delivered_at: datetime | None = None


def _stub(**overrides: Any) -> _StubWebhookEvent:
    defaults: dict[str, Any] = dict(
        id=uuid.uuid4(),
        event_type=WebhookEventType.PRICE_ALERT_CREATED.value,
        payload={"product_variant_id": str(uuid.uuid4())},
        status=WebhookEventStatus.PENDING.value,
        created_at=datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc),
        delivered_at=None,
    )
    defaults.update(overrides)
    return _StubWebhookEvent(**defaults)


def test_response_serializes_delivered_at_as_null() -> None:
    stub = _stub()
    response = WebhookEventResponse.model_validate(stub)

    dumped = response.model_dump(mode="json")
    assert dumped["delivered_at"] is None


def test_response_status_is_the_string_value() -> None:
    stub = _stub(status=WebhookEventStatus.PENDING.value)
    response = WebhookEventResponse.model_validate(stub)

    assert response.status == "PENDING"
    assert isinstance(response.status, str)


def test_response_field_set_matches_contract() -> None:
    stub = _stub()
    response = WebhookEventResponse.model_validate(stub)
    dumped = response.model_dump(mode="json")

    assert set(dumped.keys()) == {
        "id",
        "event_type",
        "payload",
        "status",
        "created_at",
        "delivered_at",
    }
    assert dumped["id"] == str(stub.id)
    assert dumped["event_type"] == stub.event_type
    assert dumped["payload"] == stub.payload


def test_list_response_shape_is_items_and_next_cursor() -> None:
    stub = _stub()
    item = WebhookEventResponse.model_validate(stub)

    envelope = WebhookEventListResponse(items=[item], next_cursor="some-cursor-token")
    dumped = envelope.model_dump(mode="json")

    assert set(dumped.keys()) == {"items", "next_cursor"}
    assert dumped["next_cursor"] == "some-cursor-token"
    assert len(dumped["items"]) == 1


def test_list_response_empty_items_has_null_next_cursor() -> None:
    envelope = WebhookEventListResponse(items=[], next_cursor=None)
    dumped = envelope.model_dump(mode="json")

    assert dumped["items"] == []
    assert dumped["next_cursor"] is None
