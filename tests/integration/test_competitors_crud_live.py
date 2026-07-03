"""Live-Postgres competitor CRUD test (SPEC-05 US1 T013, SC-001) — ⏸ DEFERRED.

Exercises the full `/v1/competitors` surface against a real database
through FastAPI's `TestClient` (no running server/container required —
only the database needs to be live):

1. Create a competitor with just name+domain -> stored with server-side
   defaults (`legal_status=REVIEW_REQUIRED`, `robots_policy=RESPECT`,
   `status=ACTIVE`), workspace-scoped (FR-003, SC-001).
2. Re-creating the same `(workspace_id, domain)` -> `409 DUPLICATE_DOMAIN`
   (FR-003).
3. Read / update / list persistence, workspace-scoped.
4. Delete returns `{"outcome": "hard_deleted"}` (FR-016) and a subsequent
   read 404s.

Needs a reachable Postgres instance with `DATABASE_URL` (app role, RLS
enforced) usable AND the SPEC-05 migration already applied
(`alembic upgrade head`). Not runnable in the no-Docker-daemon build
environment used to author this feature — SKIPS cleanly whenever
`Settings`/`DATABASE_URL` isn't usable, a real connection attempt fails,
or the `competitors` table doesn't exist yet (mirrors
`tests/integration/test_products_crud_live.py`'s skip mechanism).

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-05 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest


def _live_competitors_reachable() -> bool:
    """Best-effort probe: True only if Postgres is reachable AND the
    SPEC-05 `competitors` table already exists (migration applied)."""
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
        if "competitors" not in table_names:
            return False
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_competitors_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-05 competitors "
        "migration applied in this environment"
    ),
)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture()
def workspace_and_api_key() -> Iterator[dict[str, str]]:
    """A fresh ACTIVE workspace + a full-scoped competitors API key, cleaned up after."""
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.security.api_keys import generate_api_key

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        workspace = Workspace(
            name=f"Competitors Live Test {unique}",
            slug=f"competitors-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id

        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=workspace_id,
            name="competitors-live-test-key",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=["competitors:read", "competitors:write"],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add(api_key)
        session.commit()

    yield {"workspace_id": str(workspace_id), "api_key": full_secret}

    with get_session() as session:
        from sqlalchemy import text

        session.execute(
            text("DELETE FROM competitors WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(text("DELETE FROM api_keys WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        session.commit()


@pytest.fixture()
def auth_headers(workspace_and_api_key: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {workspace_and_api_key['api_key']}"}


# --- SC-001: create with defaults -------------------------------------------


def test_create_competitor_yields_server_side_defaults(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    response = client.post(
        "/v1/competitors",
        headers=auth_headers,
        json={"name": "Acme Co", "domain": f"acme-{unique}.example.com"},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["name"] == "Acme Co"
    assert body["legal_status"] == "REVIEW_REQUIRED"
    assert body["robots_policy"] == "RESPECT"
    assert body["status"] == "ACTIVE"


# --- FR-003: domain unique per workspace -> 409 -----------------------------


def test_recreating_same_domain_in_workspace_is_409(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    domain = f"dup-{unique}.example.com"

    first = client.post(
        "/v1/competitors", headers=auth_headers, json={"name": "First", "domain": domain}
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/v1/competitors", headers=auth_headers, json={"name": "Second", "domain": domain}
    )
    assert second.status_code == 409
    assert second.json()["detail"]["error"]["code"] == "DUPLICATE_DOMAIN"


# --- read / update / list / delete ------------------------------------------


def test_read_update_list_delete_round_trip(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    create = client.post(
        "/v1/competitors",
        headers=auth_headers,
        json={"name": "Roundtrip Co", "domain": f"roundtrip-{unique}.example.com"},
    )
    assert create.status_code == 201
    competitor_id = create.json()["id"]

    # Read.
    read = client.get(f"/v1/competitors/{competitor_id}", headers=auth_headers)
    assert read.status_code == 200
    assert read.json()["name"] == "Roundtrip Co"

    # Update.
    update = client.patch(
        f"/v1/competitors/{competitor_id}", headers=auth_headers, json={"name": "Updated Co"}
    )
    assert update.status_code == 200
    assert update.json()["name"] == "Updated Co"

    # List includes it.
    listing = client.get("/v1/competitors", headers=auth_headers)
    assert listing.status_code == 200
    assert any(item["id"] == competitor_id for item in listing.json()["items"])

    # Delete.
    delete = client.delete(f"/v1/competitors/{competitor_id}", headers=auth_headers)
    assert delete.status_code == 200
    assert delete.json() == {"id": competitor_id, "outcome": "hard_deleted"}

    # Subsequent read 404s.
    after = client.get(f"/v1/competitors/{competitor_id}", headers=auth_headers)
    assert after.status_code == 404
