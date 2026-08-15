"""``webhook_events`` queue task: `create_webhook_event` (SPEC-16 US3,
contracts/events.md).

Consumes the fire-and-forget producer seams (SPEC-09 `recompute_variant`,
SPEC-08 `finalize_jobs`, SPEC-12 `flush_stats`/`light_recheck`) — each
enqueues this task **by name** (`app_shared.messaging.enqueue`) strictly
after its own `session.commit()`, so a broker outage at the seam never
fails/rolls back the already-committed source operation (FR-009/SC-005).

This task never imports source domain code (`app_shared.alerts`/
`app_shared.jobs`/`app_shared.strategy`) — its only job is to durably
record one `webhook_events` row (`status=PENDING`, `delivered_at=NULL`,
no outbound HTTP, FR-010/SC-007). Mirrors `tasks_analysis.py::recompute_variant`'s
shape: opens its own `get_session()`, scopes to the caller's workspace,
inserts, commits.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.workers.celery_app import app
from app_shared.database import get_session, set_workspace_context
from app_shared.enums import WebhookEventStatus
from app_shared.ids import new_uuid7
from app_shared.models.webhooks import WebhookEvent
from app_shared.redis_client import get_redis_client
from app_shared.task_names import CREATE_WEBHOOK_EVENT

logger = logging.getLogger(__name__)

#: Best-effort dedup window (mirrors `scrapyd/client.py`'s `dispatched:{...}`
#: `SET NX` precedent) -- collapses same-cycle Celery retries of an
#: identical signal into one row; NOT a correctness dependency (FR-009
#: tolerates duplicates, never contradictions).
_DEDUP_KEY_PREFIX = "webhookdedup"
_DEDUP_TTL_SECONDS = 3600


def _claim_dedup_key(dedup_key: str) -> bool:
    """`True` if this call should proceed (no live duplicate claim), `False`
    to skip -- a Redis failure here is swallowed and treated as "proceed"
    (dedup is best-effort, never a reason to drop a genuine event)."""
    try:
        redis = get_redis_client()
        return bool(
            redis.set(
                f"{_DEDUP_KEY_PREFIX}:{dedup_key}", "1", nx=True, ex=_DEDUP_TTL_SECONDS
            )
        )
    except Exception:
        logger.warning(
            "create_webhook_event: dedup check failed, proceeding dedup_key=%s",
            dedup_key,
            exc_info=True,
        )
        return True


@app.task(name=CREATE_WEBHOOK_EVENT)
def create_webhook_event(
    *,
    workspace_id: str,
    event_type: str,
    payload: dict,
    dedup_key: str | None = None,
    event_id: str | None = None,
    occurred_at: str | None = None,
) -> None:
    """Insert one `webhook_events` row (`PENDING`, `delivered_at=NULL`).

    Idempotency (2026-08-15 audit risk H1). Enabling `task_acks_late` +
    `task_reject_on_worker_lost` means this task WILL be redelivered
    after a worker loss, and the transactional outbox publishes
    at-least-once by design. A plain INSERT would then write a duplicate
    `webhook_events` row on every replay, and the pre-existing Redis
    `SET NX` guard below cannot prevent it — Redis dedup is explicitly
    best-effort and fails open (its own docstring says so), which is
    exactly the wrong posture for a durability fix.

    So the correctness guard is now at the **database** level: when the
    caller supplies `event_id`/`occurred_at` (the outbox dispatcher does —
    it passes the outbox message's own id and creation time, both stable
    across every replay of that message), the row is inserted under a
    deterministic primary key with `ON CONFLICT DO NOTHING`. `webhook_events`
    is partitioned by `created_at` with `PRIMARY KEY (id, created_at)`, so
    pinning both columns pins the partition too and the conflict is
    inferable. Replaying the same message therefore produces exactly one
    row, no matter how many times it is delivered.

    Legacy/direct callers that omit both kwargs keep the old behaviour
    (fresh uuid + `now()`), which is still safe: those seams are the
    ones that went through Redis dedup before and duplicates were always
    tolerated there ("duplicates are acceptable, contradictions are not",
    FR-009).

    `dedup_key` remains the best-effort `SET NX` collapse of same-cycle
    duplicates — now a pure contention reducer sitting in front of the
    real guard, never the guard itself. Never awaits a result and makes
    no outbound HTTP call.
    """
    if dedup_key is not None and not _claim_dedup_key(dedup_key):
        return

    ws = uuid.UUID(str(workspace_id))
    row_id = uuid.UUID(str(event_id)) if event_id is not None else new_uuid7()
    created_at = (
        datetime.fromisoformat(occurred_at)
        if occurred_at is not None
        else datetime.now(timezone.utc)
    )
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    with get_session() as session:
        set_workspace_context(session, ws)
        stmt = pg_insert(WebhookEvent).values(
            id=row_id,
            workspace_id=ws,
            created_at=created_at,
            event_type=event_type,
            payload=payload,
            status=WebhookEventStatus.PENDING.value,
            delivered_at=None,
        )
        session.execute(stmt.on_conflict_do_nothing(index_elements=["id", "created_at"]))
        session.commit()
