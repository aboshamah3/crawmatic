"""Live webhook-event-creation test (SPEC-16 US3 T037, contracts/events.md,
FR-008/FR-009/FR-010/FR-011, SC-003/SC-007) — DEFERRED.

Exercises `apps/workers/app/workers/tasks_webhooks.py::create_webhook_event`
(called directly, synchronously — a `@app.task`-decorated function is a
plain callable when invoked without `.delay()`/`.apply_async()`, the same
pattern this repo already uses for `flush_profile`) directly against a real
Postgres:

1. One call inserts exactly one `webhook_events` row: `status=PENDING`,
   `delivered_at IS NULL` (SC-007), `event_type`/`payload` matching what was
   passed, in the correct workspace (SC-003).
2. A duplicate signal (same `dedup_key`, same `event_type`/`payload`) never
   produces a *contradictory* row — whether or not the best-effort Redis
   `SET NX` collapsed the retry, every row for that `dedup_key` carries
   identical `event_type`/`payload` (FR-009: duplicates tolerated,
   contradictions are not).
3. Soft-ref/retention tolerance: after an old, now-expired `webhook_events`
   monthly partition is dropped by `app_shared.maintenance.retention.
   run_retention` (SPEC-15, already-registered `PARTITIONED_TABLES` entry),
   a poll-shaped scoped query over the table still succeeds and returns
   exactly the still-live (current-month) rows — dropping an old partition
   never breaks reads of the rest of the table (FR-019 "readers tolerate
   references into dropped/expired partitions").

Needs a reachable Postgres (`DATABASE_URL`, the SPEC-16 migration applied,
i.e. `webhook_events`/`webhook_endpoints` tables exist) AND a usable
BYPASSRLS system role (`SYSTEM_DATABASE_URL`/`AUTH_DATABASE_URL` fallback,
for both `create_webhook_event`'s `get_session` writes and `run_retention`'s
system session). Not runnable in the no-Docker-daemon build environment
used to author this feature — SKIPS cleanly whenever either isn't
reachable.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host with
the SPEC-16 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text


def _live_webhook_creation_reachable() -> bool:
    """Best-effort probe: Postgres reachable, the SPEC-16 tables exist, and
    a usable BYPASSRLS system role is available (for `run_retention` +
    `create_webhook_event`'s own writes)."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL:
        return False

    try:
        from sqlalchemy import inspect

        from app_shared.database import check_connection, get_engine, get_system_sessionmaker

        check_connection()
        table_names = set(inspect(get_engine()).get_table_names())
        if not {"webhook_events", "webhook_endpoints"} <= table_names:
            return False

        with get_system_sessionmaker()() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_webhook_creation_reachable(),
    reason=(
        "Needs a reachable Postgres (DATABASE_URL, the SPEC-16 migration "
        "applied) and a usable BYPASSRLS system role in this environment."
    ),
)


@pytest.fixture()
def seeded_workspace() -> Iterator[uuid.UUID]:
    """One bare workspace — `webhook_events` carries only a real FK on
    `workspace_id` (FR-019), so nothing else needs seeding."""
    from app_shared.database import get_session
    from app_shared.enums import WorkspaceStatus
    from app_shared.models import Workspace

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        workspace = Workspace(
            name=f"webhook-events-live-test {unique}",
            slug=f"webhook-events-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        session.commit()
        workspace_id = workspace.id

    yield workspace_id

    with get_session() as session:
        session.execute(
            text("DELETE FROM webhook_events WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        session.commit()


def _poll_events(workspace_id: uuid.UUID) -> list:
    """The same "resolve first, scope second" shape the poll API uses:
    `set_workspace_context` + `scoped_select` over `webhook_events`,
    ordered by `(created_at, id)` (mirrors `apps/api/app/routers/
    webhooks.py`'s list query, without the HTTP/pagination wrapper)."""
    from app_shared.database import get_session, set_workspace_context
    from app_shared.models.webhooks import WebhookEvent
    from app_shared.repository import scoped_select

    with get_session() as session:
        set_workspace_context(session, workspace_id)
        stmt = scoped_select(WebhookEvent, workspace_id).order_by(
            WebhookEvent.created_at, WebhookEvent.id
        )
        return list(session.execute(stmt).scalars().all())


def test_create_webhook_event_writes_pending_undelivered_row(
    seeded_workspace: uuid.UUID,
) -> None:
    from app_shared.enums import WebhookEventStatus, WebhookEventType

    import app.workers.tasks_webhooks as tasks_webhooks

    payload = {
        "product_variant_id": str(uuid.uuid4()),
        "product_id": str(uuid.uuid4()),
        "alert_state_id": str(uuid.uuid4()),
        "previous_type": None,
        "new_type": "RISK",
        "previous_severity": None,
        "new_severity": "HIGH",
        "transition": "CREATED",
    }

    tasks_webhooks.create_webhook_event(
        workspace_id=str(seeded_workspace),
        event_type=WebhookEventType.PRICE_ALERT_CREATED.value,
        payload=payload,
        dedup_key=f"alert:{payload['alert_state_id']}:CREATED:api",
    )

    rows = _poll_events(seeded_workspace)
    assert len(rows) == 1
    row = rows[0]
    assert row.workspace_id == seeded_workspace
    assert row.event_type == WebhookEventType.PRICE_ALERT_CREATED.value
    assert row.payload == payload
    assert row.status == WebhookEventStatus.PENDING.value
    assert row.delivered_at is None


def test_duplicate_signal_never_creates_a_contradictory_row(
    seeded_workspace: uuid.UUID,
) -> None:
    from app_shared.enums import WebhookEventType

    import app.workers.tasks_webhooks as tasks_webhooks

    profile_id = str(uuid.uuid4())
    payload = {
        "strategy_profile_id": profile_id,
        "domain": "example.com",
        "new_status": "ACTIVE",
        "change": "PROMOTED",
        "method": "DIRECT_HTTP",
    }
    dedup_key = f"strategy:{profile_id}:ACTIVE:PROMOTED"

    for _ in range(2):
        tasks_webhooks.create_webhook_event(
            workspace_id=str(seeded_workspace),
            event_type=WebhookEventType.DOMAIN_STRATEGY_UPDATED.value,
            payload=payload,
            dedup_key=dedup_key,
        )

    rows = _poll_events(seeded_workspace)
    # Duplicates (at-least-once retries) are tolerated -- one or two rows --
    # but every row for this signal must agree (FR-009: never contradictory).
    assert 1 <= len(rows) <= 2
    for row in rows:
        assert row.event_type == WebhookEventType.DOMAIN_STRATEGY_UPDATED.value
        assert row.payload == payload


# --- soft-ref / retention tolerance ------------------------------------------


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _create_month_partition(parent: str, year: int, month: int) -> str:
    """Create an arbitrary (past) month partition directly — mirrors
    `tests/integration/test_retention_drop_live.py`'s helper, since
    `create_missing_partitions` only ever creates current+lookahead
    months."""
    from app_shared.database import get_system_sessionmaker

    start, end = _month_bounds(year, month)
    name = f"{parent}_{year:04d}_{month:02d}"
    with get_system_sessionmaker()() as session:
        session.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {parent} "
                f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
            )
        )
        session.commit()
    return name


def _run_retention(now_utc: datetime):
    from app_shared.database import get_system_sessionmaker
    from app_shared.maintenance.retention import run_retention

    with get_system_sessionmaker()() as session:
        report = run_retention(session, now_utc=now_utc)
        session.commit()
    return report


def test_poll_still_succeeds_after_expired_partition_dropped(
    seeded_workspace: uuid.UUID,
) -> None:
    from app_shared.enums import WebhookEventStatus, WebhookEventType
    from app_shared.models.webhooks import WebhookEvent

    now = datetime.now(timezone.utc)
    # Well past RETENTION_WEBHOOK_EVENTS_DAYS=90 -- guaranteed eligible for
    # drop regardless of the exact current day-of-month.
    expired_at = now - timedelta(days=200)
    _create_month_partition("webhook_events", expired_at.year, expired_at.month)

    from app_shared.database import get_session

    with get_session() as session:
        session.add(
            WebhookEvent(
                workspace_id=seeded_workspace,
                created_at=expired_at,
                event_type=WebhookEventType.SCRAPE_JOB_COMPLETED.value,
                payload={"scrape_job_id": str(uuid.uuid4()), "status": "COMPLETED"},
                status=WebhookEventStatus.PENDING.value,
                delivered_at=None,
            )
        )
        session.add(
            WebhookEvent(
                workspace_id=seeded_workspace,
                created_at=now,
                event_type=WebhookEventType.SCRAPE_JOB_COMPLETED.value,
                payload={"scrape_job_id": str(uuid.uuid4()), "status": "COMPLETED"},
                status=WebhookEventStatus.PENDING.value,
                delivered_at=None,
            )
        )
        session.commit()

    rows_before = _poll_events(seeded_workspace)
    assert len(rows_before) == 2

    _run_retention(now)

    # The expired partition (and the row it held) is gone, but the poll
    # itself never errors -- soft references into a dropped partition are
    # tolerated (FR-019); the still-live current-month row remains.
    rows_after = _poll_events(seeded_workspace)
    assert len(rows_after) == 1
    assert rows_after[0].created_at.date() == now.date()
