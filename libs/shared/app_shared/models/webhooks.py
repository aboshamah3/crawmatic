"""Webhook ORM models: webhook_endpoints, webhook_events (SPEC-16).

Per ``data-model.md`` / ``contracts/rest-api.md`` / ``contracts/events.md``
— two workspace-owned tables, both on
:class:`~app_shared.models.base.WorkspaceScopedBase` (``workspace_id NOT
NULL``, indexed), each registered in
:data:`app_shared.repository.WORKSPACE_OWNED_MODELS` and given
:func:`app_shared.models.rls.emit_rls_policy` in the creating Alembic
migration (``alembic/versions/<rev>_webhook_events_and_endpoints.py``),
not here — this module only declares ORM shape.

* :class:`WebhookEndpoint` — plain (not partitioned) tenant CRUD table
  recording where webhooks will *eventually* be delivered (mirrors
  ``refresh_rules``, SPEC-13). ``url`` is SSRF-validated at the API
  layer via the existing ``app_shared.url_safety.validate_competitor_url``
  (no second validator). ``secret_encrypted``/``secret_key_version``
  mirror the ``ProxyProvider`` versioned-Fernet convention (SPEC-10);
  the plaintext secret is never stored or returned.
* :class:`WebhookEvent` — append-only domain-change history. **Monthly-
  partitioned by ``created_at`` from birth** (mirrors
  ``PriceAlertEvent``, SPEC-09, research R3): composite ``PRIMARY KEY
  (id, created_at)`` since Postgres requires a partitioned table's
  primary key to include the partition key. No ``TimestampMixin`` —
  ``created_at`` is declared explicitly as the PK/partition column.

``WebhookEvent`` carries only a real FK on ``workspace_id`` (the RLS
anchor); every other id referenced from its ``payload`` (variant,
product, job, strategy profile, alert state) is a **soft** reference
(no FK), matching §22's soft-reference philosophy and FR-019 (readers
tolerate references into dropped/expired partitions).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import WebhookEventStatus
from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase


class WebhookEndpoint(Base, WorkspaceScopedBase, TimestampMixin):
    """``webhook_endpoints`` — a workspace's registered delivery target.

    No delivery in v1 (FR-010) — this table only records intent
    (``url``, ``event_types`` subscription list, optional encrypted
    ``secret``). Single-column ``id`` PK; only FK is ``workspace_id``.
    """

    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_webhook_endpoints_workspace_id_workspaces",
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(Text(), nullable=False)

    # Versioned Fernet ciphertext (mirrors ProxyProvider.password_encrypted/
    # password_key_version, SPEC-10). Unused in v1 (no delivery/signing) —
    # FR-005/FR-010. Never returned raw; API exposes only a derived
    # `has_secret` boolean.
    secret_encrypted: Mapped[str | None] = mapped_column(Text(), nullable=True)
    secret_key_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    event_types: Mapped[list[str]] = mapped_column(JSONB(), nullable=False, default=list)


class WebhookEvent(Base, WorkspaceScopedBase):
    """``webhook_events`` — append-only domain-change history. PARTITIONED.

    Monthly-partitioned by ``created_at``; composite ``PRIMARY KEY (id,
    created_at)``. Written by the ``create_webhook_event`` Celery task
    (fire-and-forget, post-commit) at the three producer seams (SPEC-09
    alerts, SPEC-08 jobs, SPEC-12 strategy). ``status`` is always
    ``PENDING`` and ``delivered_at`` always ``NULL`` in v1 (FR-010/
    FR-011, SC-007) — both are reserved for the future delivery
    feature.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_webhook_events_workspace_id_workspaces",
        ),
        Index(
            "ix_webhook_events_ws_created_id",
            "workspace_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_webhook_events_ws_type_created",
            "workspace_id",
            "event_type",
            "created_at",
        ),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    # PK part 2 = partition key.
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True)

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WebhookEventStatus.PENDING
    )
    delivered_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
