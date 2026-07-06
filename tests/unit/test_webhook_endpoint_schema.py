"""Unit tests for the webhook-endpoint CRUD schemas (SPEC-16 US2, T027).

Covers `WebhookEndpointCreate` defaults/`extra="forbid"`, the
`WebhookEndpointUpdate` tri-state `secret` distinction via
`model_dump(exclude_unset=True)`, and the `event_types` bound
(<= 64 entries, each <= 200 chars) from data-model.md.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.webhooks import WebhookEndpointCreate, WebhookEndpointUpdate

# --- WebhookEndpointCreate ---------------------------------------------------


def test_create_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        WebhookEndpointCreate(
            name="My integration",
            url="https://hooks.example.com/crawmatic",
            unexpected_field="nope",
        )


def test_create_defaults_enabled_true_and_event_types_empty() -> None:
    payload = WebhookEndpointCreate(
        name="My integration", url="https://hooks.example.com/x"
    )
    assert payload.enabled is True
    assert payload.event_types == []
    assert payload.secret is None


def test_create_accepts_full_field_set() -> None:
    payload = WebhookEndpointCreate(
        name="My integration",
        url="https://hooks.example.com/crawmatic",
        enabled=False,
        event_types=["price.alert.created", "scrape.job.failed"],
        secret="s3cr3t",
    )
    assert payload.enabled is False
    assert payload.event_types == ["price.alert.created", "scrape.job.failed"]
    assert payload.secret == "s3cr3t"


def test_create_event_types_rejects_more_than_64_entries() -> None:
    with pytest.raises(ValidationError):
        WebhookEndpointCreate(
            name="x",
            url="https://hooks.example.com/x",
            event_types=[f"event.{i}" for i in range(65)],
        )


def test_create_event_types_accepts_exactly_64_entries() -> None:
    payload = WebhookEndpointCreate(
        name="x",
        url="https://hooks.example.com/x",
        event_types=[f"event.{i}" for i in range(64)],
    )
    assert len(payload.event_types) == 64


def test_create_event_types_rejects_entry_over_200_chars() -> None:
    with pytest.raises(ValidationError):
        WebhookEndpointCreate(
            name="x",
            url="https://hooks.example.com/x",
            event_types=["a" * 201],
        )


def test_create_event_types_accepts_entry_of_exactly_200_chars() -> None:
    payload = WebhookEndpointCreate(
        name="x",
        url="https://hooks.example.com/x",
        event_types=["a" * 200],
    )
    assert len(payload.event_types[0]) == 200


# --- WebhookEndpointUpdate ---------------------------------------------------


def test_update_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        WebhookEndpointUpdate(unexpected_field="nope")


def test_update_all_fields_optional_and_default_unset() -> None:
    payload = WebhookEndpointUpdate()
    assert payload.model_dump(exclude_unset=True) == {}


def test_update_secret_omitted_is_absent_from_exclude_unset_dump() -> None:
    """Omitted `secret` (never assigned) must not appear in the
    `exclude_unset=True` dump the router uses -- this is how the router
    tells "unchanged" apart from "explicitly cleared"."""
    payload = WebhookEndpointUpdate(name="New name")
    updates = payload.model_dump(exclude_unset=True)
    assert "secret" not in updates
    assert updates == {"name": "New name"}


def test_update_secret_explicit_null_is_present_as_none_in_dump() -> None:
    """Explicitly setting `secret=None` DOES appear in the `exclude_unset`
    dump (as `None`) -- the router's tri-state branch treats this as
    "clear the stored secret"."""
    payload = WebhookEndpointUpdate(secret=None)
    updates = payload.model_dump(exclude_unset=True)
    assert "secret" in updates
    assert updates["secret"] is None


def test_update_secret_explicit_value_is_present_in_dump() -> None:
    payload = WebhookEndpointUpdate(secret="new-secret")
    updates = payload.model_dump(exclude_unset=True)
    assert updates["secret"] == "new-secret"


def test_update_event_types_rejects_more_than_64_entries() -> None:
    with pytest.raises(ValidationError):
        WebhookEndpointUpdate(event_types=[f"event.{i}" for i in range(65)])


def test_update_event_types_rejects_entry_over_200_chars() -> None:
    with pytest.raises(ValidationError):
        WebhookEndpointUpdate(event_types=["a" * 201])


def test_update_event_types_omitted_stays_unset() -> None:
    payload = WebhookEndpointUpdate(name="x")
    assert "event_types" not in payload.model_dump(exclude_unset=True)
