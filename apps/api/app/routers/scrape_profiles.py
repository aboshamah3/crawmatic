"""Scrape-profiles endpoints (`contracts/api-scrape-profiles.md`) — SPEC-06 US1.

Every endpoint runs on the SPEC-03 auth seam (`app.deps.get_current_principal`
=> `set_workspace_context` already applied to the yielded session) and is
scope-gated via `app.deps.require_scopes(...)`. `scrape_profiles` is
**dual-scope** (SPEC-06's first, research D2): reads go through
`app_shared.profiles.repository.visible_profiles_select` (own OR global);
create/update/delete go through `owned_profile_select`/`owned_profile_get`
(own-only — a global or other-workspace id 404s via the tenant path,
FR-021). RLS backs both as the second isolation layer.

`PUT /v1/scrape-profiles/workspace-default` (assignment, US2) lands in a
later phase of this feature (T034) — not implemented here.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.profiles.repository import owned_profile_get, visible_profiles_select
from app_shared.profiles.upsert import build_profiles_upsert, prepare_profiles
from app_shared.profiles.validation import ProfileValidationError, validate_profile

from app.deps import Principal, require_scopes
from app.schemas.scrape_profiles import (
    DeleteOutcome,
    ScrapeProfileBulkUpsertRequest,
    ScrapeProfileBulkUpsertResult,
    ScrapeProfileCreate,
    ScrapeProfileListResponse,
    ScrapeProfileResponse,
    ScrapeProfileUpdate,
)

router = APIRouter(prefix="/v1/scrape-profiles", tags=["scrape-profiles"])


# --- error builders ------------------------------------------------------


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


def _duplicate_profile(message: str) -> HTTPException:
    return HTTPException(
        status_code=409, detail={"error": {"code": "DUPLICATE_PROFILE", "message": message}}
    )


def _validation_error(exc: ProfileValidationError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": {
                "code": "VALIDATION_ERROR",
                "field": exc.field,
                "message": exc.message,
            }
        },
    )


# --- endpoints -------------------------------------------------------------


@router.post("", response_model=ScrapeProfileResponse, status_code=201)
def create_scrape_profile(
    payload: ScrapeProfileCreate,
    principal_ctx: tuple = Depends(require_scopes("scrape_profiles:write")),
) -> ScrapeProfileResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    row = payload.model_dump()
    try:
        validate_profile(row)
    except ProfileValidationError as exc:
        raise _validation_error(exc) from exc

    profile = ScrapeProfile(workspace_id=principal.workspace_id, **row)
    session.add(profile)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_profile(
            "A scrape profile with this name already exists in this workspace "
            "(unique(workspace_id, name))."
        ) from exc

    return ScrapeProfileResponse.model_validate(profile)


@router.get("", response_model=ScrapeProfileListResponse)
def list_scrape_profiles(
    limit: int | None = None,
    cursor: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("scrape_profiles:read")),
) -> ScrapeProfileListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = visible_profiles_select(principal.workspace_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(ScrapeProfile, after))
    stmt = stmt.order_by(ScrapeProfile.created_at, ScrapeProfile.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [ScrapeProfileResponse.model_validate(p) for p in envelope["items"]]
    return ScrapeProfileListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/{profile_id}", response_model=ScrapeProfileResponse)
def get_scrape_profile(
    profile_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("scrape_profiles:read")),
) -> ScrapeProfileResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    stmt = visible_profiles_select(principal.workspace_id).where(ScrapeProfile.id == profile_id)
    profile = session.execute(stmt).scalar_one_or_none()
    if profile is None:
        raise _not_found("Scrape profile not found.")

    return ScrapeProfileResponse.model_validate(profile)


@router.patch("/{profile_id}", response_model=ScrapeProfileResponse)
def update_scrape_profile(
    profile_id: uuid.UUID,
    payload: ScrapeProfileUpdate,
    principal_ctx: tuple = Depends(require_scopes("scrape_profiles:write")),
) -> ScrapeProfileResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    # Own-only (FR-021): a global or other-workspace id 404s via the
    # tenant write path, never editable through this endpoint.
    profile = owned_profile_get(session, profile_id, principal.workspace_id)
    if profile is None:
        raise _not_found("Scrape profile not found.")

    updates = payload.model_dump(exclude_unset=True)

    # Re-validate the merged (existing + changed) shape so a partial
    # update can never leave the row in an invalid state.
    merged = {
        col.name: getattr(profile, col.name) for col in ScrapeProfile.__table__.columns
    }
    merged.update(updates)
    try:
        validate_profile(merged)
    except ProfileValidationError as exc:
        raise _validation_error(exc) from exc

    for field, value in updates.items():
        setattr(profile, field, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_profile(
            "A scrape profile with this name already exists in this workspace "
            "(unique(workspace_id, name))."
        ) from exc

    return ScrapeProfileResponse.model_validate(profile)


@router.delete("/{profile_id}", response_model=DeleteOutcome)
def delete_scrape_profile(
    profile_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("scrape_profiles:write")),
) -> DeleteOutcome:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    # Own-only (FR-021): a global or other-workspace id 404s via the
    # tenant write path — the tenant can never delete a global profile.
    profile = owned_profile_get(session, profile_id, principal.workspace_id)
    if profile is None:
        raise _not_found("Scrape profile not found.")

    # Hard delete; the three assignment FKs are ON DELETE SET NULL so no
    # reference is ever left dangling (FR-023).
    session.delete(profile)
    session.flush()

    return DeleteOutcome(id=profile_id, outcome="hard_deleted")


# --- bulk-upsert (`contracts/profiles-bulk-upsert.md`, FR-020, SC-008) -----


@router.post("/bulk-upsert", response_model=ScrapeProfileBulkUpsertResult, status_code=200)
def bulk_upsert_scrape_profiles(
    payload: ScrapeProfileBulkUpsertRequest,
    principal_ctx: tuple = Depends(require_scopes("scrape_profiles:write")),
) -> ScrapeProfileBulkUpsertResult:
    """Set-based bulk upsert (FR-020, SC-008): `prepare_profiles` (validate +
    reject-and-report + last-wins dedup) -> `build_profiles_upsert`
    executed once under the caller's workspace context. Tenant-only —
    never writes a global row."""
    session, principal = principal_ctx
    assert isinstance(principal, Principal)
    ws = principal.workspace_id

    if not payload.profiles:
        return ScrapeProfileBulkUpsertResult(upserted=0, profiles=[], rejected=[])

    row_dicts = [item.model_dump() for item in payload.profiles]
    valid, rejected = prepare_profiles(row_dicts, workspace_id=ws)

    if not valid:
        return ScrapeProfileBulkUpsertResult(upserted=0, profiles=[], rejected=rejected)

    stmt = build_profiles_upsert(valid).returning(ScrapeProfile.id)
    profile_ids = [row.id for row in session.execute(stmt).all()]
    session.flush()

    profiles = (
        session.execute(
            visible_profiles_select(ws).where(ScrapeProfile.id.in_(profile_ids))
        )
        .scalars()
        .all()
    )
    return ScrapeProfileBulkUpsertResult(
        upserted=len(profiles),
        profiles=[ScrapeProfileResponse.model_validate(p) for p in profiles],
        rejected=rejected,
    )
