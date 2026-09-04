"""SaaS workspace control-plane endpoints (EPA C2, 2026-09-03).

The engine half of the six-call contract the SaaS pins in
``src/server/engine/engineControlPlane.ts``: declare monitor/reprice
rules, replicate billing entitlement evidence, quarantine or retire
either, and read back everything the engine currently believes about a
tenant.

Auth and session posture are `routers/admin.py`'s, for the same reason:
the caller is the SaaS control plane holding a static service token, not
a customer, so the surface is guarded by
`app.service_auth.require_service_token` and runs on `get_auth_session()`
(BYPASSRLS). Unlike `admin.py` every route here is nonetheless
workspace-ADDRESSED — the workspace id is in the path — so every
statement in `app_shared.control_plane.service` predicates
`workspace_id` explicitly and each route 404s on a workspace that does
not exist rather than writing rows nobody can read back.

**Idempotency.** `Idempotency-Key` is accepted and deliberately ignored.
Every route here is naturally idempotent without it: rules are addressed
by the SaaS's own `external_id` (a replayed create updates the row it
already wrote and answers 200 with the same id), entitlement writes are
monotonic in `evidence_version`, and quarantine/delete are
set-to-a-state operations. Storing keys would add a table whose only job
is to reproduce an answer these routes can always recompute.

**Status codes are the contract.** 201-then-200 on create, 204 on every
mutation that returns nothing, and 200 `{"ignored": "stale_evidence"}`
for an entitlement push the engine already has better evidence than —
the SaaS distinguishes those three, so they are asserted by the live
contract test on its side and by `tests/unit/test_control_plane_routes.py`
here.

This router is internal-only and must never be published in the
customer-facing API docs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from enum import Enum

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app_shared.control_plane import service
from app_shared.database import get_auth_session

from app.service_auth import require_service_token

router = APIRouter(
    prefix="/v1/admin/workspaces/{workspace_id}/control-plane",
    # `admin` FIRST and deliberately: `app.openapi_public.INTERNAL_TAGS`
    # excludes a route when ANY of its tags is internal, and that tag is
    # the mechanism by which this whole surface stays out of the
    # customer-facing spec. `control-plane` rides alongside it purely so
    # the internal spec groups these six routes on their own.
    tags=["admin", "control-plane"],
    dependencies=[Depends(require_service_token)],
)


def get_control_plane_session() -> Iterator[Session]:
    """Session seam for the control plane — BYPASSRLS, workspace-addressed.

    A separate dependency (rather than reusing `admin.get_admin_session`)
    so a test can override exactly this router's session, and so the two
    surfaces can diverge later without one silently changing the other.
    """
    with get_auth_session() as session:
        yield session
        session.commit()


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


# --- wire vocabulary ----------------------------------------------------


class RuleKind(str, Enum):
    """What the SaaS asked for. `REPRICE` is stored and never scheduled —
    the engine has no repricing executor; the SaaS runs pricing locally."""

    MONITOR = "MONITOR"
    REPRICE = "REPRICE"


class RuleCadence(str, Enum):
    """The coarse, named cadence the SaaS sells. Validated HERE rather
    than in the schema (C1: `control_plane_rules.cadence` is deliberately
    plain TEXT) so an unknown value is a readable 422, not a CHECK
    violation surfacing as a 500."""

    HOURLY = "HOURLY"
    EVERY_6H = "EVERY_6H"
    TWICE_DAILY = "TWICE_DAILY"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"


class SaasEntitlementStatus(str, Enum):
    """The SaaS's OWN status vocabulary, never the engine's.

    Mapped to `EntitlementState` in exactly one place
    (`app_shared.control_plane.service.SAAS_STATUS_TO_ENGINE_STATE`), so
    a status added on the SaaS side fails loudly here instead of being
    half-translated by a mapping table living in the caller.
    """

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DELINQUENT = "DELINQUENT"
    CANCELED = "CANCELED"
    KILLED = "KILLED"


class ControlPlaneEntity(str, Enum):
    """The two addressable entity types. Anything else is a 422."""

    RULE = "rule"
    ENTITLEMENT = "entitlement"


# --- request/response schemas -------------------------------------------
#
# Deliberately NOT `extra="forbid"` (the posture `app.schemas.admin` takes
# for the customer-facing provisioning body): this is a machine-to-machine
# contract between two deploys that ship independently, and a field added
# on the SaaS side must roll out without a synchronized engine release.


class RuleCreateRequest(BaseModel):
    external_id: str = Field(min_length=1, max_length=200)
    kind: RuleKind
    cadence: RuleCadence
    enabled: bool = True
    target_external_ids: list[str] = Field(default_factory=list)
    owner_tag: str | None = None


class RuleReplaceRequest(BaseModel):
    kind: RuleKind
    cadence: RuleCadence
    enabled: bool = True
    target_external_ids: list[str] = Field(default_factory=list)


class QuarantineRequest(BaseModel):
    reason: str = Field(min_length=1)
    ownership_proof: str | None = None
    grace_until: datetime | None = None


class EntitlementReplicationRequest(BaseModel):
    plan: str
    status: SaasEntitlementStatus
    product_ceiling: int | None = None
    as_of: datetime
    evidence_version: int


class RuleCreateResponse(BaseModel):
    id: uuid.UUID


class RuleStateItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    external_id: str
    kind: str
    cadence: str
    enabled: bool
    target_external_ids: list[str]
    owner_tag: str | None
    quarantined: bool


class EntitlementStateItem(BaseModel):
    plan: str | None
    status: str
    product_ceiling: int | None
    as_of: datetime | None
    evidence_version: str | None


class ControlPlaneStateResponse(BaseModel):
    rules: list[RuleStateItem]
    entitlement: EntitlementStateItem | None


# --- routes -------------------------------------------------------------


def _require_workspace(session: Session, workspace_id: uuid.UUID) -> None:
    if not service.workspace_exists(session, workspace_id):
        raise _not_found("Workspace not found.")


@router.get("/state", response_model=ControlPlaneStateResponse)
def get_control_plane_state(
    workspace_id: uuid.UUID,
    session: Session = Depends(get_control_plane_session),
) -> ControlPlaneStateResponse:
    """Everything the engine currently believes about this tenant.

    The entitlement `status` is echoed in the SaaS's OWN vocabulary, not
    the engine's: the reconciler compares this field against its desired
    state, so answering `PAST_DUE` to a tenant it calls `DELINQUENT`
    would report permanent, unfixable drift.
    """
    _require_workspace(session, workspace_id)

    rules = [
        RuleStateItem(
            external_id=row.external_id,
            kind=row.kind,
            cadence=row.cadence,
            enabled=bool(row.enabled),
            target_external_ids=list(row.target_external_ids or []),
            owner_tag=row.owner_tag,
            quarantined=bool(row.quarantined),
        )
        for row in service.list_rules(session, workspace_id)
    ]

    row = service.get_entitlement(session, workspace_id)
    entitlement = (
        None
        if row is None
        else EntitlementStateItem(
            plan=row.plan_code,
            status=service.saas_status_for(row.state),
            product_ceiling=row.product_ceiling,
            as_of=row.observed_at,
            evidence_version=row.evidence_version,
        )
    )
    return ControlPlaneStateResponse(rules=rules, entitlement=entitlement)


@router.post("/rules", response_model=RuleCreateResponse, status_code=201)
def create_rule(
    workspace_id: uuid.UUID,
    payload: RuleCreateRequest,
    response: Response,
    session: Session = Depends(get_control_plane_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RuleCreateResponse:
    """Declare a rule. 201 the first time, 200 with the same id on a replay.

    Both are success on the SaaS side; the distinction exists so an
    operator reading engine access logs can tell a genuine new
    declaration from a redelivery without diffing state.
    """
    _require_workspace(session, workspace_id)
    row, created = service.upsert_rule(
        session,
        workspace_id,
        service.RuleSpec(
            external_id=payload.external_id,
            kind=payload.kind.value,
            cadence=payload.cadence.value,
            enabled=payload.enabled,
            target_external_ids=list(payload.target_external_ids),
            owner_tag=payload.owner_tag,
        ),
        now=datetime.now(timezone.utc),
    )
    if not created:
        response.status_code = 200
    return RuleCreateResponse(id=row.id)


@router.put("/rules/{external_id}", status_code=204)
def replace_rule(
    workspace_id: uuid.UUID,
    external_id: str,
    payload: RuleReplaceRequest,
    session: Session = Depends(get_control_plane_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> None:
    """Full replace of a declared rule, keyed by the SaaS's external id.

    Creates the row when it is missing rather than 404ing: the SaaS
    reconciler's job is to make the engine match a desired state, and
    refusing to converge because a row was lost would need an operator to
    intervene for no benefit.
    """
    _require_workspace(session, workspace_id)
    service.upsert_rule(
        session,
        workspace_id,
        service.RuleSpec(
            external_id=external_id,
            kind=payload.kind.value,
            cadence=payload.cadence.value,
            enabled=payload.enabled,
            target_external_ids=list(payload.target_external_ids),
        ),
        now=datetime.now(timezone.utc),
    )
    return None


@router.post("/{entity_type}/{external_id}/quarantine", status_code=204)
def quarantine_entity(
    workspace_id: uuid.UUID,
    entity_type: ControlPlaneEntity,
    external_id: str,
    payload: QuarantineRequest,
    session: Session = Depends(get_control_plane_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> None:
    """Flag + pause, reversibly, with a reason on the record.

    `ownership_proof` is checked against the stored `owner_tag` when both
    are present: quarantining is destructive to a tenant's monitoring,
    and the SaaS's own contract says the call "must refuse when
    ownership_proof does not match". A rule with NO owner tag was not
    created by an owner-tagging caller and is quarantined without the
    check — the fail-safe direction, since the alternative would leave
    untagged rules unstoppable.
    """
    _require_workspace(session, workspace_id)

    if entity_type is ControlPlaneEntity.ENTITLEMENT:
        row = service.get_entitlement(session, workspace_id)
        if row is None:
            raise _not_found("No entitlement evidence for this workspace.")
        service.quarantine_entitlement(session, row)
        return None

    rule = service.get_rule(session, workspace_id, external_id)
    if rule is None:
        raise _not_found("Control-plane rule not found.")
    if (
        rule.owner_tag is not None
        and payload.ownership_proof is not None
        and rule.owner_tag != payload.ownership_proof
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "OWNERSHIP_PROOF_MISMATCH",
                    "message": (
                        "The supplied ownership_proof does not match this "
                        "rule's owner_tag; refusing to quarantine state "
                        "this caller does not own."
                    ),
                }
            },
        )
    service.quarantine_rule(
        session, rule, reason=payload.reason, grace_until=payload.grace_until
    )
    return None


@router.delete("/{entity_type}/{external_id}", status_code=204)
def delete_entity(
    workspace_id: uuid.UUID,
    entity_type: ControlPlaneEntity,
    external_id: str,
    session: Session = Depends(get_control_plane_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> None:
    """Retire a declared rule. Deleting entitlement evidence is refused.

    405 rather than 403/404 for the entitlement case, because the
    resource exists and the METHOD is what is not allowed: entitlement
    evidence is the engine's fail-closed brake on paid work, and a
    tenant with no row is denied — so "delete" and "suspend" would be
    indistinguishable in effect while differing completely in what an
    operator can later reconstruct. Suspending is the supported verb
    (`POST .../entitlement/{id}/quarantine`).
    """
    _require_workspace(session, workspace_id)

    if entity_type is ControlPlaneEntity.ENTITLEMENT:
        raise HTTPException(
            status_code=405,
            detail={
                "error": {
                    "code": "METHOD_NOT_ALLOWED",
                    "message": (
                        "Entitlement evidence cannot be deleted; suspend it "
                        "with the quarantine route instead."
                    ),
                }
            },
        )

    rule = service.get_rule(session, workspace_id, external_id)
    if rule is None:
        raise _not_found("Control-plane rule not found.")
    service.delete_rule(session, rule)
    return None


@router.put("/entitlement", status_code=204, response_model=None)
def replicate_entitlement(
    workspace_id: uuid.UUID,
    payload: EntitlementReplicationRequest,
    session: Session = Depends(get_control_plane_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response | None:
    """Replicate billing evidence. 204 applied, 200 ignored-as-stale.

    "Ignored" is a 200 and not an error on purpose: the engine already
    holds evidence at or above this version, nothing is broken, and a
    retry cannot help — but it is emphatically not a replication either,
    so the SaaS is told rather than left to assume its push landed.
    """
    _require_workspace(session, workspace_id)
    applied = service.replicate_entitlement(
        session,
        workspace_id,
        service.EntitlementWrite(
            plan=payload.plan,
            status=payload.status.value,
            product_ceiling=payload.product_ceiling,
            as_of=payload.as_of,
            evidence_version=payload.evidence_version,
        ),
    )
    if not applied:
        return JSONResponse(status_code=200, content={"ignored": "stale_evidence"})
    return None
