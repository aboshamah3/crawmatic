"""API-key management endpoints (`contracts/api-keys.md`) — US2.

``POST/GET /v1/api-keys`` + ``DELETE /v1/api-keys/{id}``. Human-administered
(WORKSPACE_ADMIN/SUPER_ADMIN, authenticated via access JWT — enforced by
``require_role``) and operate entirely within the caller's already-resolved
workspace context (the ``deps.get_current_principal`` seam has already
called ``set_workspace_context`` on the yielded session before these
handlers run).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import update

from app_shared.enums import ApiKeyStatus, UserRole
from app_shared.models import ApiKey
from app_shared.repository import scoped_get, scoped_select
from app_shared.security.api_keys import generate_api_key
from app_shared.security.scopes import validate_scopes

from app.deps import Principal, require_role

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])

_MANAGER_ROLES = (UserRole.WORKSPACE_ADMIN, UserRole.SUPER_ADMIN)


class CreateApiKeyRequest(BaseModel):
    name: str
    scopes: list[str]


class CreateApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    scopes: list[str]
    status: str
    created_at: datetime
    api_key: str  # the full secret — shown exactly once


class ApiKeyListItem(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    scopes: list[str]
    status: str
    last_used_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None


class ApiKeyListResponse(BaseModel):
    items: list[ApiKeyListItem]


@router.post("", response_model=CreateApiKeyResponse, status_code=201)
def create_api_key(
    payload: CreateApiKeyRequest,
    principal_ctx: tuple = Depends(require_role(*_MANAGER_ROLES)),
) -> CreateApiKeyResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    # An unknown scope -> 422 (FR-013).
    try:
        scopes = validate_scopes(payload.scopes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": {"code": "INVALID_SCOPES", "message": str(exc)}}) from exc

    full_secret, key_prefix, key_hash = generate_api_key()

    api_key = ApiKey(
        workspace_id=principal.workspace_id,
        name=payload.name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        scopes=scopes,
        status=ApiKeyStatus.ACTIVE,
    )
    session.add(api_key)
    session.flush()

    return CreateApiKeyResponse(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        scopes=list(api_key.scopes),
        status=str(api_key.status),
        created_at=api_key.created_at,
        api_key=full_secret,  # returned exactly once; never persisted/re-shown
    )


@router.get("", response_model=ApiKeyListResponse)
def list_api_keys(
    principal_ctx: tuple = Depends(require_role(*_MANAGER_ROLES)),
) -> ApiKeyListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    rows = session.execute(scoped_select(ApiKey, principal.workspace_id)).scalars().all()
    items = [
        ApiKeyListItem(
            id=row.id,
            name=row.name,
            key_prefix=row.key_prefix,
            scopes=list(row.scopes),
            status=str(row.status),
            last_used_at=row.last_used_at,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
        )
        for row in rows
    ]
    return ApiKeyListResponse(items=items)


@router.delete("/{api_key_id}", status_code=204)
def revoke_api_key(
    api_key_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_role(*_MANAGER_ROLES)),
) -> None:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    # Idempotent, workspace-scoped update — a caller can never revoke
    # another workspace's key (scoped_get filters by BOTH id and
    # workspace_id; RLS backs this up). Missing/already-revoked -> still
    # 204 (SC-004: after revocation the key authenticates 0 requests;
    # revoking twice is a no-op, not an error).
    existing = scoped_get(session, ApiKey, api_key_id, principal.workspace_id)
    if existing is None:
        return None

    session.execute(
        update(ApiKey)
        .where(ApiKey.id == api_key_id, ApiKey.workspace_id == principal.workspace_id)
        .values(status=ApiKeyStatus.REVOKED, revoked_at=datetime.now(timezone.utc))
    )
    return None
