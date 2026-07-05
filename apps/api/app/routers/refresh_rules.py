"""Refresh rules endpoints (`contracts/refresh-rules-api.md`) — SPEC-13 US1.

Every endpoint runs on the SPEC-03 auth seam (`app.deps.get_current_principal`
=> `set_workspace_context` already applied to the yielded session) and is
scope-gated via `app.deps.require_scopes(...)`. All reads/writes go through
`app_shared.repository.scoped_select`/`scoped_get` over the **ordinary
RLS-enforced request session** (never the BYPASSRLS system session the
scheduler pass uses, research R2) — RLS backs them as the second isolation
layer, same discipline as `routers/competitors.py`.

`POST` computes the first `next_run_at` via
`app_shared.scheduling.cadence.compute_next_run_at` (base=now). `PATCH`
re-validates the cadence/scope invariants on the **merged** (existing row +
patch) view and recomputes `next_run_at` **only** when a cadence field
(`cron_expression`/`interval_minutes`) actually changes — an `enabled`
toggle or any cadence-preserving edit deliberately leaves `next_run_at`
untouched (contract; FR-006/FR-016), so a re-enabled rule with a stale past
`next_run_at` fires once on the next scheduler pass by design.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app_shared.enums import ScrapeScope
from app_shared.models.catalog import Product, ProductGroup, ProductVariant
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.refresh_rules import RefreshRule
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.repository import scoped_get, scoped_select
from app_shared.scheduling.cadence import compute_next_run_at

from app.deps import Principal, require_scopes
from app.schemas.refresh_rules import (
    DeleteOutcome,
    RefreshRuleCreate,
    RefreshRuleListResponse,
    RefreshRuleResponse,
    RefreshRuleUpdate,
    RefreshRuleValidationError,
    validate_cadence,
    validate_scope_target,
)

router = APIRouter(prefix="/v1/refresh-rules", tags=["refresh-rules"])

# Which model + target-id field each non-WORKSPACE scope resolves against
# (contract "the supplied target id must resolve in-workspace via
# scoped_get"; research R6).
_SCOPE_TARGET_MODELS: dict[ScrapeScope, tuple[type, str]] = {
    ScrapeScope.COMPETITOR: (Competitor, "competitor_id"),
    ScrapeScope.PRODUCT: (Product, "product_id"),
    ScrapeScope.VARIANT: (ProductVariant, "product_variant_id"),
    ScrapeScope.PRODUCT_GROUP: (ProductGroup, "product_group_id"),
    ScrapeScope.MATCH: (CompetitorProductMatch, "match_id"),
}

# The five scope-target columns, in a fixed order (used to build the
# "merged view" for PATCH re-validation).
_TARGET_FIELDS = (
    "product_id",
    "product_variant_id",
    "product_group_id",
    "competitor_id",
    "match_id",
)

# The two cadence fields — recompute `next_run_at` on PATCH only when one
# of these actually changes (contract; FR-006/FR-016).
_CADENCE_FIELDS = ("cron_expression", "interval_minutes")


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


def _scope_target_mismatch(message: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "SCOPE_TARGET_MISMATCH", "message": message}},
    )


def _validation_error(exc: RefreshRuleValidationError) -> HTTPException:
    return HTTPException(
        status_code=422, detail={"error": {"code": exc.code, "message": str(exc)}}
    )


def _empty_update() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": {
                "code": "EMPTY_UPDATE",
                "message": "Provide at least one field to update.",
            }
        },
    )


def _verify_target_in_workspace(
    session: object,
    workspace_id: uuid.UUID,
    scope: ScrapeScope,
    target_id: uuid.UUID | None,
) -> None:
    """Verify ``target_id`` (for a non-WORKSPACE scope) resolves in-workspace.

    A missing/dangling/cross-workspace target id is indistinguishable from
    "not yours" (contract) — always reported as `SCOPE_TARGET_MISMATCH`,
    never a `404`, so existence is never leaked.
    """
    if scope == ScrapeScope.WORKSPACE:
        return
    model, field_name = _SCOPE_TARGET_MODELS[scope]
    assert target_id is not None  # enforced by validate_scope_target already
    if scoped_get(session, model, target_id, workspace_id) is None:  # type: ignore[arg-type]
        raise _scope_target_mismatch(
            f"{field_name} does not resolve to a row in this workspace."
        )


@router.post("", response_model=RefreshRuleResponse, status_code=201)
def create_refresh_rule(
    payload: RefreshRuleCreate,
    principal_ctx: tuple = Depends(require_scopes("refresh_rules:write")),
) -> RefreshRuleResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    target_field = _SCOPE_TARGET_MODELS.get(payload.scope)
    if target_field is not None:
        _verify_target_in_workspace(
            session, principal.workspace_id, payload.scope, getattr(payload, target_field[1])
        )

    now = datetime.now(timezone.utc)
    next_run_at = compute_next_run_at(payload, now)

    rule = RefreshRule(
        workspace_id=principal.workspace_id,
        name=payload.name,
        scope=payload.scope,
        product_id=payload.product_id,
        product_variant_id=payload.product_variant_id,
        product_group_id=payload.product_group_id,
        competitor_id=payload.competitor_id,
        match_id=payload.match_id,
        cron_expression=payload.cron_expression,
        interval_minutes=payload.interval_minutes,
        priority=payload.priority,
        enabled=payload.enabled,
        next_run_at=next_run_at,
    )
    session.add(rule)
    session.flush()

    return RefreshRuleResponse.model_validate(rule)


@router.get("", response_model=RefreshRuleListResponse)
def list_refresh_rules(
    limit: int | None = None,
    cursor: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("refresh_rules:read")),
) -> RefreshRuleListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(RefreshRule, principal.workspace_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(RefreshRule, after))
    stmt = stmt.order_by(RefreshRule.created_at, RefreshRule.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [RefreshRuleResponse.model_validate(r) for r in envelope["items"]]
    return RefreshRuleListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/{rule_id}", response_model=RefreshRuleResponse)
def get_refresh_rule(
    rule_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("refresh_rules:read")),
) -> RefreshRuleResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    rule = scoped_get(session, RefreshRule, rule_id, principal.workspace_id)
    if rule is None:
        raise _not_found("Refresh rule not found.")

    return RefreshRuleResponse.model_validate(rule)


@router.patch("/{rule_id}", response_model=RefreshRuleResponse)
def update_refresh_rule(
    rule_id: uuid.UUID,
    payload: RefreshRuleUpdate,
    principal_ctx: tuple = Depends(require_scopes("refresh_rules:write")),
) -> RefreshRuleResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    rule = scoped_get(session, RefreshRule, rule_id, principal.workspace_id)
    if rule is None:
        raise _not_found("Refresh rule not found.")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise _empty_update()

    # Merged view (existing row + this patch) for re-validation — patching
    # only `name`, say, must not require re-supplying cadence/scope.
    merged_scope = ScrapeScope(updates.get("scope", rule.scope))
    merged_cron = updates.get("cron_expression", rule.cron_expression)
    merged_interval = updates.get("interval_minutes", rule.interval_minutes)
    merged_targets = {
        field_name: updates.get(field_name, getattr(rule, field_name))
        for field_name in _TARGET_FIELDS
    }

    try:
        validate_cadence(merged_cron, merged_interval)
        validate_scope_target(merged_scope, **merged_targets)
    except RefreshRuleValidationError as exc:
        raise _validation_error(exc) from exc

    target_field = _SCOPE_TARGET_MODELS.get(merged_scope)
    if target_field is not None:
        changed_target_fields = set(updates) & set(_TARGET_FIELDS) | (
            {"scope"} if "scope" in updates else set()
        )
        if changed_target_fields:
            _verify_target_in_workspace(
                session, principal.workspace_id, merged_scope, merged_targets[target_field[1]]
            )

    cadence_changed = any(field_name in updates for field_name in _CADENCE_FIELDS)

    for field_name, value in updates.items():
        setattr(rule, field_name, value)

    if cadence_changed:
        rule.next_run_at = compute_next_run_at(rule, datetime.now(timezone.utc))

    session.flush()

    return RefreshRuleResponse.model_validate(rule)


@router.delete("/{rule_id}", response_model=DeleteOutcome)
def delete_refresh_rule(
    rule_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("refresh_rules:write")),
) -> DeleteOutcome:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    rule = scoped_get(session, RefreshRule, rule_id, principal.workspace_id)
    if rule is None:
        raise _not_found("Refresh rule not found.")

    session.delete(rule)
    session.flush()

    return DeleteOutcome(id=rule_id, outcome="hard_deleted")
