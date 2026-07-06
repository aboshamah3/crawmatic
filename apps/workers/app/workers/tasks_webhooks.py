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

from app.workers.celery_app import app
from app_shared.database import get_session, set_workspace_context
from app_shared.enums import WebhookEventStatus
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
        return bool(redis.set(f"{_DEDUP_KEY_PREFIX}:{dedup_key}", "1", nx=True, ex=_DEDUP_TTL_SECONDS))
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
) -> None:
    """Insert one `webhook_events` row (`PENDING`, `delivered_at=NULL`).

    `dedup_key` is optional best-effort `SET NX` de-dup: when supplied and
    already claimed (a retry of the same signal within `_DEDUP_TTL_SECONDS`),
    this call is a no-op — duplicates are acceptable, contradictions are
    not (FR-009). Never awaits a result and makes no outbound HTTP call.
    """
    if dedup_key is not None and not _claim_dedup_key(dedup_key):
        return

    ws = uuid.UUID(str(workspace_id))

    with get_session() as session:
        set_workspace_context(session, ws)
        session.add(
            WebhookEvent(
                workspace_id=ws,
                created_at=datetime.now(timezone.utc),
                event_type=event_type,
                payload=payload,
                status=WebhookEventStatus.PENDING,
                delivered_at=None,
            )
        )
        session.commit()
