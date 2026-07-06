"""Live poll-API test for `/v1/webhook-events` (SPEC-16 US1 T020,
FR-014/FR-015/FR-020, SC-001/SC-004) — DEFERRED.

Exercises the full `GET /v1/webhook-events` (+`/{id}`) surface against a
real database through FastAPI's `TestClient` (no running server/container
required — only the database needs to be live), mirroring
`tests/integration/test_refresh_rules_crud_live.py`'s self-contained
probe/fixture idiom (own workspace + API key, no shared spider/alerts
fixture module needed):

1. Seed events directly (bypassing the not-yet-built US3 producer seams)
   spanning two monthly partitions (current + next month, self-healed via
   `create_missing_partitions` exactly like `test_partition_create_live.py`)
   and walk `GET /v1/webhook-events` cursor-to-exhaustion: every event
   returned exactly once, deterministically ordered by `(created_at, id)`,
   gapless across the partition boundary (SC-001).
2. `event_type` filter narrows to the matching subset only.
3. `GET /v1/webhook-events/{id}` returns the matching payload.
4. A caller from another workspace gets 404 on direct fetch and never sees
   the row in its own list (SC-004).
5. A raw query with no `app.workspace_id` context set returns 0 rows
   (fail-closed, SC-004) — RLS alone enforces this.
6. An invalid/garbage cursor -> `422 INVALID_CURSOR`.
7. An API key without `webhooks:read` -> `403` on every webhook-events route.

Needs a reachable Postgres (`DATABASE_URL`, the SPEC-16 migration
`03dec3037c8f` applied, i.e. `webhook_events`/`webhook_endpoints` tables
exist) AND a usable BYPASSRLS system role (for the partition self-heal
step, mirroring `test_partition_create_live.py`). Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever any of that isn't reachable.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-16 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest


def _live_webhook_events_reachable() -> bool:
    """Best-effort probe: Postgres reachable, the SPEC-16 tables exist, and
    a usable BYPASSRLS system role is available (for the partition
    self-heal step)."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL:
        return False

    try:
        from sqlalchemy import inspect, text

        from app_shared.database import (
            check_connection,
            get_engine,
            get_system_sessionmaker,
        )

        check_connection()
        table_names = set(inspect(get_engine()).get_table_names())
        if not {"webhook_events", "webhook_endpoints"} <= table_names:
            return False

        system_sessionmaker = get_system_sessionmaker()
        with system_sessionmaker() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_webhook_events_reachable(),
    reason=(
        "Needs a reachable Postgres (DATABASE_URL) with the SPEC-16 "
        "webhook_events/webhook_endpoints migration applied and a usable "
        "BYPASSRLS system role in this environment."
    ),
)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _make_workspace_and_key(unique: str, scopes: list[str]) -> tuple[uuid.UUID, str]:
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.security.api_keys import generate_api_key

    with get_session() as session:
        workspace = Workspace(
            name=f"Webhook Events Live Test {unique}",
            slug=f"webhook-events-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id

        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=workspace_id,
            name="webhook-events-live-test-key",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=scopes,
            status=ApiKeyStatus.ACTIVE,
        )
        session.add(api_key)
        session.commit()

    return workspace_id, full_secret


def _cleanup_workspace(workspace_id: uuid.UUID) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        session.execute(
            text("DELETE FROM webhook_events WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.execute(
            text("DELETE FROM api_keys WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(
            text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id}
        )
        session.commit()


def _ensure_current_and_next_month_partitions() -> None:
    """Self-heal the two `webhook_events_YYYY_MM` child partitions (mirrors
    `test_partition_create_live.py`) so a seeded row dated into next month
    never fails on a missing partition."""
    from app_shared.database import get_system_sessionmaker
    from app_shared.maintenance.partitions import create_missing_partitions

    now = datetime.now(timezone.utc)
    with get_system_sessionmaker()() as session:
        create_missing_partitions(session, now_utc=now, lookahead_months=1)
        session.commit()


def _seed_event(
    workspace_id: uuid.UUID,
    *,
    event_type: str,
    created_at: datetime,
    payload: dict | None = None,
) -> uuid.UUID:
    from app_shared.database import get_session
    from app_shared.enums import WebhookEventStatus
    from app_shared.models.webhooks import WebhookEvent

    with get_session() as session:
        event = WebhookEvent(
            workspace_id=workspace_id,
            created_at=created_at,
            event_type=event_type,
            payload=payload or {"note": "seeded"},
            status=WebhookEventStatus.PENDING,
            delivered_at=None,
        )
        session.add(event)
        session.commit()
        return event.id


@dataclass
class _Fixture:
    workspace_id: uuid.UUID
    api_key_read: str
    api_key_no_scope: str
    other_workspace_id: uuid.UUID
    other_api_key_read: str
    event_ids_in_order: list[uuid.UUID]
    other_event_id: uuid.UUID


@pytest.fixture()
def fixture() -> Iterator[_Fixture]:
    _ensure_current_and_next_month_partitions()

    unique = uuid.uuid4().hex[:8]
    workspace_id, api_key_read = _make_workspace_and_key(unique, ["webhooks:read"])
    _no_scope_ws, api_key_no_scope = _make_workspace_and_key(
        f"noscope-{unique}", ["catalog:read"]
    )
    other_workspace_id, other_api_key_read = _make_workspace_and_key(
        f"other-{unique}", ["webhooks:read"]
    )

    # Seed events spanning two monthly partitions: a handful "now", then a
    # handful dated into next month, all strictly increasing in created_at
    # so ordering is unambiguous across the partition boundary (SC-001).
    now = datetime.now(timezone.utc)
    next_month = (now.replace(day=1) + timedelta(days=32)).replace(
        day=1, hour=12, minute=0, second=0, microsecond=0
    )

    event_ids: list[uuid.UUID] = []
    for i in range(3):
        event_ids.append(
            _seed_event(
                workspace_id,
                event_type="price.alert.created",
                created_at=now + timedelta(seconds=i),
            )
        )
    for i in range(3):
        event_ids.append(
            _seed_event(
                workspace_id,
                event_type="scrape.job.completed",
                created_at=next_month + timedelta(seconds=i),
            )
        )

    other_event_id = _seed_event(
        other_workspace_id,
        event_type="price.alert.created",
        created_at=now,
    )

    try:
        yield _Fixture(
            workspace_id=workspace_id,
            api_key_read=api_key_read,
            api_key_no_scope=api_key_no_scope,
            other_workspace_id=other_workspace_id,
            other_api_key_read=other_api_key_read,
            event_ids_in_order=event_ids,
            other_event_id=other_event_id,
        )
    finally:
        _cleanup_workspace(workspace_id)
        _cleanup_workspace(_no_scope_ws)
        _cleanup_workspace(other_workspace_id)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# --- 1. cursor-to-exhaustion: exactly-once, gapless, ordered (SC-001) -------


def test_poll_pagination_across_partitions_is_exactly_once_and_gapless(
    fixture: _Fixture, client
) -> None:
    headers = _auth(fixture.api_key_read)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(20):  # generous upper bound on page-walk iterations
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get("/v1/webhook-events", params=params, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("Did not reach the end of the backlog within the iteration budget.")

    expected = [str(i) for i in fixture.event_ids_in_order]
    assert seen == expected  # exactly once, deterministic order, gapless


# --- 2. event_type filter narrows the result set ----------------------------


def test_event_type_filter_narrows_result_set(fixture: _Fixture, client) -> None:
    headers = _auth(fixture.api_key_read)

    response = client.get(
        "/v1/webhook-events",
        params={"event_type": "scrape.job.completed", "limit": 50},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"], "expected at least one scrape.job.completed event"
    assert all(item["event_type"] == "scrape.job.completed" for item in body["items"])


# --- 3. single fetch by id ---------------------------------------------------


def test_get_single_event_by_id(fixture: _Fixture, client) -> None:
    headers = _auth(fixture.api_key_read)
    event_id = fixture.event_ids_in_order[0]

    response = client.get(f"/v1/webhook-events/{event_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(event_id)
    assert body["delivered_at"] is None
    assert body["status"] == "PENDING"


# --- 4. cross-workspace isolation: 404 on direct fetch, absent from list ----


def test_cross_workspace_fetch_is_404_and_absent_from_list(
    fixture: _Fixture, client
) -> None:
    headers = _auth(fixture.api_key_read)

    response = client.get(
        f"/v1/webhook-events/{fixture.other_event_id}", headers=headers
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"

    listing = client.get("/v1/webhook-events", params={"limit": 50}, headers=headers)
    assert listing.status_code == 200
    assert all(
        item["id"] != str(fixture.other_event_id) for item in listing.json()["items"]
    )


def test_unknown_event_id_is_404(fixture: _Fixture, client) -> None:
    headers = _auth(fixture.api_key_read)
    response = client.get(f"/v1/webhook-events/{uuid.uuid4()}", headers=headers)
    assert response.status_code == 404


# --- 5. no workspace context at all -> 0 rows, fail closed ------------------


def test_no_workspace_context_returns_zero_rows_fail_closed(fixture: _Fixture) -> None:
    from sqlalchemy import create_engine, text

    from app_shared.config import get_settings

    engine = create_engine(get_settings().DATABASE_URL)
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT id FROM webhook_events WHERE workspace_id = :ws"),
                {"ws": fixture.workspace_id},
            ).fetchall()
            assert rows == []
    finally:
        engine.dispose()


# --- 6. invalid cursor -> 422 INVALID_CURSOR --------------------------------


def test_invalid_cursor_is_422_invalid_cursor(fixture: _Fixture, client) -> None:
    headers = _auth(fixture.api_key_read)
    response = client.get(
        "/v1/webhook-events",
        params={"cursor": "not-a-valid-cursor!!!"},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "INVALID_CURSOR"


# --- 7. missing webhooks:read scope -> 403 on every webhook-events route ----


def test_missing_webhooks_read_scope_is_forbidden(fixture: _Fixture, client) -> None:
    headers = _auth(fixture.api_key_no_scope)

    response = client.get("/v1/webhook-events", headers=headers)
    assert response.status_code == 403

    response = client.get(
        f"/v1/webhook-events/{fixture.event_ids_in_order[0]}", headers=headers
    )
    assert response.status_code == 403


def test_empty_workspace_past_the_end_is_not_an_error(
    fixture: _Fixture, client
) -> None:
    """An empty page (past the end of the backlog) is `{items: [], next_cursor:
    null}`, not an error (contract edge case)."""
    headers = _auth(fixture.other_api_key_read)

    # `fixture.other_workspace_id` has exactly one seeded event; walk past it.
    response = client.get("/v1/webhook-events", params={"limit": 50}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["next_cursor"] is None

    exhausted = client.get(
        "/v1/webhook-events",
        params={"cursor": _last_cursor(client, headers)},
        headers=headers,
    )
    assert exhausted.status_code == 200
    assert exhausted.json() == {"items": [], "next_cursor": None}


def _last_cursor(client, headers: dict[str, str]) -> str:
    from app_shared.pagination import encode_cursor

    response = client.get("/v1/webhook-events", params={"limit": 50}, headers=headers)
    item = response.json()["items"][-1]
    return encode_cursor(
        datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")),
        uuid.UUID(item["id"]),
    )
