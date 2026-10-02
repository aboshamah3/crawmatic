"""`/v1/admin/workspaces` — SaaS provisioning surface (PLAN §7.1)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import admin
from app.service_auth import require_service_token
from unit._admin_fake_session import FakeUsageSession
from unit._jobs_fake_session import FakeOrmSession

SERVICE_HEADERS = {"Authorization": "Bearer test-service-token"}


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def session() -> FakeOrmSession:
    return FakeOrmSession()


@pytest.fixture(autouse=True)
def _authorized(session: FakeOrmSession) -> None:
    app.dependency_overrides[require_service_token] = lambda: None
    app.dependency_overrides[admin.get_admin_session] = lambda: session


def test_provision_returns_workspace_id_and_plaintext_key(client, session):
    resp = client.post(
        "/v1/admin/workspaces",
        json={"name": "Acme Store", "external_ref": "proj_123"},
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert uuid.UUID(body["workspace_id"])
    assert body["api_key"].startswith("ck_")
    assert body["external_ref"] == "proj_123"


def test_provision_persists_workspace_and_api_key(client, session):
    client.post(
        "/v1/admin/workspaces",
        json={"name": "Acme Store", "external_ref": "proj_123"},
        headers=SERVICE_HEADERS,
    )
    added_types = {type(obj).__name__ for obj in session.added}
    assert "Workspace" in added_types
    assert "ApiKey" in added_types


def test_provision_key_hash_is_stored_not_plaintext(client, session):
    resp = client.post(
        "/v1/admin/workspaces",
        json={"name": "Acme Store", "external_ref": "proj_124"},
        headers=SERVICE_HEADERS,
    )
    plaintext = resp.json()["api_key"]
    keys = [o for o in session.added if type(o).__name__ == "ApiKey"]
    assert len(keys) == 1
    assert keys[0].key_hash != plaintext
    assert plaintext.startswith(keys[0].key_prefix)


def test_provision_rejects_extra_fields(client):
    resp = client.post(
        "/v1/admin/workspaces",
        json={"name": "Acme", "external_ref": "p1", "sneaky": True},
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 422


def test_provision_requires_external_ref(client):
    resp = client.post(
        "/v1/admin/workspaces", json={"name": "Acme"}, headers=SERVICE_HEADERS
    )
    assert resp.status_code == 422


def test_archive_sets_status_suspended(client, session):
    """`WorkspaceStatus` has no `ARCHIVED` member (only ACTIVE/SUSPENDED) —
    see `app.routers.admin` module docstring for the substitution."""
    from app_shared.enums import WorkspaceStatus
    from app_shared.models.identity import Workspace

    ws = Workspace(
        id=uuid.uuid4(), name="Acme", slug="acme", status=WorkspaceStatus.ACTIVE
    )
    session.seed(ws)

    resp = client.post(
        f"/v1/admin/workspaces/{ws.id}/archive", headers=SERVICE_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "suspended"
    assert ws.status == WorkspaceStatus.SUSPENDED


def test_archive_response_status_rejects_a_non_enum_value():
    """`WorkspaceArchiveResponse.status` was typed as bare `str` -- any
    string, including a mis-rendered `"WorkspaceStatus.SUSPENDED"` (what
    a non-`StrEnum` `WorkspaceStatus` would produce from a bare
    `str(...)` call), would validate silently. Typing it as
    `WorkspaceStatus` makes Pydantic reject anything that isn't a real
    member (review finding I6d)."""
    import uuid as uuid_mod

    import pytest as _pytest
    from pydantic import ValidationError

    from app.schemas.admin import WorkspaceArchiveResponse

    with _pytest.raises(ValidationError):
        WorkspaceArchiveResponse(
            workspace_id=uuid_mod.uuid4(), status="WorkspaceStatus.SUSPENDED"
        )


def test_archive_unknown_workspace_is_404(client, session):
    resp = client.post(
        f"/v1/admin/workspaces/{uuid.uuid4()}/archive", headers=SERVICE_HEADERS
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_admin_routes_require_the_service_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the dependency override the real seam must refuse.

    `require_service_token` isn't overridden here (only
    `admin.get_admin_session` would be, and this test doesn't even set
    that) -- it calls the real `app.service_auth.get_settings`, so that
    one call is monkeypatched the same way `test_service_auth.py` does,
    to avoid needing a full `Settings()` (DATABASE_URL, REDIS_URL, ...)
    in the unit-test environment.
    """

    class _Settings:
        SAAS_SERVICE_TOKEN = "s3cret-service-token"

    monkeypatch.setattr("app.service_auth.get_settings", lambda: _Settings())
    app.dependency_overrides.clear()
    with TestClient(app) as bare:
        resp = bare.post(
            "/v1/admin/workspaces", json={"name": "x", "external_ref": "y"}
        )
    assert resp.status_code == 401


def _usage_row(**over):
    base = dict(
        workspace_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        cycle_ts="2026-08-03T14:00:00+00:00",
        links_total=7,
        links_succeeded=6,
        protected_links_attempted=1,
        protected_links_succeeded=1,
        check_successful=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _usage_client(rows):
    session = FakeUsageSession(rows)
    app.dependency_overrides[require_service_token] = lambda: None
    app.dependency_overrides[admin.get_admin_session] = lambda: session
    return TestClient(app), session


def test_usage_returns_the_frozen_contract_fields():
    client, _ = _usage_client([_usage_row()])
    resp = client.get(
        "/v1/admin/usage?since=2026-08-01T00:00:00Z&until=2026-08-08T00:00:00Z",
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert set(item) == {
        "workspace_id",
        "product_id",
        "cycle_ts",
        "links_total",
        "links_succeeded",
        "protected_links_attempted",
        "protected_links_succeeded",
        "check_successful",
        # Task B3 (2026-09-03): additive network_operations transport facts.
        "proxied_http_attempted",
        "proxied_browser_attempted",
        "proxy_bytes",
        # Task C6/F17 (2026-09-08): additive, the workspace's OWN share of
        # the physical operations the cycle used (`cost_allocations`).
        "allocated_cost_micro_units",
    }


def test_usage_defaults_proxied_transport_fields_to_zero_when_source_lacks_them():
    """A row built before Task B3 (no network_operations join in the
    fake session) must still validate — the three new fields default to
    `0`, never `None`/missing, keeping the response additive."""
    client, _ = _usage_client([_usage_row()])
    resp = client.get(
        "/v1/admin/usage?since=2026-08-01T00:00:00Z&until=2026-08-08T00:00:00Z",
        headers=SERVICE_HEADERS,
    )
    item = resp.json()["items"][0]
    assert item["proxied_http_attempted"] == 0
    assert item["proxied_browser_attempted"] == 0
    assert item["proxy_bytes"] == 0


def test_usage_reports_supplied_proxied_transport_fields():
    row = _usage_row(
        proxied_http_attempted=2,
        proxied_browser_attempted=1,
        proxy_bytes=2_200_000,
    )
    client, _ = _usage_client([row])
    resp = client.get(
        "/v1/admin/usage?since=2026-08-01T00:00:00Z&until=2026-08-08T00:00:00Z",
        headers=SERVICE_HEADERS,
    )
    item = resp.json()["items"][0]
    assert item["proxied_http_attempted"] == 2
    assert item["proxied_browser_attempted"] == 1
    assert item["proxy_bytes"] == 2_200_000


def test_usage_returns_the_items_next_cursor_envelope():
    client, _ = _usage_client([_usage_row()])
    resp = client.get(
        "/v1/admin/usage?since=2026-08-01T00:00:00Z&until=2026-08-08T00:00:00Z",
        headers=SERVICE_HEADERS,
    )
    body = resp.json()
    assert set(body) == {"items", "next_cursor"}
    assert body["next_cursor"] is None


def test_usage_emits_a_cursor_when_more_rows_exist():
    rows = [_usage_row() for _ in range(3)]
    client, _ = _usage_client(rows)
    resp = client.get(
        "/v1/admin/usage?since=2026-08-01T00:00:00Z&until=2026-08-08T00:00:00Z&limit=2",
        headers=SERVICE_HEADERS,
    )
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None


def test_usage_window_over_31_days_is_422():
    client, _ = _usage_client([])
    resp = client.get(
        "/v1/admin/usage?since=2026-01-01T00:00:00Z&until=2026-06-01T00:00:00Z",
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "WINDOW_TOO_LARGE"


def test_usage_inverted_window_is_422_invalid_window_not_window_too_large():
    """CHANGED (review finding I6b): `since >= until` used to map to the
    misleading `422 WINDOW_TOO_LARGE`. It's now the distinct
    `422 INVALID_WINDOW` -- `WINDOW_TOO_LARGE` stays reserved for the
    real >31-day case (see `test_usage_window_over_31_days_is_422`,
    unchanged)."""
    client, _ = _usage_client([])
    resp = client.get(
        "/v1/admin/usage?since=2026-08-08T00:00:00Z&until=2026-08-01T00:00:00Z",
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "INVALID_WINDOW"


def test_usage_accepts_naive_since_and_until():
    """A naive `since`/`until` (no UTC offset in the query string) must be
    treated as UTC, not rejected and not silently misinterpreted (review
    finding I6c)."""
    client, _ = _usage_client([_usage_row()])
    resp = client.get(
        "/v1/admin/usage?since=2026-08-01T00:00:00&until=2026-08-08T00:00:00",
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 200


def test_usage_bad_cursor_is_422():
    client, _ = _usage_client([])
    resp = client.get(
        "/v1/admin/usage?since=2026-08-01T00:00:00Z&until=2026-08-08T00:00:00Z&cursor=%21%21%21",
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "INVALID_CURSOR"


def test_usage_requires_since_and_until():
    client, _ = _usage_client([])
    resp = client.get("/v1/admin/usage", headers=SERVICE_HEADERS)
    assert resp.status_code == 422


def test_slugify_is_display_only_and_bounded():
    """`f"{base}-{ref}"[:200]` used to truncate from the END, which is
    where `external_ref` lives -- since `name` is capped at 200 chars,
    two different refs on a 200-char name collided into the same slug,
    so customer B's provisioning call would 409 naming customer A's
    workspace. Truncating `base` (never `ref`) fixes this."""
    from app.routers.admin import _slugify

    assert len(_slugify("A" * 200)) <= 200


def test_provision_duplicate_external_ref_is_409(client, session):
    """The SaaS retries provisioning; a retry must not surface a 500."""
    from sqlalchemy.exc import IntegrityError

    original_flush = session.flush
    calls = {"n": 0}

    def _flush_raising_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        return original_flush(*args, **kwargs)

    session.flush = _flush_raising_once
    # A real rollback discards the unflushed Workspace; the fake has no undo log.
    session.rollback = lambda *a, **k: session._rows.pop(type(session.added[0]), None)

    resp = client.post(
        "/v1/admin/workspaces",
        json={"name": "Acme Store", "external_ref": "proj_dupe"},
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_EXTERNAL_REF"


def _provision(client, name, ref):
    return client.post(
        "/v1/admin/workspaces",
        json={"name": name, "external_ref": ref},
        headers=SERVICE_HEADERS,
    )


def _keys(session):
    return [o for o in session.added if type(o).__name__ == "ApiKey"]


def test_same_ref_different_name_is_same_workspace_one_key(client, session):
    first = _provision(client, "Acme", "proj_1")
    second = _provision(client, "Totally Different", "proj_1")
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["workspace_id"] == first.json()["workspace_id"]
    assert second.json()["api_key"] is None
    assert len(_keys(session)) == 1
    assert len([o for o in session.added if type(o).__name__ == "Workspace"]) == 1


def test_refs_differing_only_in_punctuation_are_two_workspaces(client, session):
    a = _provision(client, "Shop", "Store_1")
    b = _provision(client, "Shop", "store.1")
    assert a.status_code == b.status_code == 201
    assert a.json()["workspace_id"] != b.json()["workspace_id"]
    assert len(_keys(session)) == 2


def test_name_ref_boundary_split_is_two_workspaces(client, session):
    a = _provision(client, "a-b", "c")
    b = _provision(client, "a", "b-c")
    assert a.status_code == b.status_code == 201
    assert a.json()["workspace_id"] != b.json()["workspace_id"]


def test_slug_collisions_get_numeric_suffixes(client, session):
    for ref in ("r1", "r2", "r3"):
        assert _provision(client, "Acme Store", ref).status_code == 201
    slugs = [o.slug for o in session.added if type(o).__name__ == "Workspace"]
    assert slugs == ["acme-store", "acme-store-2", "acme-store-3"]


def test_backfill_script_emits_guarded_updates():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[2] / "scripts/security/backfill_workspace_external_ref.py"
    spec = importlib.util.spec_from_file_location("backfill_ref", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ws = str(uuid.uuid4())
    (stmt,) = mod.build_statements([[ws, "proj'1"]])
    assert "external_ref IS NULL" in stmt and "'proj''1'" in stmt
    with pytest.raises(ValueError):
        mod.build_statements([[ws, "a"], [ws, "b"]])
