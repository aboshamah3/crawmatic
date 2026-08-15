"""Producer side of the transactional outbox (audit risk H1).

``write_outbox_message(session, ...)`` is the drop-in replacement for
``app_shared.messaging.enqueue(...)`` at every seam that previously
committed domain data and *then* fired a Celery task. The difference is
the whole point:

* ``enqueue`` talks to Redis. If Redis is down, or the process dies
  between ``COMMIT`` and ``send_task``, the follow-up work is gone while
  the domain rows look perfectly committed.
* ``write_outbox_message`` performs one INSERT **in the caller's own,
  not-yet-committed transaction**. If the caller rolls back, the message
  vanishes with the domain write (no orphan publish). If the caller
  commits, the message is as durable as the data that caused it, and
  :mod:`app_shared.outbox.dispatcher` will publish it — now, or after the
  next restart, or after Redis comes back.

The insert uses ``ON CONFLICT DO NOTHING`` against the partial unique
index ``(workspace_id, dedup_key) WHERE status = 'PENDING'``
(:data:`app_shared.models.outbox.OUTBOX_PENDING_DEDUP_INDEX`). Two
properties follow, both deliberate:

1. a producer sweep that re-runs before the dispatcher has drained
   cannot enqueue the same logical message twice;
2. a dedup collision can never raise, so it can never abort the caller's
   domain transaction — the failure mode a naive ``UniqueConstraint``
   would introduce into paths like ``finalize_jobs``.

This module never imports celery/scrapy/fastapi (see
``tests/unit/test_import_boundaries.py``): the producer seam is
deliberately a plain DB write, and the Celery dependency lives only in
the dispatcher.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from app_shared.enums import OutboxStatus
from app_shared.ids import new_uuid7
from app_shared.models.outbox import OUTBOX_PENDING_PREDICATE, OutboxMessage

__all__ = ["write_outbox_message"]


def write_outbox_message(
    session: Session,
    *,
    workspace_id: uuid.UUID | str,
    task_name: str,
    queue: str,
    kwargs: dict[str, Any] | None = None,
    dedup_key: str | None = None,
    now: datetime | None = None,
    message_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Record "publish ``task_name`` to ``queue``" in ``session``'s transaction.

    Args:
        session: the caller's session — **not** committed or flushed to a
            new transaction here. The message becomes durable exactly when
            the caller commits, and disappears if the caller rolls back.
        workspace_id: the workspace whose domain write produced this
            message (the RLS anchor; also what the consumer scopes to).
        task_name: a constant from ``app_shared.task_names``.
        queue: the target Celery queue.
        kwargs: JSON-serialisable task kwargs, exactly as they would have
            been passed to ``app_shared.messaging.enqueue``.
        dedup_key: optional logical identity. When supplied, at most one
            PENDING row can carry it per workspace (ON CONFLICT DO
            NOTHING); it is also threaded to the consumer so existing
            best-effort Redis dedup keeps working.
        now: injectable clock (tests); defaults to ``datetime.now(utc)``.
        message_id: pre-chosen row id. Supply this when the *consumer*
            needs a stable idempotency key: the caller mints the id, puts
            it inside ``kwargs``, and passes it here, so every redelivery
            of this message carries the same identity and the consumer
            can collapse replays with an ``ON CONFLICT DO NOTHING``. (The
            dispatcher stays generic — it publishes ``payload`` verbatim
            and knows nothing about any task's idempotency scheme.)

    Returns:
        The id this call assigned. Note it is returned even when the
        insert was a dedup no-op — callers treat the id as "the logical
        message identity", never as proof a new row was created; the
        dispatcher is the only component that needs to know.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    message_id = message_id if message_id is not None else new_uuid7()

    values = {
        "id": message_id,
        "workspace_id": (
            workspace_id if isinstance(workspace_id, uuid.UUID) else uuid.UUID(str(workspace_id))
        ),
        "created_at": moment,
        "updated_at": moment,
        "task_name": task_name,
        "queue": queue,
        "payload": dict(kwargs or {}),
        "dedup_key": dedup_key,
        "status": OutboxStatus.PENDING.value,
        "attempts": 0,
        "available_at": moment,
        "published_at": None,
        "last_error": None,
    }

    stmt = pg_insert(OutboxMessage).values(**values)
    if dedup_key is not None:
        # Arbiter = the partial unique index; `index_where` must match the
        # index predicate exactly or Postgres cannot infer the arbiter.
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["workspace_id", "dedup_key"],
            index_where=text(OUTBOX_PENDING_PREDICATE),
        )
    session.execute(stmt)
    return message_id
