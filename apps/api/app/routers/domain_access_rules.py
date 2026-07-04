"""Domain-access-rule endpoints (`contracts/api-access.md`) — SPEC-10 US1.

Every endpoint runs on the SPEC-03 auth seam (`app.deps.get_current_principal`
=> `set_workspace_context` already applied to the yielded session) and is
scope-gated via `app.deps.require_scopes(...)`. `domain_access_rules` is
**tenant-only** (`WorkspaceScopedBase`, registered in
`app_shared.repository.WORKSPACE_OWNED_MODELS`) — standard
`scoped_select`/`scoped_get` CRUD, same discipline as
`routers/competitors.py`.

`competitor_id` must resolve to a competitor in the caller's own
workspace (checked via the narrow unscoped `id IN (...)` lookup +
`app_shared.catalog.consistency.assert_refs_in_workspace`, mirroring
`routers/matches.py`'s `_resolve_competitor` — `422` cross-workspace /
`404` dangling). `access_policy_id` is checked via
`app_shared.access.repository.assert_policy_assignable` (own or global
-> OK, cross-workspace -> `422`, dangling -> `404`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_shared.access.repository import assert_policy_assignable
from app_shared.catalog.consistency import (
    CrossWorkspaceReference,
    MissingReference,
    assert_refs_in_workspace,
)
from app_shared.models.access import DomainAccessRule
from app_shared.models.competitors_matches import Competitor
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.repository import scoped_get, scoped_select

from app.deps import Principal, require_scopes
from app.schemas.access import (
    DeleteOutcome,
    DomainAccessRuleCreate,
    DomainAccessRuleListResponse,
    DomainAccessRuleResponse,
    DomainAccessRuleUpdate,
)

router = APIRouter(prefix="/v1/domain-access-rules", tags=["domain-access-rules"])


# --- error builders ------------------------------------------------------


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


def _duplicate_rule(message: str) -> HTTPException:
    return HTTPException(
        status_code=409, detail={"error": {"code": "DUPLICATE_DOMAIN_RULE", "message": message}}
    )


def _workspace_mismatch(message: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "WORKSPACE_MISMATCH", "message": message}},
    )


def _workspace_map(session: Session, model: type, ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """One narrow, unscoped ``id IN (...)`` lookup -> ``{id: workspace_id}``.

    Deliberately unscoped (mirrors `routers/matches.py`) so a
    cross-workspace reference can be told apart from a nonexistent one
    before `assert_refs_in_workspace` turns either into a clean
    `422`/`404` — never a raw `IntegrityError`.
    """
    if not ids:
        return {}
    rows = session.execute(
        select(model.id, model.workspace_id).where(model.id.in_(ids))  # noqa: workspace-scope
    ).all()
    return {row.id: row.workspace_id for row in rows}


def _check_competitor_in_workspace(
    session: Session, workspace_id: uuid.UUID, competitor_id: uuid.UUID
) -> None:
    resolved = _workspace_map(session, Competitor, [competitor_id])
    try:
        assert_refs_in_workspace(workspace_id, [competitor_id], resolved)
    except MissingReference as exc:
        raise _not_found("Competitor not found.") from exc
    except CrossWorkspaceReference as exc:
        raise _workspace_mismatch("Competitor belongs to a different workspace.") from exc


def _check_policy_assignable(
    session: Session, workspace_id: uuid.UUID, policy_id: uuid.UUID | None
) -> None:
    try:
        assert_policy_assignable(session, workspace_id, policy_id)
    except MissingReference as exc:
        raise _not_found("Access policy not found.") from exc
    except CrossWorkspaceReference as exc:
        raise _workspace_mismatch("Access policy belongs to a different workspace.") from exc


# --- endpoints -------------------------------------------------------------


@router.post("", response_model=DomainAccessRuleResponse, status_code=201)
def create_domain_access_rule(
    payload: DomainAccessRuleCreate,
    principal_ctx: tuple = Depends(require_scopes("domain_rules:write")),
) -> DomainAccessRuleResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    _check_competitor_in_workspace(session, ws, payload.competitor_id)
    _check_policy_assignable(session, ws, payload.access_policy_id)

    rule = DomainAccessRule(
        workspace_id=ws,
        competitor_id=payload.competitor_id,
        domain=payload.domain,
        url_pattern=payload.url_pattern,
        url_pattern_override=payload.url_pattern_override,
        access_policy_id=payload.access_policy_id,
        max_concurrent_requests=payload.max_concurrent_requests,
        max_requests_per_minute=payload.max_requests_per_minute,
        cooldown_seconds=payload.cooldown_seconds,
        block_detection_rules=payload.block_detection_rules,
        enabled=payload.enabled,
    )
    session.add(rule)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_rule(
            "A domain access rule for this (competitor, domain, url_pattern) "
            "already exists in this workspace."
        ) from exc

    return DomainAccessRuleResponse.model_validate(rule)


@router.get("", response_model=DomainAccessRuleListResponse)
def list_domain_access_rules(
    limit: int | None = None,
    cursor: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("domain_rules:read")),
) -> DomainAccessRuleListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(DomainAccessRule, principal.workspace_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(DomainAccessRule, after))
    stmt = stmt.order_by(DomainAccessRule.created_at, DomainAccessRule.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [DomainAccessRuleResponse.model_validate(r) for r in envelope["items"]]
    return DomainAccessRuleListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/{rule_id}", response_model=DomainAccessRuleResponse)
def get_domain_access_rule(
    rule_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("domain_rules:read")),
) -> DomainAccessRuleResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    rule = scoped_get(session, DomainAccessRule, rule_id, principal.workspace_id)
    if rule is None:
        raise _not_found("Domain access rule not found.")

    return DomainAccessRuleResponse.model_validate(rule)


@router.patch("/{rule_id}", response_model=DomainAccessRuleResponse)
def update_domain_access_rule(
    rule_id: uuid.UUID,
    payload: DomainAccessRuleUpdate,
    principal_ctx: tuple = Depends(require_scopes("domain_rules:write")),
) -> DomainAccessRuleResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    rule = scoped_get(session, DomainAccessRule, rule_id, ws)
    if rule is None:
        raise _not_found("Domain access rule not found.")

    updates = payload.model_dump(exclude_unset=True)
    if "competitor_id" in updates:
        _check_competitor_in_workspace(session, ws, updates["competitor_id"])
    if "access_policy_id" in updates:
        _check_policy_assignable(session, ws, updates["access_policy_id"])

    for field, value in updates.items():
        setattr(rule, field, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_rule(
            "A domain access rule for this (competitor, domain, url_pattern) "
            "already exists in this workspace."
        ) from exc

    return DomainAccessRuleResponse.model_validate(rule)


@router.delete("/{rule_id}", response_model=DeleteOutcome)
def delete_domain_access_rule(
    rule_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("domain_rules:write")),
) -> DeleteOutcome:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    rule = scoped_get(session, DomainAccessRule, rule_id, principal.workspace_id)
    if rule is None:
        raise _not_found("Domain access rule not found.")

    session.delete(rule)
    session.flush()

    return DeleteOutcome(id=rule_id, outcome="hard_deleted")
