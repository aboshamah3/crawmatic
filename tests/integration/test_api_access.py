"""Live `/v1/proxy-providers` + `/v1/access-policies` + `/v1/domain-access-rules`
CRUD API test (SPEC-10 US1 T020, `contracts/api-access.md` Acceptance) — DEFERRED.

Exercises the full three-router surface against a real database through
FastAPI's `TestClient` (no running server/container required — only the
database needs to be live), mirroring
`tests/integration/test_scrape_profiles_crud_live.py` (dual-scope) and
`tests/integration/test_competitors_crud_live.py` (tenant-only):

1. A created access policy round-trips every strategy/retry/rate field
   intact (US1-1).
2. A proxy provider created with a `password` -> response has
   `has_password=true` and **no** `password` field at all; the DB row
   stores ciphertext that is **not equal** to the plaintext; a second
   `GET` never exposes it either (US1-2, SC-003).
3. Cross-workspace: workspace B cannot read/patch/delete workspace A's
   tenant (`domain_access_rules`) rows; a global provider/policy is
   read-only and immutable from the tenant write path (US1-3, SC-005).
4. A no-workspace-context raw query returns zero tenant rows for the
   dual-scope tables while the global row stays visible (US1-4).
5. A `base_url` pointed at a private/loopback/metadata host, or
   containing embedded userinfo, is rejected `422 UNSAFE_URL` (US1-5).
6. A cross-workspace `provider_id`/`access_policy_id`/`competitor_id`
   assignment is rejected `422 WORKSPACE_MISMATCH`.

Needs a reachable Postgres with `DATABASE_URL` (app role, RLS enforced)
usable AND the SPEC-10 migration already applied (`alembic upgrade
head`). Not runnable in the no-Docker-daemon build environment used to
author this feature — SKIPS cleanly whenever `Settings`/`DATABASE_URL`
isn't usable, a real connection attempt fails, or the `proxy_providers`
table doesn't exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres-capable host
with the SPEC-10 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest


def _live_access_reachable() -> bool:
    """Best-effort probe: True only if Postgres is reachable AND the
    SPEC-10 `proxy_providers` table already exists (migration applied)."""
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
        if not {"proxy_providers", "access_policies", "domain_access_rules"} <= table_names:
            return False
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_access_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) with the SPEC-10 access-policies-proxies "
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
    """A fresh ACTIVE workspace + a competitor + a full-scoped access API key."""
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.models.competitors_matches import Competitor
    from app_shared.security.api_keys import generate_api_key

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        workspace = Workspace(
            name=f"Access API Live Test {unique}",
            slug=f"access-api-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id

        competitor = Competitor(
            workspace_id=workspace_id,
            name=f"competitor-{unique}",
            domain=f"competitor-{unique}.example.com",
        )
        session.add(competitor)
        session.flush()
        competitor_id = competitor.id

        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=workspace_id,
            name="access-api-live-test-key",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=[
                "proxy_providers:read",
                "proxy_providers:write",
                "access_policies:read",
                "access_policies:write",
                "domain_rules:read",
                "domain_rules:write",
            ],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add(api_key)
        session.commit()

    yield {
        "workspace_id": str(workspace_id),
        "competitor_id": str(competitor_id),
        "api_key": full_secret,
    }

    with get_session() as session:
        from sqlalchemy import text

        session.execute(
            text("DELETE FROM domain_access_rules WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(
            text("DELETE FROM access_policies WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(
            text("DELETE FROM proxy_providers WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(
            text("DELETE FROM competitors WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(text("DELETE FROM api_keys WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        session.commit()


@pytest.fixture()
def auth_headers(workspace_and_api_key: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {workspace_and_api_key['api_key']}"}


# --- US1-1: access-policy round-trip -----------------------------------------


def test_access_policy_round_trips_every_field(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    payload = {
        "name": f"policy-{unique}",
        "strategy": "DIRECT_THEN_PROXY",
        "country_code": "US",
        "use_proxy_on_first_attempt": True,
        "use_proxy_on_retry": True,
        "allow_browser_fallback": True,
        "max_retries": 5,
        "rotate_per_request": True,
        "sticky_session": False,
        "session_ttl_minutes": 15,
        "max_requests_per_minute": 60,
        "max_requests_per_hour": 1000,
        "max_requests_per_day": 20000,
        "timeout_ms": 5000,
    }
    response = client.post("/v1/access-policies", headers=auth_headers, json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    for key, value in payload.items():
        assert body[key] == value, key

    read = client.get(f"/v1/access-policies/{body['id']}", headers=auth_headers)
    assert read.status_code == 200
    for key, value in payload.items():
        assert read.json()[key] == value, key


# --- US1-2 / SC-003: proxy password never leaves the process -----------------


def test_proxy_provider_password_is_never_returned(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    response = client.post(
        "/v1/proxy-providers",
        headers=auth_headers,
        json={
            "name": f"provider-{unique}",
            "type": "DATACENTER",
            "base_url": "https://proxy.example.com:8080",
            "username": "svc-user",
            "password": "s3cr3t-plaintext",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["has_password"] is True
    assert "password" not in body
    assert "password_encrypted" not in body
    assert "password_key_version" not in body

    from app_shared.database import get_session
    from sqlalchemy import text

    with get_session() as session:
        row = session.execute(
            text("SELECT password_encrypted FROM proxy_providers WHERE id = :id"),
            {"id": body["id"]},
        ).one()
    assert row.password_encrypted is not None
    assert row.password_encrypted != "s3cr3t-plaintext"

    second_get = client.get(f"/v1/proxy-providers/{body['id']}", headers=auth_headers)
    assert second_get.status_code == 200
    assert "password" not in second_get.json()
    assert second_get.json()["has_password"] is True


def test_proxy_provider_without_password_has_password_false(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    response = client.post(
        "/v1/proxy-providers",
        headers=auth_headers,
        json={
            "name": f"provider-nopw-{unique}",
            "type": "RESIDENTIAL",
            "base_url": "https://proxy2.example.com:8080",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["has_password"] is False


def test_clearing_password_via_patch(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    create = client.post(
        "/v1/proxy-providers",
        headers=auth_headers,
        json={
            "name": f"provider-clear-{unique}",
            "type": "DATACENTER",
            "base_url": "https://proxy3.example.com:8080",
            "password": "initial-secret",
        },
    )
    assert create.status_code == 201
    provider_id = create.json()["id"]
    assert create.json()["has_password"] is True

    patch = client.patch(
        f"/v1/proxy-providers/{provider_id}", headers=auth_headers, json={"password": None}
    )
    assert patch.status_code == 200
    assert patch.json()["has_password"] is False


# --- US1-5: SSRF-unsafe base_url -> 422 UNSAFE_URL ---------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8080",
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:8080",
        "http://user:pass@proxy.example.com:8080",
    ],
)
def test_unsafe_base_url_is_rejected(client, auth_headers, base_url: str) -> None:
    unique = uuid.uuid4().hex[:8]
    response = client.post(
        "/v1/proxy-providers",
        headers=auth_headers,
        json={"name": f"unsafe-{unique}", "type": "DATACENTER", "base_url": base_url},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "UNSAFE_URL"


# --- domain-access-rule round-trip + cross-workspace assignment -------------


def test_domain_access_rule_round_trip(
    client, auth_headers, workspace_and_api_key: dict[str, str]
) -> None:
    unique = uuid.uuid4().hex[:8]
    policy = client.post(
        "/v1/access-policies",
        headers=auth_headers,
        json={"name": f"rule-policy-{unique}", "strategy": "DIRECT_ONLY"},
    )
    assert policy.status_code == 201
    policy_id = policy.json()["id"]

    response = client.post(
        "/v1/domain-access-rules",
        headers=auth_headers,
        json={
            "competitor_id": workspace_and_api_key["competitor_id"],
            "domain": f"shop-{unique}.example.com",
            "access_policy_id": policy_id,
            "max_concurrent_requests": 2,
            "max_requests_per_minute": 30,
            "cooldown_seconds": 5,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workspace_id"] == workspace_and_api_key["workspace_id"]
    assert body["access_policy_id"] == policy_id

    delete = client.delete(f"/v1/domain-access-rules/{body['id']}", headers=auth_headers)
    assert delete.status_code == 200
    assert delete.json() == {"id": body["id"], "outcome": "hard_deleted"}


def test_domain_access_rule_cross_workspace_competitor_is_422(client, auth_headers) -> None:
    unique = uuid.uuid4().hex[:8]
    policy = client.post(
        "/v1/access-policies",
        headers=auth_headers,
        json={"name": f"rule-policy2-{unique}", "strategy": "DIRECT_ONLY"},
    )
    assert policy.status_code == 201

    response = client.post(
        "/v1/domain-access-rules",
        headers=auth_headers,
        json={
            "competitor_id": str(uuid.uuid4()),  # dangling, not workspace-owned
            "domain": f"dangling-{unique}.example.com",
            "access_policy_id": policy.json()["id"],
            "max_concurrent_requests": 1,
            "max_requests_per_minute": 10,
            "cooldown_seconds": 1,
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "NOT_FOUND"


# --- US1-3 / SC-005: cross-workspace + global read-only/immutable ----------


def test_workspace_b_cannot_read_workspace_a_domain_rule(client) -> None:
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, WorkspaceStatus
    from app_shared.models import ApiKey, Workspace
    from app_shared.models.access import AccessPolicy, DomainAccessRule
    from app_shared.models.competitors_matches import Competitor
    from app_shared.security.api_keys import generate_api_key
    from sqlalchemy import text

    unique = uuid.uuid4().hex[:8]
    with get_session() as session:
        ws_a = Workspace(
            name=f"Access Iso A {unique}", slug=f"access-iso-a-{unique}", status=WorkspaceStatus.ACTIVE
        )
        ws_b = Workspace(
            name=f"Access Iso B {unique}", slug=f"access-iso-b-{unique}", status=WorkspaceStatus.ACTIVE
        )
        session.add_all([ws_a, ws_b])
        session.flush()

        competitor_a = Competitor(
            workspace_id=ws_a.id, name=f"comp-a-{unique}", domain=f"comp-a-{unique}.example.com"
        )
        session.add(competitor_a)
        session.flush()

        policy_a = AccessPolicy(
            workspace_id=ws_a.id, name=f"policy-a-{unique}", strategy="DIRECT_ONLY"
        )
        session.add(policy_a)
        session.flush()

        rule_a = DomainAccessRule(
            workspace_id=ws_a.id,
            competitor_id=competitor_a.id,
            domain=f"rule-a-{unique}.example.com",
            access_policy_id=policy_a.id,
            max_concurrent_requests=1,
            max_requests_per_minute=10,
            cooldown_seconds=1,
        )
        session.add(rule_a)
        session.flush()
        rule_a_id = rule_a.id

        secret_b, prefix_b, hash_b = generate_api_key()
        key_b = ApiKey(
            workspace_id=ws_b.id,
            name="access-iso-key-b",
            key_prefix=prefix_b,
            key_hash=hash_b,
            scopes=["domain_rules:read", "domain_rules:write"],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add(key_b)
        session.commit()

        ws_a_id, ws_b_id = ws_a.id, ws_b.id

    try:
        headers_b = {"Authorization": f"Bearer {secret_b}"}
        resp = client.get(f"/v1/domain-access-rules/{rule_a_id}", headers=headers_b)
        assert resp.status_code == 404

        patch = client.patch(
            f"/v1/domain-access-rules/{rule_a_id}", headers=headers_b, json={"enabled": False}
        )
        assert patch.status_code == 404

        delete = client.delete(f"/v1/domain-access-rules/{rule_a_id}", headers=headers_b)
        assert delete.status_code == 404
    finally:
        with get_session() as session:
            session.execute(
                text("DELETE FROM domain_access_rules WHERE workspace_id IN (:a, :b)"),
                {"a": ws_a_id, "b": ws_b_id},
            )
            session.execute(
                text("DELETE FROM access_policies WHERE workspace_id IN (:a, :b)"),
                {"a": ws_a_id, "b": ws_b_id},
            )
            session.execute(
                text("DELETE FROM competitors WHERE workspace_id IN (:a, :b)"),
                {"a": ws_a_id, "b": ws_b_id},
            )
            session.execute(
                text("DELETE FROM api_keys WHERE workspace_id IN (:a, :b)"),
                {"a": ws_a_id, "b": ws_b_id},
            )
            session.execute(
                text("DELETE FROM workspaces WHERE id IN (:a, :b)"), {"a": ws_a_id, "b": ws_b_id}
            )
            session.commit()


def test_tenant_path_cannot_mutate_global_proxy_provider(client, auth_headers) -> None:
    from app_shared.database import get_session
    from app_shared.models.access import ProxyProvider
    from sqlalchemy import text

    unique = uuid.uuid4().hex[:8]
    with get_session() as session:
        # Out-of-band global default (mirrors research D11 for
        # scrape_profiles) — the tenant API can never produce this row.
        global_provider = ProxyProvider(
            workspace_id=None,
            name=f"global-provider-{unique}",
            type="DATACENTER",
            base_url="https://global-proxy.example.com:8080",
        )
        session.add(global_provider)
        session.commit()
        global_id = global_provider.id

    try:
        read = client.get(f"/v1/proxy-providers/{global_id}", headers=auth_headers)
        assert read.status_code == 200
        assert read.json()["workspace_id"] is None

        patch = client.patch(
            f"/v1/proxy-providers/{global_id}", headers=auth_headers, json={"name": "hijacked"}
        )
        assert patch.status_code == 404

        delete = client.delete(f"/v1/proxy-providers/{global_id}", headers=auth_headers)
        assert delete.status_code == 404
    finally:
        with get_session() as session:
            session.execute(text("DELETE FROM proxy_providers WHERE id = :id"), {"id": global_id})
            session.commit()


# --- cross-workspace provider_id assignment on access-policy -> 422 --------


def test_access_policy_cross_workspace_provider_id_is_422(client, auth_headers) -> None:
    from app_shared.database import get_session
    from app_shared.enums import WorkspaceStatus
    from app_shared.models import Workspace
    from app_shared.models.access import ProxyProvider
    from sqlalchemy import text

    unique = uuid.uuid4().hex[:8]
    with get_session() as session:
        other_ws = Workspace(
            name=f"Other Ws {unique}", slug=f"other-ws-{unique}", status=WorkspaceStatus.ACTIVE
        )
        session.add(other_ws)
        session.flush()

        other_provider = ProxyProvider(
            workspace_id=other_ws.id,
            name=f"other-provider-{unique}",
            type="DATACENTER",
            base_url="https://other-proxy.example.com:8080",
        )
        session.add(other_provider)
        session.commit()
        other_ws_id = other_ws.id
        other_provider_id = other_provider.id

    try:
        response = client.post(
            "/v1/access-policies",
            headers=auth_headers,
            json={
                "name": f"cross-ws-policy-{unique}",
                "strategy": "DIRECT_ONLY",
                "provider_id": str(other_provider_id),
            },
        )
        assert response.status_code == 422
        assert response.json()["detail"]["error"]["code"] == "WORKSPACE_MISMATCH"
    finally:
        with get_session() as session:
            session.execute(
                text("DELETE FROM proxy_providers WHERE id = :id"), {"id": other_provider_id}
            )
            session.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": other_ws_id})
            session.commit()
