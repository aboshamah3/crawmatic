"""Guard test for SPEC-16 US2 (T026) — `WebhookEndpointResponse` must never
serialize the encrypted secret.

Mirrors `tests/unit/test_access_guards.py`'s SPEC-10
`ProxyProviderResponse`/`has_password` guard: a static `model_fields` sweep
plus an actual serialized-instance JSON check (the field-name sweep alone
would not catch a stray `model_validate(orm_obj)` call that happens to
attribute-copy extra columns onto an already-declared field set — the JSON
check exercises the real serialization path).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import app.schemas.webhooks as webhook_schemas

FORBIDDEN_SECRET_FIELDS = {"secret", "secret_encrypted", "secret_key_version"}


def test_webhook_endpoint_response_schema_never_declares_secret_fields() -> None:
    """SPEC-16 T026: `WebhookEndpointResponse` never declares `secret`,
    `secret_encrypted`, or `secret_key_version` as a field — only the
    derived `has_secret` boolean."""
    cls = webhook_schemas.WebhookEndpointResponse
    field_names = set(cls.model_fields.keys())
    leaked = field_names & FORBIDDEN_SECRET_FIELDS
    assert not leaked, f"{cls.__name__} exposes forbidden field(s): {sorted(leaked)}"
    assert "has_secret" in cls.model_fields


def test_no_webhook_response_schema_in_module_carries_secret_fields() -> None:
    """Belt-and-suspenders sweep: no `*Response` class anywhere in
    `apps/api/app/schemas/webhooks.py` (present or future) may declare a
    `secret`/`secret_encrypted`/`secret_key_version` field."""
    for name in dir(webhook_schemas):
        if not name.endswith("Response"):
            continue
        cls = getattr(webhook_schemas, name)
        model_fields = getattr(cls, "model_fields", None)
        if model_fields is None:
            continue
        leaked = set(model_fields.keys()) & FORBIDDEN_SECRET_FIELDS
        assert not leaked, f"{name} exposes forbidden field(s): {sorted(leaked)}"


@dataclass
class _StubWebhookEndpoint:
    """A minimal stand-in for the `WebhookEndpoint` ORM row -- exercises
    `_to_response` without needing a database."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    url: str
    enabled: bool
    event_types: list[str]
    secret_encrypted: str | None
    secret_key_version: int | None
    created_at: datetime
    updated_at: datetime


def _stub(
    secret_encrypted: str | None, secret_key_version: int | None
) -> _StubWebhookEndpoint:
    now = datetime.now(timezone.utc)
    return _StubWebhookEndpoint(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        name="My integration",
        url="https://hooks.example.com/crawmatic",
        enabled=True,
        event_types=["price.alert.created"],
        secret_encrypted=secret_encrypted,
        secret_key_version=secret_key_version,
        created_at=now,
        updated_at=now,
    )


def test_to_response_json_never_contains_the_secret_ciphertext() -> None:
    """SPEC-16 T026: the serialized JSON of a `WebhookEndpointResponse` built
    from an endpoint that DOES have a secret must never contain the
    ciphertext, the key version, or any `secret*` key -- only `has_secret`."""
    endpoint = _stub(
        secret_encrypted="gAAAAA-fake-fernet-ciphertext", secret_key_version=3
    )
    response = webhook_schemas._to_response(endpoint)  # type: ignore[arg-type]

    dumped = response.model_dump()
    assert "secret" not in dumped
    assert "secret_encrypted" not in dumped
    assert "secret_key_version" not in dumped
    assert dumped["has_secret"] is True

    as_json = response.model_dump_json()
    assert "gAAAAA-fake-fernet-ciphertext" not in as_json
    assert "secret_encrypted" not in as_json
    assert "secret_key_version" not in as_json


def test_to_response_has_secret_false_when_no_secret_stored() -> None:
    """`has_secret` is False when `secret_encrypted is None` -- the derived
    boolean tracks the ciphertext column exactly."""
    endpoint = _stub(secret_encrypted=None, secret_key_version=None)
    response = webhook_schemas._to_response(endpoint)  # type: ignore[arg-type]

    assert response.has_secret is False
    dumped = response.model_dump()
    assert "secret" not in dumped
    assert "secret_encrypted" not in dumped
    assert "secret_key_version" not in dumped
