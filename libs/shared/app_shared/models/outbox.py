"""Transactional-outbox ORM model: ``outbox_messages`` (H1 hardening).

Audit risk **H1** ("Asynchronous work can be lost on worker/broker
failure", CORE_PRODUCT_PRODUCTION_READINESS_AUDIT_2026-08-15 §H1/§11):
several producers committed domain data and then enqueued a Celery task
fire-and-forget. Between the ``COMMIT`` and the broker accepting the
message there is a window in which a crash, a Redis outage, or a
container eviction silently drops the follow-up work while the source
rows look perfectly committed.

:class:`OutboxMessage` closes that window. The producer writes ONE row
into ``outbox_messages`` inside the SAME transaction as its domain
write; a separate dispatcher (:mod:`app_shared.outbox.dispatcher`)
publishes it to Celery afterwards with at-least-once semantics and
bounded retries. Either both the domain rows and the intent to publish
commit, or neither does.

Design decisions (deliberate, per the migration brief):

* **Plain table, NOT partitioned.** Every other high-volume table here
  (``price_observations``/``request_attempts``/``price_alert_events``/
  ``webhook_events``) is monthly-partitioned because it is an
  *append-only history* read by time range. ``outbox_messages`` is the
  opposite shape: a **drain-to-empty work queue**. Its steady-state size
  is "whatever has not been published yet" (normally a handful of rows),
  its hot path is a ``FOR UPDATE SKIP LOCKED`` claim on a tiny partial
  index, and its retention is achieved by DELETEing terminal rows rather
  than by dropping a whole month. Partitioning would add a partition
  scan to every claim and buy nothing, so it is deliberately omitted —
  the table is therefore also NOT registered in
  ``app_shared.maintenance.registry.PARTITIONED_TABLES``; its retention
  lives in :mod:`app_shared.outbox.reconciler`, scheduled alongside the
  existing SPEC-15 maintenance passes.
* **Workspace-owned + RLS.** Every message carries the workspace whose
  domain write produced it, so the table is on
  :class:`~app_shared.models.base.WorkspaceScopedBase`, registered in
  :data:`app_shared.repository.WORKSPACE_OWNED_MODELS`, and given
  :func:`app_shared.models.rls.emit_rls_policy` in its creating Alembic
  migration (``alembic/versions/<rev>_outbox_messages.py``), not here.
  Producers write it under the ordinary workspace-scoped session (that
  is the whole point — same transaction as the domain write); the
  cross-tenant drain/reconcile passes run on the sanctioned BYPASSRLS
  system session, exactly like ``finalize_jobs``/``run_refresh_pass``.
* **``payload`` is a soft reference bag.** JSONB task kwargs, no FKs
  beyond ``workspace_id`` (the RLS anchor) — §22's soft-reference
  philosophy, same as ``webhook_events.payload``.
* **Partial unique index on ``(workspace_id, dedup_key)`` WHERE status =
  'PENDING'.** At most one *unpublished* copy of a given logical
  message can exist, so a re-run of a producer sweep before the
  dispatcher has drained cannot double-publish. It is deliberately NOT
  a full unique constraint: dedup keys such as
  ``strategy:{profile}:{status}:{change}`` legitimately recur months
  later, and a permanent constraint would silently swallow the genuine
  later event. Producers insert with ``ON CONFLICT DO NOTHING`` against
  this index, so a collision can never abort the caller's domain
  transaction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKeyConstraint, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import OutboxStatus, enum_column
from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase

#: Name of the partial unique index used as the ``ON CONFLICT`` arbiter by
#: :func:`app_shared.outbox.writer.write_outbox_message`. Declared here (not
#: inline in the writer) so the model, the migration and the writer can
#: never drift apart.
OUTBOX_PENDING_DEDUP_INDEX = "ix_outbox_messages_pending_dedup"

#: Partial-index predicate for the claim index and the dedup arbiter. Kept
#: as a raw SQL string because it must render identically in the ORM
#: metadata, the Alembic migration, and the ``index_where`` of the writer's
#: ``ON CONFLICT`` clause.
OUTBOX_PENDING_PREDICATE = f"status = '{OutboxStatus.PENDING.value}'"


class OutboxMessage(Base, WorkspaceScopedBase, TimestampMixin):
    """``outbox_messages`` — one durable "publish this Celery task" intent.

    Lifecycle (:class:`app_shared.outbox.enums.OutboxStatus`)::

        PENDING --(publish ok)--> PUBLISHED --(retention)--> deleted
           |
           +--(attempts >= max_attempts)--> DEAD  (alertable, never auto-deleted
                                                   until the DEAD retention window)

    ``available_at`` is the earliest time the dispatcher may claim the
    row; a failed publish pushes it forward by exponential backoff, which
    is also what keeps one poison message from starving the queue.
    """

    __tablename__ = "outbox_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_outbox_messages_workspace_id_workspaces",
        ),
        # The dispatcher's claim query: `WHERE status = 'PENDING' AND
        # available_at <= now ORDER BY available_at, id FOR UPDATE SKIP
        # LOCKED`. Partial on PENDING so the index stays proportional to
        # the *backlog*, not to the table's history (mirrors
        # `ix_refresh_rules_due`, SPEC-13 research R5 / Principle VIII).
        Index(
            "ix_outbox_messages_claim",
            "available_at",
            "id",
            postgresql_where=text(OUTBOX_PENDING_PREDICATE),
        ),
        # At most one *unpublished* copy of a logical message — see the
        # module docstring for why this is partial rather than a plain
        # UniqueConstraint.
        Index(
            OUTBOX_PENDING_DEDUP_INDEX,
            "workspace_id",
            "dedup_key",
            unique=True,
            postgresql_where=text(OUTBOX_PENDING_PREDICATE),
        ),
        # Retention + operator dashboards ("how many DEAD?", "how old is
        # the oldest PUBLISHED row?").
        Index("ix_outbox_messages_status_updated_at", "status", "updated_at"),
    )

    #: Celery task name, from ``app_shared.task_names`` (never an import of
    #: ``apps/workers`` — the same producer-seam boundary as
    #: ``app_shared.messaging``).
    task_name: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Target Celery queue name (``maintenance``/``price_analysis``/...).
    queue: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The task's kwargs, exactly as they would have been passed to
    #: ``app_shared.messaging.enqueue(..., kwargs=...)``. JSON-serialisable
    #: scalars only (Celery convention) — ids are stringified by the caller.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)

    #: Optional logical identity of this message. Arbiter of the partial
    #: unique index above and threaded through to the consumer so the
    #: existing best-effort Redis dedup keeps working unchanged.
    dedup_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    status: Mapped[OutboxStatus] = enum_column(
        OutboxStatus, nullable=False, default=OutboxStatus.PENDING
    )

    #: Publish attempts made so far. Compared against
    #: ``Settings.OUTBOX_MAX_ATTEMPTS`` to decide PENDING -> DEAD.
    attempts: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)

    #: Earliest claimable time (defaults to row creation; pushed forward by
    #: exponential backoff after each failed publish).
    available_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)

    published_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)

    #: Last publish error, truncated — operator breadcrumb for DEAD rows.
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)


__all__ = [
    "OUTBOX_PENDING_DEDUP_INDEX",
    "OUTBOX_PENDING_PREDICATE",
    "OutboxMessage",
]
