"""Tenant policy limits (security E6/E7, P6-T5)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app_shared.access import repository as access_repo
from app_shared.access.repository import GlobalPolicyNotAssignable, assert_policy_assignable
from app_shared.catalog.consistency import CrossWorkspaceReference
from app_shared.enums import CompetitorStatus, LegalStatus, RobotsPolicy
from app_shared.models.competitors_matches import Competitor

from app.deps import Principal, get_current_principal
from app.main import app
from app.routers import admin
from app.service_auth import require_service_token
from unit._jobs_fake_session import FakeOrmSession

WS = uuid.uuid4()
OTHER_WS = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def session() -> FakeOrmSession:
    return FakeOrmSession()


@pytest.fixture()
def client(session: FakeOrmSession) -> TestClient:
    def _dep() -> Iterator[tuple[FakeOrmSession, Principal]]:
        yield session, Principal(
            kind="api_key", id=uuid.uuid4(), role=None,
            scopes=["competitors:read", "competitors:write", "domain_rules:write"],
            workspace_id=WS,
        )

    app.dependency_overrides[get_current_principal] = _dep
    return TestClient(app)


def _visibility(monkeypatch, mapping):
    monkeypatch.setattr(access_repo, "_policy_visibility_map", lambda *_a, **_k: mapping)


# --- E6: assert_policy_assignable --------------------------------------------


def test_own_policy_assignable(monkeypatch) -> None:
    pid = uuid.uuid4()
    _visibility(monkeypatch, {pid: WS})
    assert_policy_assignable(None, WS, pid)  # type: ignore[arg-type]


def test_global_policy_rejected_for_tenant(monkeypatch) -> None:
    pid = uuid.uuid4()
    _visibility(monkeypatch, {pid: None})
    with pytest.raises(GlobalPolicyNotAssignable):
        assert_policy_assignable(None, WS, pid)  # type: ignore[arg-type]


def test_global_policy_allowed_for_operator(monkeypatch) -> None:
    pid = uuid.uuid4()
    _visibility(monkeypatch, {pid: None})
    assert_policy_assignable(None, WS, pid, allow_global=True)  # type: ignore[arg-type]


def test_cross_workspace_still_rejected_for_operator(monkeypatch) -> None:
    pid = uuid.uuid4()
    _visibility(monkeypatch, {pid: OTHER_WS})
    with pytest.raises(CrossWorkspaceReference):
        assert_policy_assignable(None, WS, pid, allow_global=True)  # type: ignore[arg-type]


def _rule_body(competitor_id, policy_id, **over):
    body = {
        "competitor_id": str(competitor_id), "domain": "amazon.sa",
        "access_policy_id": str(policy_id), "max_concurrent_requests": 2,
        "max_requests_per_minute": 30, "cooldown_seconds": 0,
    }
    body.update(over)
    return body


def test_tenant_linking_global_policy_is_403(client, session, monkeypatch) -> None:
    comp = Competitor(id=uuid.uuid4(), workspace_id=WS, name="A", domain="amazon.sa")
    session.seed(comp)
    pid = uuid.uuid4()
    _visibility(monkeypatch, {pid: None})
    resp = client.post("/v1/domain-access-rules", json=_rule_body(comp.id, pid))
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"]["code"] == "OPERATOR_POLICY_REQUIRED"


@pytest.mark.parametrize(
    "over",
    [{"max_requests_per_minute": 301}, {"max_requests_per_minute": 0},
     {"max_concurrent_requests": 17}, {"max_concurrent_requests": 0}],
)
def test_rule_over_limit_is_422(client, over) -> None:
    resp = client.post(
        "/v1/domain-access-rules", json=_rule_body(uuid.uuid4(), uuid.uuid4(), **over)
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "over", [{"max_requests_per_minute": 301}, {"max_concurrent_requests": 17}]
)
def test_competitor_over_limit_is_422(client, over) -> None:
    resp = client.post("/v1/competitors", json={"name": "A", "domain": "amazon.sa", **over})
    assert resp.status_code == 422


def test_competitor_at_ceiling_is_201(client) -> None:
    resp = client.post(
        "/v1/competitors",
        json={"name": "A", "domain": "amazon.sa",
              "max_requests_per_minute": 300, "max_concurrent_requests": 16},
    )
    assert resp.status_code == 201


# --- E7: self-approval -------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [{"robots_policy": "IGNORE_AFTER_APPROVAL"}, {"legal_status": "APPROVED"}],
)
def test_tenant_create_self_approval_is_403(client, over) -> None:
    resp = client.post("/v1/competitors", json={"name": "A", "domain": "amazon.sa", **over})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"]["code"] == "OPERATOR_APPROVAL_REQUIRED"


@pytest.mark.parametrize(
    "over",
    [{"robots_policy": "IGNORE_AFTER_APPROVAL"}, {"legal_status": "APPROVED"}],
)
def test_tenant_update_self_approval_is_403(client, session, over) -> None:
    comp = Competitor(id=uuid.uuid4(), workspace_id=WS, name="A", domain="amazon.sa")
    session.seed(comp)
    resp = client.patch(f"/v1/competitors/{comp.id}", json=over)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"]["code"] == "OPERATOR_APPROVAL_REQUIRED"


def test_tenant_safe_values_still_ok(client, session) -> None:
    now = datetime.now(timezone.utc)
    comp = Competitor(
        id=uuid.uuid4(), workspace_id=WS, name="A", domain="amazon.sa",
        status=CompetitorStatus.ACTIVE, legal_status=LegalStatus.REVIEW_REQUIRED,
        robots_policy=RobotsPolicy.RESPECT, created_at=now, updated_at=now,
    )
    session.seed(comp)
    resp = client.patch(
        f"/v1/competitors/{comp.id}",
        json={"robots_policy": "REVIEW_REQUIRED", "legal_status": "DISABLED"},
    )
    assert resp.status_code == 200


def test_admin_can_approve(session) -> None:
    comp = Competitor(id=uuid.uuid4(), workspace_id=WS, name="A", domain="amazon.sa")
    session.seed(comp)
    app.dependency_overrides[require_service_token] = lambda: None
    app.dependency_overrides[admin.get_admin_session] = lambda: session
    resp = TestClient(app).patch(
        f"/v1/admin/workspaces/{WS}/competitors/{comp.id}/approval",
        json={"robots_policy": "IGNORE_AFTER_APPROVAL", "legal_status": "APPROVED"},
        headers={"Authorization": f"Bearer test-service-{uuid.uuid4()}"},  # unique: avoids shared limiter bucket
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["robots_policy"] == RobotsPolicy.IGNORE_AFTER_APPROVAL
    assert comp.legal_status == LegalStatus.APPROVED
