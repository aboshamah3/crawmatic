"""Live CRUD test for `/v1/webhook-endpoints` (SPEC-16 US2 T028,
FR-002/003/004/005, SC-002/SC-004) — DEFERRED.

Exercises the full `POST/GET/GET{id}/PATCH/DELETE /v1/webhook-endpoints`
surface against a real database through FastAPI's `TestClient` (no running
server/container required — only the database needs to be live), mirroring
`tests/integration/test_api_webhook_events.py`'s (T020) self-contained
probe/fixture idiom and `tests/integration/test_scrape_profiles_isolation_live.py`'s
cross-workspace-isolation shape:

1. Create with a public https URL -> round-trips with `has_secret` (never
   the raw secret); list/get/patch/delete round trip (SC-002).
2. Each SSRF class (private/loopback IP, link-local IP, internal metadata
   hostname, embedded userinfo, non-http(s) scheme) -> `422 UNSAFE_URL`,
   nothing persisted.
3. Secret is stored encrypted (`secret_encrypted` non-null in the DB row)
   and never returned in any response body.
4. PATCH tri-state secret: omitted=unchanged, null=clear, value=re-encrypt;
   `updated_at` advances on every successful PATCH (FR-004).
5. Cross-workspace GET/PATCH/DELETE -> 404; list never shows another
   workspace's row (SC-004).
6. No `app.workspace_id` context set -> 0 rows (fail-closed, raw SQL).
7. An API key with only `webhooks:read` is refused every write op (403)
   but permitted on every read op (US2 AS6).

Needs a reachable Postgres (`DATABASE_URL`, the SPEC-16 migration applied,
i.e. `webhook_events`/`webhook_endpoints` tables exist). Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever that isn't reachable.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host with
the SPEC-16 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest


def _live_webhook_endpoints_reachable() -> bool:
    """Best-effort probe: Postgres reachable and the SPEC-16 tables exist."""
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

        system_sessionmaker = get_system_sessionmaker()
        with system_sessionmaker() as session:
            from sqlalchemy import text

            session.execute(text("SELECT 1"))
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_webhook_endpoints_reachable(),
    reason=(
        "Needs a reachable Postgres (DATABASE_URL) with the SPEC-16 "
        "webhook_events/webhook_endpoints migration applied in this environment."
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
            name=f"Webhook Endpoints Live Test {unique}",
            slug=f"webhook-endpoints-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id

        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=workspace_id,
            name="webhook-endpoints-live-test-key",
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
            text("DELETE FROM webhook_endpoints WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(text("DELETE FROM api_keys WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        session.commit()


@dataclass
class _Fixture:
    workspace_id: uuid.UUID
    api_key_write: str
    api_key_read_only: str
    other_workspace_id: uuid.UUID
    other_api_key_write: str


@pytest.fixture()
def fixture() -> Iterator[_Fixture]:
    unique = uuid.uuid4().hex[:8]
    workspace_id, api_key_write = _make_workspace_and_key(unique, ["webhooks:read", "webhooks:write"])
    _read_only_ws, api_key_read_only = _make_workspace_and_key(
        f"readonly-{unique}", ["webhooks:read"]
    )
    other_workspace_id, other_api_key_write = _make_workspace_and_key(
        f"other-{unique}", ["webhooks:read", "webhooks:write"]
    )

    try:
        yield _Fixture(
            workspace_id=workspace_id,
            api_key_write=api_key_write,
            api_key_read_only=api_key_read_only,
            other_workspace_id=other_workspace_id,
            other_api_key_write=other_api_key_write,
        )
    finally:
        _cleanup_workspace(workspace_id)
        _cleanup_workspace(_read_only_ws)
        _cleanup_workspace(other_workspace_id)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# --- 1. create/list/get/patch/delete round trip (SC-002) --------------------


def test_create_round_trips_with_has_secret_never_raw_secret(fixture: _Fixture, client) -> None:
    headers = _auth(fixture.api_key_write)
    response = client.post(
        "/v1/webhook-endpoints",
        json={
            "name": "My integration",
            "url": "https://hooks.example.com/crawmatic",
            "event_types": ["price.alert.created"],
            "secret": "super-secret-value",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["has_secret"] is True
    assert body["name"] == "My integration"
    assert body["url"] == "https://hooks.example.com/crawmatic"
    assert body["enabled"] is True
    assert body["event_types"] == ["price.alert.created"]
    assert "secret" not in body
    assert "secret_encrypted" not in body
    assert "secret_key_version" not in body
    assert "super-secret-value" not in response.text


def test_full_crud_round_trip(fixture: _Fixture, client) -> None:
    headers = _auth(fixture.api_key_write)

    create_resp = client.post(
        "/v1/webhook-endpoints",
        json={"name": "Round Trip", "url": "https://hooks.example.com/rt"},
        headers=headers,
    )
    assert create_resp.status_code == 201
    endpoint_id = create_resp.json()["id"]

    list_resp = client.get("/v1/webhook-endpoints", headers=headers)
    assert list_resp.status_code == 200
    assert any(item["id"] == endpoint_id for item in list_resp.json()["items"])

    get_resp = client.get(f"/v1/webhook-endpoints/{endpoint_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == endpoint_id
    original_updated_at = get_resp.json()["updated_at"]

    patch_resp = client.patch(
        f"/v1/webhook-endpoints/{endpoint_id}",
        json={"name": "Renamed", "enabled": False},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["name"] == "Renamed"
    assert patched["enabled"] is False
    assert patched["updated_at"] >= original_updated_at

    delete_resp = client.delete(f"/v1/webhook-endpoints/{endpoint_id}", headers=headers)
    assert delete_resp.status_code == 204

    get_after_delete = client.get(f"/v1/webhook-endpoints/{endpoint_id}", headers=headers)
    assert get_after_delete.status_code == 404


# --- 2. each SSRF class -> 422 UNSAFE_URL, nothing persisted -----------------


@pytest.mark.parametrize(
    ("label", "unsafe_url", "expected_reason"),
    [
        ("loopback_ip", "https://127.0.0.1/hook", "PRIVATE_OR_INTERNAL_IP"),
        ("link_local_ip", "https://169.254.169.254/hook", "PRIVATE_OR_INTERNAL_IP"),
        ("internal_metadata_hostname", "http://metadata.google.internal/hook", "INTERNAL_HOSTNAME"),
        ("userinfo_present", "https://user:pass@hooks.example.com/hook", "USERINFO_PRESENT"),
        ("non_http_scheme", "ftp://hooks.example.com/hook", "BAD_SCHEME"),
    ],
)
def test_create_rejects_each_ssrf_class(
    fixture: _Fixture, client, label: str, unsafe_url: str, expected_reason: str
) -> None:
    headers = _auth(fixture.api_key_write)
    response = client.post(
        "/v1/webhook-endpoints",
        json={"name": f"unsafe-{label}", "url": unsafe_url},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    error = response.json()["detail"]["error"]
    assert error["code"] == "UNSAFE_URL"
    assert error["reason"] == expected_reason

    # Nothing persisted: the unsafe name never shows up in the list.
    listing = client.get("/v1/webhook-endpoints", headers=headers)
    assert all(item["name"] != f"unsafe-{label}" for item in listing.json()["items"])


def test_patch_rejects_unsafe_url_and_leaves_row_unchanged(fixture: _Fixture, client) -> None:
    headers = _auth(fixture.api_key_write)
    create_resp = client.post(
        "/v1/webhook-endpoints",
        json={"name": "Safe Endpoint", "url": "https://hooks.example.com/safe"},
        headers=headers,
    )
    endpoint_id = create_resp.json()["id"]

    patch_resp = client.patch(
        f"/v1/webhook-endpoints/{endpoint_id}",
        json={"url": "https://127.0.0.1/evil"},
        headers=headers,
    )
    assert patch_resp.status_code == 422
    assert patch_resp.json()["detail"]["error"]["code"] == "UNSAFE_URL"

    get_resp = client.get(f"/v1/webhook-endpoints/{endpoint_id}", headers=headers)
    assert get_resp.json()["url"] == "https://hooks.example.com/safe"


# --- 3. secret stored encrypted in the DB, never returned --------------------


def test_secret_is_encrypted_in_the_database_row(fixture: _Fixture, client) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    headers = _auth(fixture.api_key_write)
    create_resp = client.post(
        "/v1/webhook-endpoints",
        json={
            "name": "Secret Endpoint",
            "url": "https://hooks.example.com/secret",
            "secret": "top-secret-value",
        },
        headers=headers,
    )
    endpoint_id = create_resp.json()["id"]

    with get_session() as session:
        row = session.execute(
            text("SELECT secret_encrypted, secret_key_version FROM webhook_endpoints WHERE id = :id"),
            {"id": endpoint_id},
        ).one()
        assert row.secret_encrypted is not None
        assert row.secret_encrypted != "top-secret-value"
        assert row.secret_key_version is not None


# --- 4. PATCH tri-state secret + updated_at advances (FR-004) ---------------


def test_patch_secret_tri_state(fixture: _Fixture, client) -> None:
    headers = _auth(fixture.api_key_write)
    create_resp = client.post(
        "/v1/webhook-endpoints",
        json={
            "name": "Tri State",
            "url": "https://hooks.example.com/tri",
            "secret": "initial-secret",
        },
        headers=headers,
    )
    endpoint_id = create_resp.json()["id"]
    assert create_resp.json()["has_secret"] is True

    # Omitted -> unchanged.
    unchanged_resp = client.patch(
        f"/v1/webhook-endpoints/{endpoint_id}", json={"name": "Tri State Renamed"}, headers=headers
    )
    assert unchanged_resp.json()["has_secret"] is True

    # Explicit null -> clear.
    cleared_resp = client.patch(
        f"/v1/webhook-endpoints/{endpoint_id}", json={"secret": None}, headers=headers
    )
    assert cleared_resp.json()["has_secret"] is False

    # Non-null value -> re-encrypt.
    reencrypted_resp = client.patch(
        f"/v1/webhook-endpoints/{endpoint_id}", json={"secret": "new-secret"}, headers=headers
    )
    assert reencrypted_resp.json()["has_secret"] is True


# --- 5. cross-workspace isolation (SC-004) -----------------------------------


def test_cross_workspace_get_patch_delete_is_404(fixture: _Fixture, client) -> None:
    owner_headers = _auth(fixture.api_key_write)
    other_headers = _auth(fixture.other_api_key_write)

    create_resp = client.post(
        "/v1/webhook-endpoints",
        json={"name": "Owner Only", "url": "https://hooks.example.com/owner"},
        headers=owner_headers,
    )
    endpoint_id = create_resp.json()["id"]

    get_resp = client.get(f"/v1/webhook-endpoints/{endpoint_id}", headers=other_headers)
    assert get_resp.status_code == 404

    patch_resp = client.patch(
        f"/v1/webhook-endpoints/{endpoint_id}", json={"name": "Hijacked"}, headers=other_headers
    )
    assert patch_resp.status_code == 404

    delete_resp = client.delete(f"/v1/webhook-endpoints/{endpoint_id}", headers=other_headers)
    assert delete_resp.status_code == 404

    listing = client.get("/v1/webhook-endpoints", headers=other_headers)
    assert all(item["id"] != endpoint_id for item in listing.json()["items"])

    # Untouched from the owner's perspective.
    still_there = client.get(f"/v1/webhook-endpoints/{endpoint_id}", headers=owner_headers)
    assert still_there.status_code == 200
    assert still_there.json()["name"] == "Owner Only"


# --- 6. no workspace context -> 0 rows, fail closed --------------------------


def test_no_workspace_context_returns_zero_rows_fail_closed(fixture: _Fixture, client) -> None:
    from sqlalchemy import create_engine, text

    from app_shared.config import get_settings

    headers = _auth(fixture.api_key_write)
    client.post(
        "/v1/webhook-endpoints",
        json={"name": "Fail Closed", "url": "https://hooks.example.com/fc"},
        headers=headers,
    )

    engine = create_engine(get_settings().DATABASE_URL)
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT id FROM webhook_endpoints WHERE workspace_id = :ws"),
                {"ws": fixture.workspace_id},
            ).fetchall()
            assert rows == []
    finally:
        engine.dispose()


# --- 7. webhooks:read-only key: reads OK, writes 403 (US2 AS6) ---------------


def test_read_only_scope_is_refused_on_every_write_op(fixture: _Fixture, client) -> None:
    read_headers = _auth(fixture.api_key_read_only)

    create_resp = client.post(
        "/v1/webhook-endpoints",
        json={"name": "Should Not Create", "url": "https://hooks.example.com/no"},
        headers=read_headers,
    )
    assert create_resp.status_code == 403

    write_headers = _auth(fixture.api_key_write)
    seeded = client.post(
        "/v1/webhook-endpoints",
        json={"name": "Seeded", "url": "https://hooks.example.com/seeded"},
        headers=write_headers,
    )
    endpoint_id = seeded.json()["id"]

    patch_resp = client.patch(
        f"/v1/webhook-endpoints/{endpoint_id}", json={"name": "Nope"}, headers=read_headers
    )
    assert patch_resp.status_code == 403

    delete_resp = client.delete(f"/v1/webhook-endpoints/{endpoint_id}", headers=read_headers)
    assert delete_resp.status_code == 403


def test_read_only_scope_is_permitted_on_every_read_op(fixture: _Fixture, client) -> None:
    write_headers = _auth(fixture.api_key_write)
    seeded = client.post(
        "/v1/webhook-endpoints",
        json={"name": "Readable", "url": "https://hooks.example.com/readable"},
        headers=write_headers,
    )
    endpoint_id = seeded.json()["id"]

    read_headers = _auth(fixture.api_key_read_only)

    list_resp = client.get("/v1/webhook-endpoints", headers=read_headers)
    assert list_resp.status_code == 200

    get_resp = client.get(f"/v1/webhook-endpoints/{endpoint_id}", headers=read_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == endpoint_id
