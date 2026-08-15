"""`maintenance` queue tasks for the transactional outbox (audit H1).

``outbox_drain`` publishes durably-recorded ``outbox_messages`` to
Celery; ``outbox_reconcile`` reports backlog/dead-letter health and ages
out terminal rows. Both are thin orchestrators over
:mod:`app_shared.outbox` — the pure logic lives there and is unit-tested
without a broker or a DB.

Both run on the BYPASSRLS system session
(``app_shared.database.get_system_session``), the sanctioned SPEC-13
cross-tenant seam: draining and reconciling is inherently a patrol over
every workspace's messages, exactly like ``finalize_jobs`` scanning every
workspace's jobs. No workspace-owned *domain* row is read or written
here — only the outbox's own bookkeeping columns — and the workspace each
message belongs to is carried in the message itself, re-scoped by the
consumer task with ``set_workspace_context``.

Both tasks are idempotent under at-least-once redelivery, which is what
lets them run under ``task_acks_late``:

* ``outbox_drain`` claims rows with ``FOR UPDATE SKIP LOCKED`` and flips
  each to PUBLISHED in its own transaction, so a replayed drain simply
  finds fewer (or no) claimable rows. A message published twice is
  handled by the consumer's own idempotency (that is the outbox's
  at-least-once contract).
* ``outbox_reconcile`` only counts rows and deletes rows already past a
  retention cutoff — replaying it deletes nothing new.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.workers.celery_app import app
from app_shared.config import get_settings
from app_shared.database import get_system_session, get_system_sessionmaker
from app_shared.outbox import drain_outbox, sweep_outbox
from app_shared.task_names import OUTBOX_DRAIN, OUTBOX_RECONCILE

logger = logging.getLogger("workers.outbox")


@app.task(name=OUTBOX_DRAIN)
def outbox_drain() -> None:
    """Publish up to ``OUTBOX_DRAIN_BATCH_LIMIT`` PENDING messages.

    Safe to run concurrently with any number of other drains (``SKIP
    LOCKED`` per-message claim, one transaction each) and safe to
    redeliver. Emits one structured run-report line per pass.
    """
    settings = get_settings()
    report = drain_outbox(
        get_system_sessionmaker(),
        now=datetime.now(timezone.utc),
        batch_limit=settings.OUTBOX_DRAIN_BATCH_LIMIT,
        max_attempts=settings.OUTBOX_MAX_ATTEMPTS,
        backoff_base_seconds=settings.OUTBOX_RETRY_BACKOFF_BASE_SECONDS,
    )

    if report.claimed:
        logger.info(
            "outbox_drain claimed=%d published=%d failed=%d dead_lettered=%d",
            report.claimed,
            report.published,
            report.failed,
            report.dead_lettered,
        )


@app.task(name=OUTBOX_RECONCILE)
def outbox_reconcile() -> None:
    """Report outbox backlog/dead-letter health and apply retention.

    The `oldest_pending_age_seconds` it logs is the single number that
    surfaces "async work is silently piling up"; a non-zero `dead` count
    means committed domain work never reached a worker and is meant to be
    alerted on (audit §H5).
    """
    settings = get_settings()
    with get_system_session() as session:
        sweep_outbox(
            session,
            now=datetime.now(timezone.utc),
            stuck_after_seconds=settings.OUTBOX_STUCK_AFTER_SECONDS,
            retention_days=settings.RETENTION_OUTBOX_MESSAGES_DAYS,
        )
        session.commit()
