"""Live-Postgres scrape-profile CRUD test (SPEC-06 US1 T027, FR-002/003/010, SC-001/SC-006) — ⏸ DEFERRED.

Exercises the full `/v1/scrape-profiles` surface against a real database
through FastAPI's `TestClient` (no running server/container required —
only the database needs to be live):

1. Create with `validation_rules`/`confidence_rules` JSONB bundles ->
   read back **byte-identical** (round-trip fidelity, FR-010).
2. Create with only `name` -> documented defaults applied
   (`mode=HTTP`, `adapter_key=default_http`, the three `*_enabled=True`,
   `variant_strategy=PAGE_SINGLE_PRICE`, `request_timeout_ms=30000`,
   FR-002).
3. Re-creating the same `(workspace_id, name)` -> `409 DUPLICATE_PROFILE`
   (FR-003).
4. Read / update / list / delete persistence, workspace-scoped.
5. Invalid payloads (bad enum, un-compilable/catastrophic regex,
   session cookie, malformed `validation_rules`/`confidence_rules`) ->
   `422 VALIDATION_ERROR` with a field-specific body (SC-006).

Needs a reachable Postgres instance with `DATABASE_URL` (app role, RLS
enforced) usable AND the SPEC-06 migration already applied
(`alembic upgrade head`). Not runnable in the no-Docker-daemon build
environment used to author this feature — SKIPS cleanly whenever
`Settings`/`DATABASE_URL` isn't usable, a real connection attempt fails,
or the `scrape_profiles` table doesn't exist yet (mirrors
`tests/integration/test_competitors_crud_live.py`'s skip mechanism).

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-06 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest


def _live_scrape_profiles_reachable() -> bool:
    """Best-effort probe: True only if Postgres is reachable AND the
    SPEC-06 `scrape_profiles` table already exists (migration applied)."""
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
    """A fresh ACTIVE workspace + a full-scoped scrape-profiles API key, cleaned up after."""
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.security.api_keys import generate_api_key

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        workspace = Workspace(
            name=f"Scrape Profiles Live Test {unique}",
            slug=f"scrape-profiles-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id

        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=workspace_id,
            name="scrape-profiles-live-test-key",
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


# --- FR-010: round-trip fidelity of JSONB bundles ---------------------------


def test_create_with_rules_bundles_round_trips_byte_identical(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    validation_rules = {
        "required_currency": "USD",
        "min_price": "1.00",
        "max_price": "999.99",
        "reject_if_text_contains": ["out of stock", "sold out"],
        "prefer_text_contains": ["in stock"],
    }
    confidence_rules = {"css": 0.85, "regex": 0.7, "jsonld": 0.95}

    response = client.post(
        "/v1/scrape-profiles",
        headers=auth_headers,
        json={
            "name": f"round-trip-{unique}",
            "validation_rules": validation_rules,
            "confidence_rules": confidence_rules,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["validation_rules"] == validation_rules
    assert body["confidence_rules"] == confidence_rules

    read = client.get(f"/v1/scrape-profiles/{body['id']}", headers=auth_headers)
    assert read.status_code == 200
    assert read.json()["validation_rules"] == validation_rules
    assert read.json()["confidence_rules"] == confidence_rules


# --- SC-001/FR-002: documented defaults --------------------------------------


def test_create_with_only_name_yields_documented_defaults(
    client, auth_headers, workspace_and_api_key
) -> None:
    unique = uuid.uuid4().hex[:8]
    response = client.post(
        "/v1/scrape-profiles", headers=auth_headers, json={"name": f"defaults-{unique}"}
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["mode"] == "HTTP"
    assert body["adapter_key"] == "default_http"
    assert body["jsonld_enabled"] is True
    assert body["platform_patterns_enabled"] is True
    assert body["embedded_json_enabled"] is True
    assert body["variant_strategy"] == "PAGE_SINGLE_PRICE"
    assert body["request_timeout_ms"] == 30000
    # A tenant-created row always carries the caller's own workspace_id
    # (never global, FR-021).
    assert body["workspace_id"] == workspace_and_api_key["workspace_id"]


# --- FR-003: name unique per workspace -> 409 --------------------------------


def test_recreating_same_name_in_workspace_is_409(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    name = f"dup-{unique}"

    first = client.post("/v1/scrape-profiles", headers=auth_headers, json={"name": name})
    assert first.status_code == 201, first.text

    second = client.post("/v1/scrape-profiles", headers=auth_headers, json={"name": name})
    assert second.status_code == 409
    assert second.json()["detail"]["error"]["code"] == "DUPLICATE_PROFILE"


# --- read / update / list / delete ------------------------------------------


def test_read_update_list_delete_round_trip(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    create = client.post(
        "/v1/scrape-profiles", headers=auth_headers, json={"name": f"roundtrip-{unique}"}
    )
    assert create.status_code == 201
    profile_id = create.json()["id"]

    read = client.get(f"/v1/scrape-profiles/{profile_id}", headers=auth_headers)
    assert read.status_code == 200
    assert read.json()["name"] == f"roundtrip-{unique}"

    update = client.patch(
        f"/v1/scrape-profiles/{profile_id}",
        headers=auth_headers,
        json={"price_selector": ".price"},
    )
    assert update.status_code == 200
    assert update.json()["price_selector"] == ".price"

    listing = client.get("/v1/scrape-profiles", headers=auth_headers)
    assert listing.status_code == 200
    assert any(item["id"] == profile_id for item in listing.json()["items"])

    delete = client.delete(f"/v1/scrape-profiles/{profile_id}", headers=auth_headers)
    assert delete.status_code == 200
    assert delete.json() == {"id": profile_id, "outcome": "hard_deleted"}

    after = client.get(f"/v1/scrape-profiles/{profile_id}", headers=auth_headers)
    assert after.status_code == 404


# --- SC-006: invalid payloads -> 422 field-specific --------------------------


def test_invalid_enum_is_422(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    response = client.post(
        "/v1/scrape-profiles",
        headers=auth_headers,
        json={"name": f"bad-enum-{unique}", "mode": "NOT_A_MODE"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] in {"VALIDATION_ERROR", "INVALID_ENUM"}


def test_uncompilable_regex_is_422(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    response = client.post(
        "/v1/scrape-profiles",
        headers=auth_headers,
        json={"name": f"bad-regex-{unique}", "price_regex": "(unclosed"},
    )
    assert response.status_code == 422
    body = response.json()["detail"]["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert body["field"] == "price_regex"


def test_session_cookie_is_422(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    response = client.post(
        "/v1/scrape-profiles",
        headers=auth_headers,
        json={"name": f"bad-cookie-{unique}", "cookies": {"sessionid": "abc123"}},
    )
    assert response.status_code == 422
    body = response.json()["detail"]["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert body["field"] == "cookies"


def test_malformed_validation_rules_is_422(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    response = client.post(
        "/v1/scrape-profiles",
        headers=auth_headers,
        json={
            "name": f"bad-rules-{unique}",
            "validation_rules": {"min_price": "100.00", "max_price": "10.00"},
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "VALIDATION_ERROR"


def test_confidence_out_of_range_is_422(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    response = client.post(
        "/v1/scrape-profiles",
        headers=auth_headers,
        json={"name": f"bad-confidence-{unique}", "confidence_rules": {"css": 1.5}},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "VALIDATION_ERROR"
