"""Access-policy endpoints (`contracts/api-access.md`) — SPEC-10 US1.

Every endpoint runs on the SPEC-03 auth seam (`app.deps.get_current_principal`
=> `set_workspace_context` already applied to the yielded session) and is
scope-gated via `app.deps.require_scopes(...)`. `access_policies` is
**dual-scope** (the SPEC-06 `scrape_profiles` pattern, research D2):
reads go through `app_shared.access.repository.visible_policies_select`
(own OR global); create/update/delete go through
`owned_policy_select`/`owned_policy_get` (own-only — a global or
other-workspace id 404s via the tenant path, FR-006).

`provider_id`, when set, is checked via
`app_shared.access.repository.assert_provider_assignable` — own or
global -> OK, cross-workspace -> `422 WORKSPACE_MISMATCH`, dangling ->
`404 NOT_FOUND`, `None` -> OK (clears the reference).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from app_shared.access.repository import (
    assert_provider_assignable,
    owned_policy_get,
    visible_policies_select,
)
from app_shared.catalog.consistency import CrossWorkspaceReference, MissingReference
from app_shared.models.access import AccessPolicy
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate

from app.deps import Principal, require_scopes
from app.schemas.access import (
    AccessPolicyCreate,
    AccessPolicyListResponse,
    AccessPolicyResponse,
    AccessPolicyUpdate,
    DeleteOutcome,
)

router = APIRouter(prefix="/v1/access-policies", tags=["access-policies"])


# --- error builders ------------------------------------------------------


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


def _duplicate_policy(message: str) -> HTTPException:
    return HTTPException(
        status_code=409, detail={"error": {"code": "DUPLICATE_POLICY", "message": message}}
    )


def _workspace_mismatch(message: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "WORKSPACE_MISMATCH", "message": message}},
    )


def _check_provider_assignable(
    session: object, workspace_id: uuid.UUID, provider_id: uuid.UUID | None
) -> None:
    """`assert_provider_assignable` wrapper mapping to this router's own
    `404`/`422` error builders (`contracts/access-repository.md`)."""
    try:
        assert_provider_assignable(session, workspace_id, provider_id)  # type: ignore[arg-type]
    except MissingReference as exc:
        raise _not_found("Proxy provider not found.") from exc
    except CrossWorkspaceReference as exc:
        raise _workspace_mismatch(
            "Proxy provider belongs to a different workspace."
        ) from exc


# --- endpoints -------------------------------------------------------------


@router.post("", response_model=AccessPolicyResponse, status_code=201)
def create_access_policy(
    payload: AccessPolicyCreate,
    principal_ctx: tuple = Depends(require_scopes("access_policies:write")),
) -> AccessPolicyResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    _check_provider_assignable(session, principal.workspace_id, payload.provider_id)

    policy = AccessPolicy(
        workspace_id=principal.workspace_id,
        name=payload.name,
        strategy=payload.strategy,
        provider_id=payload.provider_id,
        country_code=payload.country_code,
        use_proxy_on_first_attempt=payload.use_proxy_on_first_attempt,
        use_proxy_on_retry=payload.use_proxy_on_retry,
        allow_browser_fallback=payload.allow_browser_fallback,
        max_retries=payload.max_retries,
        rotate_per_request=payload.rotate_per_request,
        sticky_session=payload.sticky_session,
        session_ttl_minutes=payload.session_ttl_minutes,
        max_requests_per_minute=payload.max_requests_per_minute,
        max_requests_per_hour=payload.max_requests_per_hour,
        max_requests_per_day=payload.max_requests_per_day,
        timeout_ms=payload.timeout_ms,
    )
    session.add(policy)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_policy(
            "An access policy with this name already exists in this workspace "
            "(unique(workspace_id, name))."
        ) from exc

    return AccessPolicyResponse.model_validate(policy)


@router.get("", response_model=AccessPolicyListResponse)
def list_access_policies(
    limit: int | None = None,
    cursor: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("access_policies:read")),
) -> AccessPolicyListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = visible_policies_select(principal.workspace_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(AccessPolicy, after))
    stmt = stmt.order_by(AccessPolicy.created_at, AccessPolicy.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [AccessPolicyResponse.model_validate(p) for p in envelope["items"]]
    return AccessPolicyListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/{policy_id}", response_model=AccessPolicyResponse)
def get_access_policy(
    policy_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("access_policies:read")),
) -> AccessPolicyResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    stmt = visible_policies_select(principal.workspace_id).where(AccessPolicy.id == policy_id)
    policy = session.execute(stmt).scalar_one_or_none()
    if policy is None:
        raise _not_found("Access policy not found.")

    return AccessPolicyResponse.model_validate(policy)


@router.patch("/{policy_id}", response_model=AccessPolicyResponse)
def update_access_policy(
    policy_id: uuid.UUID,
    payload: AccessPolicyUpdate,
    principal_ctx: tuple = Depends(require_scopes("access_policies:write")),
) -> AccessPolicyResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    # Own-only (FR-006): a global or other-workspace id 404s via the
    # tenant write path, never editable through this endpoint.
    policy = owned_policy_get(session, policy_id, principal.workspace_id)
    if policy is None:
        raise _not_found("Access policy not found.")

    updates = payload.model_dump(exclude_unset=True)
    if "provider_id" in updates:
        _check_provider_assignable(session, principal.workspace_id, updates["provider_id"])

    for field, value in updates.items():
        setattr(policy, field, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_policy(
            "An access policy with this name already exists in this workspace "
            "(unique(workspace_id, name))."
        ) from exc

    return AccessPolicyResponse.model_validate(policy)


@router.delete("/{policy_id}", response_model=DeleteOutcome)
def delete_access_policy(
    policy_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("access_policies:write")),
) -> DeleteOutcome:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    # Own-only (FR-006): a global or other-workspace id 404s via the
    # tenant write path — the tenant can never delete a global policy.
    policy = owned_policy_get(session, policy_id, principal.workspace_id)
    if policy is None:
        raise _not_found("Access policy not found.")

    session.delete(policy)
    session.flush()

    return DeleteOutcome(id=policy_id, outcome="hard_deleted")
