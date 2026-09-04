"""`/v1/admin/workspaces/{id}/control-plane` — the six SaaS calls (EPA C2).

Drives the shipped router through FastAPI's `TestClient` with the
service-token dependency overridden, exactly as `test_admin_router.py`
does, and with `unit._jobs_fake_session.FakeOrmSession` standing in for
the database — that fake evaluates real `WHERE` clauses, so the negative
cases here (unknown workspace -> 404, a rule that belongs to another
workspace being invisible) are genuinely distinguished rather than
arranged.

The last test in this module replays the SaaS's live contract sequence
(`saas/app/src/server/engine/contract.controlPlane.test.ts`, C4) end to
end in one session: create -> replay -> state -> replace -> entitlement
v5 -> entitlement v4 (ignored) -> quarantine -> state -> delete -> state
-> delete entitlement (405). That test is the one that fails loudest if
any status code in this router drifts.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import control_plane
from app.service_auth import require_service_token
from app_shared.control_plane import service
from app_shared.enums import ProductStatus, ScrapeScope, WorkspaceStatus
from app_shared.models.control_plane import ControlPlaneRule
from app_shared.models.cost_authorization import EntitlementState, WorkspaceEntitlement
from app_shared.models.identity import Workspace
from app_shared.models.refresh_rules import RefreshRule
from unit._jobs_fake_session import FakeOrmSession

SERVICE_HEADERS = {"Authorization": "Bearer test-service-token"}
_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def session() -> FakeOrmSession:
    return FakeOrmSession()


@pytest.fixture()
def workspace_id(session: FakeOrmSession) -> uuid.UUID:
    workspace = Workspace(
        id=uuid.uuid4(), name="Acme", slug="acme", status=WorkspaceStatus.ACTIVE
    )
    session.seed(workspace)
    return workspace.id


@pytest.fixture(autouse=True)
def _authorized(session: FakeOrmSession) -> None:
    app.dependency_overrides[require_service_token] = lambda: None
    app.dependency_overrides[control_plane.get_control_plane_session] = lambda: session


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _base(workspace_id: uuid.UUID) -> str:
    return f"/v1/admin/workspaces/{workspace_id}/control-plane"


def _rule_body(**over) -> dict:
    body = {
        "external_id": f"monitor-{uuid.uuid4()}",
        "kind": "MONITOR",
        "cadence": "DAILY",
        "enabled": True,
        "target_external_ids": [],
        "owner_tag": "crawmatic-saas:ws",
    }
    body.update(over)
    return body


def _entitlement_body(**over) -> dict:
    body = {
        "plan": "starter",
        "status": "ACTIVE",
        "product_ceiling": 5,
        "as_of": _NOW.isoformat(),
        "evidence_version": 5,
    }
    body.update(over)
    return body


# --- auth ---------------------------------------------------------------


def test_control_plane_routes_require_the_service_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the override the real seam must refuse (mirrors
    `test_admin_router.test_admin_routes_require_the_service_token`)."""

    class _Settings:
        SAAS_SERVICE_TOKEN = "s3cret-service-token"

    monkeypatch.setattr("app.service_auth.get_settings", lambda: _Settings())
    app.dependency_overrides.clear()
    with TestClient(app) as bare:
        resp = bare.get(f"/v1/admin/workspaces/{uuid.uuid4()}/control-plane/state")
    assert resp.status_code == 401


# --- rules --------------------------------------------------------------


def test_create_rule_returns_201_and_an_id(client, session, workspace_id):
    resp = client.post(
        f"{_base(workspace_id)}/rules", json=_rule_body(), headers=SERVICE_HEADERS
    )
    assert resp.status_code == 201
    assert uuid.UUID(resp.json()["id"])


def test_create_rule_again_replays_as_200_with_the_same_id(
    client, session, workspace_id
):
    body = _rule_body()
    first = client.post(
        f"{_base(workspace_id)}/rules", json=body, headers=SERVICE_HEADERS
    )
    second = client.post(
        f"{_base(workspace_id)}/rules", json=body, headers=SERVICE_HEADERS
    )
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    rules = [o for o in session.added if isinstance(o, ControlPlaneRule)]
    assert len(rules) == 1


def test_state_echoes_the_created_rule_in_snake_case(client, session, workspace_id):
    body = _rule_body(target_external_ids=["p-1", "p-2"])
    client.post(f"{_base(workspace_id)}/rules", json=body, headers=SERVICE_HEADERS)

    resp = client.get(f"{_base(workspace_id)}/state", headers=SERVICE_HEADERS)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["entitlement"] is None
    (rule,) = payload["rules"]
    assert set(rule) == {
        "external_id",
        "kind",
        "cadence",
        "enabled",
        "target_external_ids",
        "owner_tag",
        "quarantined",
    }
    assert rule["external_id"] == body["external_id"]
    assert rule["kind"] == "MONITOR"
    assert rule["cadence"] == "DAILY"
    assert rule["enabled"] is True
    assert rule["quarantined"] is False
    assert rule["target_external_ids"] == ["p-1", "p-2"]


def test_state_does_not_leak_another_workspaces_rule(client, session, workspace_id):
    other = Workspace(
        id=uuid.uuid4(), name="Other", slug="other", status=WorkspaceStatus.ACTIVE
    )
    session.seed(other)
    client.post(
        f"{_base(other.id)}/rules",
        json=_rule_body(external_id="monitor-other"),
        headers=SERVICE_HEADERS,
    )

    resp = client.get(f"{_base(workspace_id)}/state", headers=SERVICE_HEADERS)
    assert resp.json()["rules"] == []


def test_replace_rule_is_204_and_updates_the_row(client, session, workspace_id):
    body = _rule_body()
    client.post(f"{_base(workspace_id)}/rules", json=body, headers=SERVICE_HEADERS)

    resp = client.put(
        f"{_base(workspace_id)}/rules/{body['external_id']}",
        json={
            "kind": "MONITOR",
            "cadence": "WEEKLY",
            "enabled": True,
            "target_external_ids": ["p-9"],
        },
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 204
    assert resp.content == b""

    state = client.get(f"{_base(workspace_id)}/state", headers=SERVICE_HEADERS).json()
    assert state["rules"][0]["cadence"] == "WEEKLY"
    assert state["rules"][0]["target_external_ids"] == ["p-9"]
    # The create-path owner tag survives a replace body that carries none.
    assert state["rules"][0]["owner_tag"] == body["owner_tag"]


def test_unknown_cadence_is_422(client, session, workspace_id):
    resp = client.post(
        f"{_base(workspace_id)}/rules",
        json=_rule_body(cadence="EVERY_3_MINUTES"),
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 422


def test_unknown_kind_is_422(client, session, workspace_id):
    resp = client.post(
        f"{_base(workspace_id)}/rules",
        json=_rule_body(kind="DELETE_EVERYTHING"),
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 422


def test_unknown_entity_type_is_422(client, session, workspace_id):
    resp = client.delete(
        f"{_base(workspace_id)}/widget/x", headers=SERVICE_HEADERS
    )
    assert resp.status_code == 422


def test_unknown_workspace_is_404(client, session):
    resp = client.get(
        f"/v1/admin/workspaces/{uuid.uuid4()}/control-plane/state",
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_the_idempotency_key_header_is_accepted(client, session, workspace_id):
    resp = client.post(
        f"{_base(workspace_id)}/rules",
        json=_rule_body(),
        headers={**SERVICE_HEADERS, "Idempotency-Key": "saas:ws:rule:x:v1"},
    )
    assert resp.status_code == 201


# --- refresh-rule wiring ------------------------------------------------


def test_a_monitor_rule_creates_a_workspace_refresh_rule(
    client, session, workspace_id
):
    client.post(
        f"{_base(workspace_id)}/rules",
        json=_rule_body(external_id="monitor-abc", cadence="EVERY_6H"),
        headers=SERVICE_HEADERS,
    )
    refresh = [o for o in session.added if isinstance(o, RefreshRule)]
    assert len(refresh) == 1
    assert refresh[0].name == "cp:monitor-abc"
    assert refresh[0].scope is ScrapeScope.WORKSPACE
    assert refresh[0].interval_minutes == 360
    assert refresh[0].enabled is True
    assert refresh[0].next_run_at is not None


def test_a_monitor_rule_adopts_an_existing_workspace_refresh_rule(
    client, session, workspace_id
):
    """Mushtryati already has a daily workspace sweep; declaring a monitor
    must drive THAT rule, never add a second one alongside it."""
    existing = RefreshRule(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name="daily-workspace-sweep",
        scope=ScrapeScope.WORKSPACE,
        interval_minutes=1440,
        enabled=True,
    )
    session.seed(existing)

    client.post(
        f"{_base(workspace_id)}/rules",
        json=_rule_body(cadence="HOURLY"),
        headers=SERVICE_HEADERS,
    )

    assert [o for o in session.added if isinstance(o, RefreshRule)] == []
    assert existing.interval_minutes == 60
    (rule,) = [o for o in session.added if isinstance(o, ControlPlaneRule)]
    assert rule.refresh_rule_id == existing.id


def test_a_reprice_rule_schedules_nothing(client, session, workspace_id):
    client.post(
        f"{_base(workspace_id)}/rules",
        json=_rule_body(kind="REPRICE"),
        headers=SERVICE_HEADERS,
    )
    assert [o for o in session.added if isinstance(o, RefreshRule)] == []


def test_a_disabled_rule_disables_its_refresh_rule(client, session, workspace_id):
    body = _rule_body()
    client.post(f"{_base(workspace_id)}/rules", json=body, headers=SERVICE_HEADERS)
    client.put(
        f"{_base(workspace_id)}/rules/{body['external_id']}",
        json={
            "kind": "MONITOR",
            "cadence": "DAILY",
            "enabled": False,
            "target_external_ids": [],
        },
        headers=SERVICE_HEADERS,
    )
    (refresh,) = [o for o in session.added if isinstance(o, RefreshRule)]
    assert refresh.enabled is False


# --- quarantine + delete ------------------------------------------------


def test_quarantine_flags_the_rule_and_disables_its_refresh_rule(
    client, session, workspace_id
):
    body = _rule_body()
    client.post(f"{_base(workspace_id)}/rules", json=body, headers=SERVICE_HEADERS)

    resp = client.post(
        f"{_base(workspace_id)}/rule/{body['external_id']}/quarantine",
        json={
            "reason": "out of entitlement",
            "ownership_proof": body["owner_tag"],
            "grace_until": (_NOW + timedelta(hours=1)).isoformat(),
        },
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 204

    (refresh,) = [o for o in session.added if isinstance(o, RefreshRule)]
    assert refresh.enabled is False

    state = client.get(f"{_base(workspace_id)}/state", headers=SERVICE_HEADERS).json()
    assert state["rules"][0]["quarantined"] is True

    (rule,) = [o for o in session.added if isinstance(o, ControlPlaneRule)]
    assert rule.quarantine_reason == "out of entitlement"
    assert rule.grace_until is not None


def test_quarantine_refuses_a_mismatched_ownership_proof(
    client, session, workspace_id
):
    body = _rule_body(owner_tag="crawmatic-saas:tenant-a")
    client.post(f"{_base(workspace_id)}/rules", json=body, headers=SERVICE_HEADERS)

    resp = client.post(
        f"{_base(workspace_id)}/rule/{body['external_id']}/quarantine",
        json={"reason": "x", "ownership_proof": "someone-else"},
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "OWNERSHIP_PROOF_MISMATCH"


def test_quarantine_of_an_unknown_rule_is_404(client, session, workspace_id):
    resp = client.post(
        f"{_base(workspace_id)}/rule/nope/quarantine",
        json={"reason": "x"},
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 404


def test_quarantining_the_entitlement_suspends_it(client, session, workspace_id):
    client.put(
        f"{_base(workspace_id)}/entitlement",
        json=_entitlement_body(),
        headers=SERVICE_HEADERS,
    )
    resp = client.post(
        f"{_base(workspace_id)}/entitlement/entitlement/quarantine",
        json={"reason": "billing killed"},
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 204
    (row,) = [o for o in session.added if isinstance(o, WorkspaceEntitlement)]
    assert row.state is EntitlementState.SUSPENDED


def test_delete_rule_removes_it_and_disables_the_refresh_rule(
    client, session, workspace_id
):
    body = _rule_body()
    client.post(f"{_base(workspace_id)}/rules", json=body, headers=SERVICE_HEADERS)

    resp = client.delete(
        f"{_base(workspace_id)}/rule/{body['external_id']}", headers=SERVICE_HEADERS
    )
    assert resp.status_code == 204

    state = client.get(f"{_base(workspace_id)}/state", headers=SERVICE_HEADERS).json()
    assert state["rules"] == []
    (refresh,) = [o for o in session.added if isinstance(o, RefreshRule)]
    assert refresh.enabled is False


def test_delete_of_an_unknown_rule_is_404(client, session, workspace_id):
    resp = client.delete(
        f"{_base(workspace_id)}/rule/nope", headers=SERVICE_HEADERS
    )
    assert resp.status_code == 404


def test_delete_entitlement_is_405(client, session, workspace_id):
    resp = client.delete(
        f"{_base(workspace_id)}/entitlement/entitlement", headers=SERVICE_HEADERS
    )
    assert resp.status_code == 405
    assert resp.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


# --- entitlement replication -------------------------------------------


def test_entitlement_replication_is_204_and_stores_the_version_as_text(
    client, session, workspace_id
):
    resp = client.put(
        f"{_base(workspace_id)}/entitlement",
        json=_entitlement_body(evidence_version=5),
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 204
    (row,) = [o for o in session.added if isinstance(o, WorkspaceEntitlement)]
    assert row.evidence_version == "5"
    assert row.plan_code == "starter"
    assert row.product_ceiling == 5
    assert row.state is EntitlementState.ACTIVE


def test_a_lower_evidence_version_is_ignored_as_stale(client, session, workspace_id):
    client.put(
        f"{_base(workspace_id)}/entitlement",
        json=_entitlement_body(evidence_version=5),
        headers=SERVICE_HEADERS,
    )
    resp = client.put(
        f"{_base(workspace_id)}/entitlement",
        json=_entitlement_body(evidence_version=4, plan="enterprise"),
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json() == {"ignored": "stale_evidence"}
    (row,) = [o for o in session.added if isinstance(o, WorkspaceEntitlement)]
    assert row.plan_code == "starter"
    assert row.evidence_version == "5"


def test_an_equal_evidence_version_is_ignored_as_stale(client, session, workspace_id):
    client.put(
        f"{_base(workspace_id)}/entitlement",
        json=_entitlement_body(evidence_version=5),
        headers=SERVICE_HEADERS,
    )
    resp = client.put(
        f"{_base(workspace_id)}/entitlement",
        json=_entitlement_body(evidence_version=5),
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json() == {"ignored": "stale_evidence"}


def test_saas_evidence_outranks_a_seeded_placeholder(client, session, workspace_id):
    """`seeded-YYYY-MM-DD` rows are placeholders written by the engine's own
    seeder — they must never out-version real billing evidence, so they
    read as version 0."""
    seeded = WorkspaceEntitlement(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        state=EntitlementState.SUSPENDED,
        plan_code=None,
        evidence_version="seeded-2026-08-26",
        observed_at=_NOW,
    )
    session.seed(seeded)

    resp = client.put(
        f"{_base(workspace_id)}/entitlement",
        json=_entitlement_body(evidence_version=1),
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 204
    assert seeded.evidence_version == "1"
    assert seeded.state is EntitlementState.ACTIVE


def test_an_unparseable_stored_version_reads_as_zero_and_warns(
    client, session, workspace_id, caplog
):
    stored = WorkspaceEntitlement(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        state=EntitlementState.ACTIVE,
        plan_code="starter",
        evidence_version="saas-billing-v7",
        observed_at=_NOW,
    )
    session.seed(stored)

    with caplog.at_level(logging.WARNING, logger="app_shared.control_plane.service"):
        resp = client.put(
            f"{_base(workspace_id)}/entitlement",
            json=_entitlement_body(evidence_version=1),
            headers=SERVICE_HEADERS,
        )

    assert resp.status_code == 204
    assert stored.evidence_version == "1"
    assert any("non-numeric" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    ("saas_status", "engine_state"),
    [
        ("ACTIVE", EntitlementState.ACTIVE),
        ("DELINQUENT", EntitlementState.PAST_DUE),
        ("PAUSED", EntitlementState.SUSPENDED),
        ("KILLED", EntitlementState.SUSPENDED),
        ("CANCELED", EntitlementState.CANCELLED),
    ],
)
def test_every_saas_status_maps_to_an_engine_state(
    client, session, workspace_id, saas_status, engine_state
):
    resp = client.put(
        f"{_base(workspace_id)}/entitlement",
        json=_entitlement_body(status=saas_status),
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 204
    (row,) = [o for o in session.added if isinstance(o, WorkspaceEntitlement)]
    assert row.state is engine_state


def test_an_unknown_saas_status_is_422(client, session, workspace_id):
    resp = client.put(
        f"{_base(workspace_id)}/entitlement",
        json=_entitlement_body(status="BANKRUPT"),
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    ("engine_state", "echoed"),
    [
        (EntitlementState.ACTIVE, "ACTIVE"),
        (EntitlementState.PAST_DUE, "DELINQUENT"),
        (EntitlementState.SUSPENDED, "PAUSED"),
        (EntitlementState.CANCELLED, "CANCELED"),
    ],
)
def test_state_echoes_the_status_in_the_saas_vocabulary(
    client, session, workspace_id, engine_state, echoed
):
    """The reconciler compares this field against ITS vocabulary — echoing
    the engine's spelling would report drift that can never be fixed."""
    session.seed(
        WorkspaceEntitlement(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            state=engine_state,
            plan_code="starter",
            evidence_version="5",
            product_ceiling=25,
            observed_at=_NOW,
        )
    )

    body = client.get(f"{_base(workspace_id)}/state", headers=SERVICE_HEADERS).json()
    assert body["entitlement"] == {
        "plan": "starter",
        "status": echoed,
        "product_ceiling": 25,
        "as_of": body["entitlement"]["as_of"],
        "evidence_version": "5",
    }


def test_a_null_product_ceiling_round_trips_as_null(client, session, workspace_id):
    client.put(
        f"{_base(workspace_id)}/entitlement",
        json=_entitlement_body(product_ceiling=None),
        headers=SERVICE_HEADERS,
    )
    body = client.get(f"{_base(workspace_id)}/state", headers=SERVICE_HEADERS).json()
    assert body["entitlement"]["product_ceiling"] is None


# --- the C4 live-contract sequence, end to end --------------------------


def test_the_saas_live_contract_sequence(client, session, workspace_id):
    base = _base(workspace_id)
    external_id = f"monitor-{uuid.uuid4()}"
    owner_tag = f"crawmatic-saas:{workspace_id}"
    rule_body = {
        "external_id": external_id,
        "kind": "MONITOR",
        "cadence": "DAILY",
        "enabled": True,
        "target_external_ids": [],
        "owner_tag": owner_tag,
    }

    created = client.post(f"{base}/rules", json=rule_body, headers=SERVICE_HEADERS)
    assert created.status_code == 201
    rule_id = created.json()["id"]

    replayed = client.post(f"{base}/rules", json=rule_body, headers=SERVICE_HEADERS)
    assert replayed.status_code == 200
    assert replayed.json()["id"] == rule_id

    state = client.get(f"{base}/state", headers=SERVICE_HEADERS)
    assert state.status_code == 200
    assert state.json()["rules"][0]["external_id"] == external_id

    replaced = client.put(
        f"{base}/rules/{external_id}",
        json={
            "kind": "MONITOR",
            "cadence": "WEEKLY",
            "enabled": True,
            "target_external_ids": [],
        },
        headers=SERVICE_HEADERS,
    )
    assert replaced.status_code == 204

    fresh = client.put(
        f"{base}/entitlement",
        json=_entitlement_body(evidence_version=5),
        headers=SERVICE_HEADERS,
    )
    assert fresh.status_code == 204

    stale = client.put(
        f"{base}/entitlement",
        json=_entitlement_body(evidence_version=4),
        headers=SERVICE_HEADERS,
    )
    assert stale.status_code == 200
    assert stale.json() == {"ignored": "stale_evidence"}

    quarantined = client.post(
        f"{base}/rule/{external_id}/quarantine",
        json={
            "reason": "C4 live contract test cleanup",
            "ownership_proof": owner_tag,
            "grace_until": (_NOW + timedelta(hours=1)).isoformat(),
        },
        headers=SERVICE_HEADERS,
    )
    assert quarantined.status_code == 204

    after = client.get(f"{base}/state", headers=SERVICE_HEADERS).json()
    assert after["rules"][0]["quarantined"] is True

    deleted = client.delete(f"{base}/rule/{external_id}", headers=SERVICE_HEADERS)
    assert deleted.status_code == 204

    gone = client.get(f"{base}/state", headers=SERVICE_HEADERS).json()
    assert all(r["external_id"] != external_id for r in gone["rules"])

    refused = client.delete(
        f"{base}/entitlement/entitlement", headers=SERVICE_HEADERS
    )
    assert refused.status_code == 405


# --- the seeder never touches SaaS-written evidence ---------------------


def test_control_plane_evidence_versions_are_not_seeded_versions() -> None:
    """`refresh_seeded_entitlements` re-stamps rows whose `evidence_version`
    starts with `seeded-` and nothing else. Everything this router writes is
    `str(int)`, so the two writers can never fight over a row."""
    from app_shared.costauth.entitlements import is_seeded_evidence_version

    assert not is_seeded_evidence_version("5")
    assert not is_seeded_evidence_version("0")
    assert not is_seeded_evidence_version(None)
    assert is_seeded_evidence_version("seeded-2026-08-26")


def test_a_seeded_row_reads_as_version_zero_but_a_numeric_one_does_not() -> None:
    def _row(version: str | None) -> WorkspaceEntitlement:
        return WorkspaceEntitlement(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            state=EntitlementState.ACTIVE,
            evidence_version=version,
            observed_at=_NOW,
        )

    assert service.stored_evidence_version(None) == 0
    assert service.stored_evidence_version(_row(None)) == 0
    assert service.stored_evidence_version(_row("seeded-2026-08-26")) == 0
    assert service.stored_evidence_version(_row("7")) == 7


def test_product_status_active_is_the_ceiling_denominator() -> None:
    """Guards the one enum the ceiling check counts on (`ProductStatus`
    has exactly two members; archived products do not consume a seat)."""
    assert {member.value for member in ProductStatus} == {"active", "archived"}
