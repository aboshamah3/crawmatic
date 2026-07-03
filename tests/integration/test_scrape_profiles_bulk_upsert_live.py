"""Live-Postgres scrape-profile bulk-upsert test (SPEC-06 US1 T029, FR-020, SC-008) — ⏸ DEFERRED.

Exercises `POST /v1/scrape-profiles/bulk-upsert` against a real database
through FastAPI's `TestClient` (no running server/container required —
only the database needs to be live):

1. A mixed valid/invalid batch -> every valid row upserted, every
   invalid row reported in `rejected[]` (field-specific), the batch
   never aborted (reject-and-report, FR-020).
2. Re-pushing the same valid batch unmodified -> `0` net-new rows
   (matched on `(workspace_id, name)`), no duplicates — idempotent.
3. Re-pushing with a changed field -> the matched row updates in place.
4. Never writes a global row: every upserted row carries the caller's
   own `workspace_id`.
5. Statement-count boundedness: the whole batch lands via a single
   `ON CONFLICT ... DO UPDATE` statement regardless of row count
   (asserted by comparing `upserted` against a larger batch size, same
   convention as `tests/integration/test_matches_bulk_upsert_live.py` —
   a query-log assertion needs a live connection to install an
   `event.listens_for` hook, deferred to the PG-capable host run).

Needs a reachable Postgres instance with `DATABASE_URL` (app role, RLS
enforced) usable AND the SPEC-06 migration already applied
(`alembic upgrade head`). Not runnable in the no-Docker-daemon build
environment used to author this feature — SKIPS cleanly whenever
`Settings`/`DATABASE_URL` isn't usable, a real connection attempt fails,
or the `scrape_profiles` table doesn't exist yet (mirrors
`tests/integration/test_matches_bulk_upsert_live.py`'s skip mechanism).

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-06 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest


def _live_scrape_profiles_reachable() -> bool:
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
        if "scrape_profiles" not in table_names:
            return False
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_scrape_profiles_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-06 scrape_profiles "
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
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.security.api_keys import generate_api_key

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        workspace = Workspace(
            name=f"Profiles Bulk Live Test {unique}",
            slug=f"profiles-bulk-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id

        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=workspace_id,
            name="profiles-bulk-live-test-key",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=["scrape_profiles:read", "scrape_profiles:write"],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add(api_key)
        session.commit()

    yield {"workspace_id": str(workspace_id), "api_key": full_secret}

    with get_session() as session:
        from sqlalchemy import text

        session.execute(
            text("DELETE FROM scrape_profiles WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(text("DELETE FROM api_keys WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        session.commit()


@pytest.fixture()
def auth_headers(workspace_and_api_key: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {workspace_and_api_key['api_key']}"}


# --- FR-020: mixed valid/invalid batch -> reject-and-report ----------------


def test_mixed_batch_upserts_valid_and_reports_invalid(
    client, auth_headers, workspace_and_api_key: dict[str, str]
) -> None:
    unique = uuid.uuid4().hex[:8]
    response = client.post(
        "/v1/scrape-profiles/bulk-upsert",
        headers=auth_headers,
        json={
            "profiles": [
                {"name": f"bulk-good-1-{unique}"},
                {"name": f"bulk-bad-{unique}", "mode": "NOT_A_MODE"},
                {"name": f"bulk-good-2-{unique}", "price_selector": ".price"},
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["upserted"] == 2
    assert {p["name"] for p in body["profiles"]} == {
        f"bulk-good-1-{unique}",
        f"bulk-good-2-{unique}",
    }
    assert len(body["rejected"]) == 1
    rejected = body["rejected"][0]
    assert rejected["name"] == f"bulk-bad-{unique}"
    assert rejected["field"] == "mode"
    assert rejected["code"] == "INVALID_ENUM"

    # Never writes a global row -- every upserted profile carries the
    # caller's own workspace_id.
    for profile in body["profiles"]:
        assert profile["workspace_id"] == workspace_and_api_key["workspace_id"]


# --- idempotent re-push + update-in-place -----------------------------------


def test_repush_same_batch_updates_in_place_no_duplicates(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    name = f"bulk-idempotent-{unique}"

    first = client.post(
        "/v1/scrape-profiles/bulk-upsert",
        headers=auth_headers,
        json={"profiles": [{"name": name, "price_selector": ".v1"}]},
    )
    assert first.status_code == 200
    assert first.json()["upserted"] == 1
    profile_id = first.json()["profiles"][0]["id"]

    second = client.post(
        "/v1/scrape-profiles/bulk-upsert",
        headers=auth_headers,
        json={"profiles": [{"name": name, "price_selector": ".v2"}]},
    )
    assert second.status_code == 200
    assert second.json()["upserted"] == 1
    assert second.json()["profiles"][0]["id"] == profile_id
    assert second.json()["profiles"][0]["price_selector"] == ".v2"

    listing = client.get("/v1/scrape-profiles", headers=auth_headers)
    matching = [item for item in listing.json()["items"] if item["name"] == name]
    assert len(matching) == 1


# --- bounded statement count (single ON CONFLICT for the whole batch) ------


def test_larger_batch_upserts_all_rows_in_one_bounded_call(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    profiles = [{"name": f"bulk-scale-{unique}-{i}"} for i in range(25)]

    response = client.post(
        "/v1/scrape-profiles/bulk-upsert", headers=auth_headers, json={"profiles": profiles}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["upserted"] == 25
    assert body["rejected"] == []
