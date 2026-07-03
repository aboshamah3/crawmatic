"""Matches endpoints (`contracts/api-matches.md`) — SPEC-05 US2 (single-record CRUD).

Every endpoint runs on the SPEC-03 auth seam (`app.deps.get_current_principal`
=> `set_workspace_context` already applied to the yielded session) and is
scope-gated via `app.deps.require_scopes(...)`. All reads/writes go
through `app_shared.repository.scoped_select`/`scoped_get` — RLS backs
them as the second isolation layer (FR-002/FR-015), same discipline as
`routers/competitors.py`/`routers/variants.py`.

Every `competitor_url` is safety-validated (`app_shared.url_safety`) and
normalized+patterned (`app_shared.url_pattern`) before it is stored — on
create and, when it changes, on update (FR-007/008/009/010/011). Match
references (`competitor_id`, the variant) must resolve within the
caller's workspace — checked via the SPEC-04 workspace-consistency
pre-check (`app_shared.catalog.consistency`) before the composite FKs
would otherwise surface a raw `IntegrityError` (FR-006). `product_id` is
never client-supplied; it is derived from the resolved variant's parent
(research D4).

Bulk-upsert (`POST /v1/matches/bulk-upsert`, US3) lands in a later phase
of this feature and is intentionally absent here.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_shared.catalog.consistency import (
    CrossWorkspaceReference,
    MissingReference,
    assert_refs_in_workspace,
)
from app_shared.models.catalog import ProductVariant
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.repository import scoped_get, scoped_select
from app_shared.url_pattern import derive_match_url_fields
from app_shared.url_safety import UnsafeUrlError, validate_competitor_url

from app.deps import Principal, require_scopes
from app.schemas.matches import (
    DeleteOutcome,
    MatchCreate,
    MatchListResponse,
    MatchResponse,
    MatchUpdate,
)

router = APIRouter(prefix="/v1/matches", tags=["matches"])


# --- error builders ----------------------------------------------------------


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


def _workspace_mismatch(message: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "WORKSPACE_MISMATCH", "message": message}},
    )


def _unsafe_url(exc: UnsafeUrlError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "UNSAFE_URL", "message": str(exc), "reason": exc.reason.value}},
    )


def _duplicate_match(message: str) -> HTTPException:
    return HTTPException(
        status_code=409, detail={"error": {"code": "DUPLICATE_MATCH", "message": message}}
    )


# --- reference resolution (Layer 2, contracts/workspace-consistency.md) -----


def _workspace_map(session: Session, model: type, ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """One narrow, unscoped ``id IN (...)`` lookup -> ``{id: workspace_id}``.

    Deliberately unscoped (mirrors `routers/variants.py`
    ``_resolve_parent_product_ids``) so a cross-workspace reference can be
    told apart from a nonexistent one before `assert_refs_in_workspace`
    turns either into a clean `422`/`404` — never a raw `IntegrityError`.
    """
    if not ids:
        return {}
    rows = session.execute(
        select(model.id, model.workspace_id).where(model.id.in_(ids))  # noqa: workspace-scope
    ).all()
    return {row.id: row.workspace_id for row in rows}


def _resolve_competitor(session: Session, workspace_id: uuid.UUID, competitor_id: uuid.UUID) -> None:
    resolved = _workspace_map(session, Competitor, [competitor_id])
    try:
        assert_refs_in_workspace(workspace_id, [competitor_id], resolved)
    except MissingReference as exc:
        raise _not_found("Competitor not found.") from exc
    except CrossWorkspaceReference as exc:
        raise _workspace_mismatch(
            "Competitor belongs to a different workspace."
        ) from exc


def _resolve_variant(
    session: Session, workspace_id: uuid.UUID, payload: MatchCreate
) -> ProductVariant:
    """Resolve the match's variant in-workspace, yielding `product_variant_id` + `product_id`.

    Exactly one of `product_variant_id`/`variant_external_id`/`variant_sku`
    is set (enforced by the schema). An explicit id is checked via the
    unscoped-lookup + consistency pre-check (distinguishes cross-workspace
    from nonexistent, FR-006); an `external_id`/`sku` lookup is inherently
    workspace-scoped, so a miss there is always `404`.
    """
    if payload.product_variant_id is not None:
        resolved = _workspace_map(session, ProductVariant, [payload.product_variant_id])
        try:
            assert_refs_in_workspace(workspace_id, [payload.product_variant_id], resolved)
        except MissingReference as exc:
            raise _not_found("Variant not found.") from exc
        except CrossWorkspaceReference as exc:
            raise _workspace_mismatch(
                "Variant belongs to a different workspace."
            ) from exc
        variant = scoped_get(session, ProductVariant, payload.product_variant_id, workspace_id)
        assert variant is not None
        return variant

    if payload.variant_external_id:
        stmt = scoped_select(ProductVariant, workspace_id).where(
            ProductVariant.external_id == payload.variant_external_id
        )
        variant = session.execute(stmt).scalars().first()
        if variant is None:
            raise _not_found("Variant not found (variant_external_id).")
        return variant

    stmt = scoped_select(ProductVariant, workspace_id).where(
        ProductVariant.sku == payload.variant_sku
    )
    variant = session.execute(stmt).scalars().first()
    if variant is None:
        raise _not_found("Variant not found (variant_sku).")
    return variant


# --- endpoints -----------------------------------------------------------


@router.post("", response_model=MatchResponse, status_code=201)
def create_match(
    payload: MatchCreate,
    principal_ctx: tuple = Depends(require_scopes("matches:write")),
) -> MatchResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    try:
        validate_competitor_url(payload.competitor_url)
    except UnsafeUrlError as exc:
        raise _unsafe_url(exc) from exc
    normalized_url, url_pattern, url_pattern_version = derive_match_url_fields(
        payload.competitor_url
    )

    variant = _resolve_variant(session, ws, payload)
    _resolve_competitor(session, ws, payload.competitor_id)

    match = CompetitorProductMatch(
        workspace_id=ws,
        product_id=variant.product_id,
        product_variant_id=variant.id,
        competitor_id=payload.competitor_id,
        competitor_url=payload.competitor_url,
        normalized_competitor_url=normalized_url,
        url_pattern=url_pattern,
        url_pattern_version=url_pattern_version,
        competitor_variant_identifier=payload.competitor_variant_identifier,
        competitor_variant_sku=payload.competitor_variant_sku,
        competitor_variant_options=payload.competitor_variant_options,
        external_title=payload.external_title,
        scrape_profile_id=payload.scrape_profile_id,
        access_policy_id=payload.access_policy_id,
        priority=payload.priority,
        status=payload.status,
    )
    session.add(match)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_match(
            "A match already exists for this (variant, competitor, normalized "
            "URL) tuple in this workspace "
            "(unique(workspace_id, product_variant_id, competitor_id, "
            "normalized_competitor_url))."
        ) from exc

    return MatchResponse.model_validate(match)


@router.get("", response_model=MatchListResponse)
def list_matches(
    limit: int | None = None,
    cursor: str | None = None,
    product_variant_id: uuid.UUID | None = None,
    competitor_id: uuid.UUID | None = None,
    principal_ctx: tuple = Depends(require_scopes("matches:read")),
) -> MatchListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = scoped_select(CompetitorProductMatch, principal.workspace_id)
    if product_variant_id is not None:
        stmt = stmt.where(CompetitorProductMatch.product_variant_id == product_variant_id)
    if competitor_id is not None:
        stmt = stmt.where(CompetitorProductMatch.competitor_id == competitor_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(CompetitorProductMatch, after))
    stmt = stmt.order_by(
        CompetitorProductMatch.created_at, CompetitorProductMatch.id
    ).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [MatchResponse.model_validate(m) for m in envelope["items"]]
    return MatchListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/{match_id}", response_model=MatchResponse)
def get_match(
    match_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("matches:read")),
) -> MatchResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    match = scoped_get(session, CompetitorProductMatch, match_id, principal.workspace_id)
    if match is None:
        raise _not_found("Match not found.")

    return MatchResponse.model_validate(match)


@router.patch("/{match_id}", response_model=MatchResponse)
def update_match(
    match_id: uuid.UUID,
    payload: MatchUpdate,
    principal_ctx: tuple = Depends(require_scopes("matches:write")),
) -> MatchResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    match = scoped_get(session, CompetitorProductMatch, match_id, principal.workspace_id)
    if match is None:
        raise _not_found("Match not found.")

    updates = payload.model_dump(exclude_unset=True)

    if "competitor_url" in updates:
        new_url = updates["competitor_url"]
        try:
            validate_competitor_url(new_url)
        except UnsafeUrlError as exc:
            raise _unsafe_url(exc) from exc
        normalized_url, url_pattern, url_pattern_version = derive_match_url_fields(new_url)
        match.competitor_url = new_url
        match.normalized_competitor_url = normalized_url
        match.url_pattern = url_pattern
        match.url_pattern_version = url_pattern_version
        del updates["competitor_url"]

    for field, value in updates.items():
        setattr(match, field, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_match(
            "A match already exists for this (variant, competitor, normalized "
            "URL) tuple in this workspace "
            "(unique(workspace_id, product_variant_id, competitor_id, "
            "normalized_competitor_url))."
        ) from exc

    return MatchResponse.model_validate(match)


@router.delete("/{match_id}", response_model=DeleteOutcome)
def delete_match(
    match_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("matches:write")),
) -> DeleteOutcome:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    match = scoped_get(session, CompetitorProductMatch, match_id, principal.workspace_id)
    if match is None:
        raise _not_found("Match not found.")

    # No dependent history exists in this spec (observations/attempts are
    # SPEC-07+) -> hard delete (FR-016). Structured so a future
    # archive-by-status path only needs to swap the branch below for
    # `match.status = MatchStatus.ARCHIVED`.
    session.delete(match)
    session.flush()

    return DeleteOutcome(id=match_id, outcome="hard_deleted")
