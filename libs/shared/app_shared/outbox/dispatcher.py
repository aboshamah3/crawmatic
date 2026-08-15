"""Consumer side of the transactional outbox: the drain pass (audit H1).

``drain_outbox`` claims PENDING ``outbox_messages`` and publishes each to
Celery. It is the piece that turns "durably recorded intent" into "the
broker has the task", with **at-least-once** semantics and bounded
retries.

Concurrency safety
------------------
Each message is claimed and finished in its **own transaction** with
``SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`` — copied deliberately from
the SPEC-13 scheduler claim (``apps/scheduler/app/scheduler/refresh.py``,
research R5): ``SKIP LOCKED`` alone guarantees a row is held by at most
one claimant, so any number of drain passes (multiple workers, an
overlapping scheduler tick, a manual invocation) can run concurrently
without publishing the same message twice concurrently and without
blocking each other. No global/advisory pass lock is needed or wanted.

The claim query is inherently cross-tenant (the drain patrols every
workspace), so it is a sanctioned unscoped access on the BYPASSRLS
system session, annotated ``# noqa: workspace-scope`` — the same
precedent as ``_scan_job_refs``/``run_refresh_pass``. Every value the
message carries is already workspace-stamped, and the consumer task
re-scopes with ``set_workspace_context`` before touching a row.

Ordering: publish-then-commit
-----------------------------
``send_task`` is issued **before** the status flip commits. A crash in
between therefore leaves the row PENDING and the message is published
again — at-least-once, duplicates over misses. That is the correct trade
for this system and the reason every consumer had to be audited for
idempotency first (audit §H1/§12). The reverse order
(commit-then-publish) would reintroduce exactly the lost-work window the
outbox exists to close.

Failure handling
----------------
Every attempt increments ``attempts`` and is committed even when the
publish raised, so a broker outage cannot spin on one row. A failed
attempt is rescheduled with capped exponential backoff; once ``attempts``
reaches ``max_attempts`` the row becomes ``DEAD`` — a dead letter that is
never retried automatically and that operators alert on
(:func:`app_shared.outbox.reconciler.sweep_outbox` reports the count).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_shared.enums import OutboxStatus
from app_shared.messaging import enqueue
from app_shared.models.outbox import OutboxMessage

logger = logging.getLogger("app_shared.outbox.dispatcher")

__all__ = ["DrainReport", "drain_outbox", "next_backoff_seconds", "MAX_BACKOFF_SECONDS"]

#: Longest a failed message ever waits before the next publish attempt.
MAX_BACKOFF_SECONDS = 600

#: Characters of an exception repr kept in ``last_error`` — a breadcrumb,
#: never an unbounded blob in a row retention keeps for days.
_MAX_ERROR_CHARS = 500


@dataclass(frozen=True)
class DrainReport:
    """One pass's outcome — the structured log line's payload."""

    claimed: int = 0
    published: int = 0
    failed: int = 0
    dead_lettered: int = 0


def next_backoff_seconds(attempts: int, *, base_seconds: int) -> int:
    """Capped exponential backoff for the ``attempts``-th failed publish.

    ``base * 2**(attempts-1)``, clamped to :data:`MAX_BACKOFF_SECONDS`.
    """
    if attempts < 1:
        return base_seconds
    exponent = min(attempts - 1, 20)
    return min(base_seconds * (2**exponent), MAX_BACKOFF_SECONDS)


def _claim_one(session: Session, now: datetime) -> OutboxMessage | None:
    """Claim the oldest publishable message, or ``None`` if there is none."""
    return (
        session.execute(
            select(OutboxMessage)  # noqa: workspace-scope
            .where(
                OutboxMessage.status == OutboxStatus.PENDING,
                OutboxMessage.available_at <= now,
            )
            .order_by(OutboxMessage.available_at, OutboxMessage.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        .scalars()
        .first()
    )


def drain_outbox(
    session_factory: Callable[[], Session],
    *,
    now: datetime,
    batch_limit: int,
    max_attempts: int,
    backoff_base_seconds: int,
    publish: Callable[..., None] = enqueue,
) -> DrainReport:
    """Publish up to ``batch_limit`` PENDING messages; return a report.

    Args:
        session_factory: called once per message so every claim gets its
            own transaction (``app_shared.database.get_system_sessionmaker()``
            in production — the BYPASSRLS system session, as the claim is
            cross-tenant).
        now: pass time (injectable clock).
        batch_limit: maximum messages claimed in this pass.
        max_attempts: attempt count after which a message becomes ``DEAD``.
        backoff_base_seconds: first-retry delay; doubles per attempt up to
            :data:`MAX_BACKOFF_SECONDS`.
        publish: the broker seam, defaulting to
            ``app_shared.messaging.enqueue`` (injected in tests).
    """
    claimed = published = failed = dead = 0

    while claimed < batch_limit:
        with session_factory() as session:
            message = _claim_one(session, now)
            if message is None:
                session.rollback()
                break

            claimed += 1
            message.attempts = message.attempts + 1
            message.updated_at = now

            try:
                publish(
                    message.task_name,
                    queue=message.queue,
                    kwargs=dict(message.payload or {}),
                )
            except Exception as exc:  # noqa: BLE001 - handled below, never re-raised
                failed += 1
                message.last_error = repr(exc)[:_MAX_ERROR_CHARS]
                if message.attempts >= max_attempts:
                    message.status = OutboxStatus.DEAD
                    dead += 1
                    logger.error(
                        "outbox_dead_letter id=%s workspace_id=%s task=%s attempts=%d error=%s",
                        message.id,
                        message.workspace_id,
                        message.task_name,
                        message.attempts,
                        message.last_error,
                    )
                else:
                    message.available_at = now + timedelta(
                        seconds=next_backoff_seconds(
                            message.attempts, base_seconds=backoff_base_seconds
                        )
                    )
                    logger.warning(
                        "outbox_publish_failed id=%s task=%s attempt=%d retry_at=%s error=%s",
                        message.id,
                        message.task_name,
                        message.attempts,
                        message.available_at,
                        message.last_error,
                    )
                # Committed even on failure: the attempt counter and the
                # backoff MUST be durable, else a broker outage spins.
                session.commit()
                continue

            message.status = OutboxStatus.PUBLISHED
            message.published_at = now
            message.last_error = None
            # Publish-then-commit (see module docstring): a crash here
            # replays the message, never loses it.
            session.commit()
            published += 1

    return DrainReport(claimed=claimed, published=published, failed=failed, dead_lettered=dead)
