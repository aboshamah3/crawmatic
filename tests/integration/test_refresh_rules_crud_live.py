"""Live-Postgres refresh-rules CRUD test (SPEC-13 US1 T013, FR-004/005; US1
AS-1..4; SC-001/004) — ⏸ DEFERRED.

Exercises the full `/v1/refresh-rules` surface against a real database
through FastAPI's `TestClient` (no running server/container required — only
the database needs to be live), mirroring
`tests/integration/test_competitors_crud_live.py`'s probe/fixture idiom:

1. Create a `WORKSPACE`/cron rule and a `PRODUCT_GROUP`/interval rule ->
   `enabled=true` + a correct, strictly-future first `next_run_at` (US1
   AS-1/2).
2. Read / list / delete round-trip.
3. `PATCH {"enabled": false}` persists and does not touch `next_run_at`
   (US1 AS-3); a cadence-changing PATCH recomputes it.
4. Neither/both cadence -> `422 INVALID_CADENCE`; bad cron ->
   `422 INVALID_CRON`; missing/cross-workspace target id ->
   `422 SCOPE_TARGET_MISMATCH` (US1 AS-5/6).
5. A rule created in workspace A is invisible/unaddressable (404) to a
   workspace-B caller, even by direct id (US1 AS-4, SC-004).

Needs a reachable Postgres instance with `DATABASE_URL` (app role, RLS
enforced) usable AND the SPEC-13 migration already applied (`alembic
upgrade head`). Not runnable in the no-Docker-daemon build environment used
to author this feature — SKIPS cleanly whenever `Settings`/`DATABASE_URL`
isn't usable, a real connection attempt fails, or the `refresh_rules` table
doesn't exist yet (mirrors `test_competitors_crud_live.py`'s skip
mechanism).

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host with
the SPEC-13 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest


def _live_refresh_rules_reachable() -> bool:
    """Best-effort probe: True only if Postgres is reachable AND the
    SPEC-13 `refresh_rules` table already exists (migration applied)."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL:
        return False

    try:
        from sqlalchemy import inspect

        from app_shared.database import check_connection, get_engine

        check_connection()
        inspector = inspect(get_engine())
        table_names = set(inspector.get_table_names())
        if "refresh_rules" not in table_names:
            return False
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_refresh_rules_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-13 refresh_rules "
        "migration applied in this environment"
    ),
)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _make_workspace_and_key(unique: str) -> tuple[uuid.UUID, str]:
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.security.api_keys import generate_api_key

    with get_session() as session:
        workspace = Workspace(
            name=f"Refresh Rules Live Test {unique}",
            slug=f"refresh-rules-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id

        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=workspace_id,
            name="refresh-rules-live-test-key",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=["refresh_rules:read", "refresh_rules:write"],
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
            text("DELETE FROM refresh_rules WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(text("DELETE FROM api_keys WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        session.commit()


@pytest.fixture()
def workspace_and_api_key() -> Iterator[dict[str, str]]:
    """A fresh ACTIVE workspace + a full-scoped refresh-rules API key, cleaned up after."""
    unique = uuid.uuid4().hex[:8]
    workspace_id, full_secret = _make_workspace_and_key(unique)

    yield {"workspace_id": str(workspace_id), "api_key": full_secret}

    _cleanup_workspace(workspace_id)


@pytest.fixture()
def auth_headers(workspace_and_api_key: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {workspace_and_api_key['api_key']}"}


@pytest.fixture()
def other_workspace_and_api_key() -> Iterator[dict[str, str]]:
    """A second, independent ACTIVE workspace + API key (cross-workspace denial)."""
    unique = uuid.uuid4().hex[:8]
    workspace_id, full_secret = _make_workspace_and_key(f"other-{unique}")

    yield {"workspace_id": str(workspace_id), "api_key": full_secret}

    _cleanup_workspace(workspace_id)


@pytest.fixture()
def other_auth_headers(other_workspace_and_api_key: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {other_workspace_and_api_key['api_key']}"}


# --- US1 AS-1/2: create computes enabled=true + first next_run_at ----------


def test_create_workspace_cron_rule_computes_future_next_run_at(client, auth_headers) -> None:
    before = datetime.now(timezone.utc)
    response = client.post(
        "/v1/refresh-rules",
        headers=auth_headers,
        json={
            "name": "Hourly workspace refresh",
            "scope": "WORKSPACE",
            "cron_expression": "0 * * * *",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["enabled"] is True
    assert body["next_run_at"] is not None
    next_run_at = datetime.fromisoformat(body["next_run_at"].replace("Z", "+00:00"))
    assert next_run_at > before


def test_create_product_group_interval_rule(client, auth_headers) -> None:
    group_id = str(uuid.uuid4())
    response = client.post(
        "/v1/refresh-rules",
        headers=auth_headers,
        json={
            "name": "Group refresh",
            "scope": "PRODUCT_GROUP",
            "product_group_id": group_id,
            "interval_minutes": 30,
        },
    )
    # The group id is dangling (doesn't exist in this workspace) ->
    # SCOPE_TARGET_MISMATCH, not a crash (contract: missing/cross-workspace
    # target id is indistinguishable from "not yours").
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"]["code"] == "SCOPE_TARGET_MISMATCH"


# --- US1 AS-5: neither/both cadence -> INVALID_CADENCE ----------------------


def test_neither_cadence_field_is_422_invalid_cadence(client, auth_headers) -> None:
    # Rejected by RefreshRuleCreate's schema-level model_validator (fires
    # during FastAPI's own request-body parsing, before the handler runs) —
    # asserted by status code only, mirroring the `test_matches_crud_live.py`
    # precedent for `MatchCreate`'s cross-field validator.
    response = client.post(
        "/v1/refresh-rules",
        headers=auth_headers,
        json={"name": "Bad rule", "scope": "WORKSPACE"},
    )
    assert response.status_code == 422


def test_both_cadence_fields_is_422(client, auth_headers) -> None:
    response = client.post(
        "/v1/refresh-rules",
        headers=auth_headers,
        json={
            "name": "Bad rule",
            "scope": "WORKSPACE",
            "cron_expression": "0 * * * *",
            "interval_minutes": 5,
        },
    )
    assert response.status_code == 422


def test_bad_cron_is_422(client, auth_headers) -> None:
    response = client.post(
        "/v1/refresh-rules",
        headers=auth_headers,
        json={"name": "Bad rule", "scope": "WORKSPACE", "cron_expression": "garbage"},
    )
    assert response.status_code == 422


# --- read / update / list / delete ------------------------------------------


def test_read_update_list_delete_round_trip(client, auth_headers) -> None:
    create = client.post(
        "/v1/refresh-rules",
        headers=auth_headers,
        json={
            "name": "Roundtrip rule",
            "scope": "WORKSPACE",
            "interval_minutes": 60,
        },
    )
    assert create.status_code == 201, create.text
    rule_id = create.json()["id"]
    original_next_run_at = create.json()["next_run_at"]

    # Read.
    read = client.get(f"/v1/refresh-rules/{rule_id}", headers=auth_headers)
    assert read.status_code == 200
    assert read.json()["name"] == "Roundtrip rule"

    # PATCH enabled=false does not touch next_run_at (US1 AS-3; FR-006/016).
    disable = client.patch(
        f"/v1/refresh-rules/{rule_id}", headers=auth_headers, json={"enabled": False}
    )
    assert disable.status_code == 200
    assert disable.json()["enabled"] is False
    assert disable.json()["next_run_at"] == original_next_run_at

    # PATCH changing the cadence recomputes next_run_at.
    recadence = client.patch(
        f"/v1/refresh-rules/{rule_id}", headers=auth_headers, json={"interval_minutes": 15}
    )
    assert recadence.status_code == 200
    assert recadence.json()["interval_minutes"] == 15

    # Empty PATCH -> 422 EMPTY_UPDATE.
    empty = client.patch(f"/v1/refresh-rules/{rule_id}", headers=auth_headers, json={})
    assert empty.status_code == 422
    assert empty.json()["detail"]["error"]["code"] == "EMPTY_UPDATE"

    # List includes it.
    listing = client.get("/v1/refresh-rules", headers=auth_headers)
    assert listing.status_code == 200
    assert any(item["id"] == rule_id for item in listing.json()["items"])

    # Delete.
    delete = client.delete(f"/v1/refresh-rules/{rule_id}", headers=auth_headers)
    assert delete.status_code == 200
    assert delete.json() == {"id": rule_id, "outcome": "hard_deleted"}

    # Subsequent read 404s.
    after = client.get(f"/v1/refresh-rules/{rule_id}", headers=auth_headers)
    assert after.status_code == 404


# --- US1 AS-4 / SC-004: cross-workspace invisibility ------------------------


def test_cross_workspace_rule_is_invisible_and_unaddressable(
    client, auth_headers, other_auth_headers
) -> None:
    create = client.post(
        "/v1/refresh-rules",
        headers=auth_headers,
        json={"name": "Workspace A rule", "scope": "WORKSPACE", "interval_minutes": 45},
    )
    assert create.status_code == 201, create.text
    rule_id = create.json()["id"]

    # Workspace B cannot read it by id.
    cross_read = client.get(f"/v1/refresh-rules/{rule_id}", headers=other_auth_headers)
    assert cross_read.status_code == 404

    # Workspace B cannot write it either.
    cross_write = client.patch(
        f"/v1/refresh-rules/{rule_id}", headers=other_auth_headers, json={"enabled": False}
    )
    assert cross_write.status_code == 404

    # Workspace B's list never includes it.
    cross_list = client.get("/v1/refresh-rules", headers=other_auth_headers)
    assert cross_list.status_code == 200
    assert all(item["id"] != rule_id for item in cross_list.json()["items"])
