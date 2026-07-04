"""Proxy-provider endpoints (`contracts/api-access.md`) — SPEC-10 US1.

Every endpoint runs on the SPEC-03 auth seam (`app.deps.get_current_principal`
=> `set_workspace_context` already applied to the yielded session) and is
scope-gated via `app.deps.require_scopes(...)`. `proxy_providers` is
**dual-scope** (the SPEC-06 `scrape_profiles` pattern, research D2):
reads go through `app_shared.access.repository.visible_providers_select`
(own OR global); create/update/delete go through
`owned_provider_select`/`owned_provider_get` (own-only — a global or
other-workspace id 404s via the tenant path, FR-006).

`base_url` is validated at save time by the existing SSRF guard
(`app_shared.url_safety.validate_competitor_url`, D4) — no new
validator. A plaintext `password` is never persisted as-is: it is
encrypted via `app_shared.security.encryption.encrypt_secret` into
`(password_encrypted, password_key_version)` and the response exposes
only a derived `has_password` boolean — never the plaintext or the
ciphertext (SC-003).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from app_shared.access.repository import owned_provider_get, visible_providers_select
from app_shared.models.access import ProxyProvider
from app_shared.pagination import InvalidCursor, clamp_limit, decode_cursor, keyset_predicate, paginate
from app_shared.security.encryption import encrypt_secret
from app_shared.url_safety import UnsafeUrlError, validate_competitor_url

from app.deps import Principal, require_scopes
from app.schemas.access import (
    DeleteOutcome,
    ProxyProviderCreate,
    ProxyProviderListResponse,
    ProxyProviderResponse,
    ProxyProviderUpdate,
)

router = APIRouter(prefix="/v1/proxy-providers", tags=["proxy-providers"])


# --- error builders ------------------------------------------------------


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


def _duplicate_provider(message: str) -> HTTPException:
    return HTTPException(
        status_code=409, detail={"error": {"code": "DUPLICATE_PROVIDER", "message": message}}
    )


def _unsafe_url(exc: UnsafeUrlError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "UNSAFE_URL", "message": str(exc), "reason": exc.reason.value}},
    )


# --- response builder (never echoes the password, SC-003) ------------------


def _to_response(provider: ProxyProvider) -> ProxyProviderResponse:
    return ProxyProviderResponse(
        id=provider.id,
        workspace_id=provider.workspace_id,
        name=provider.name,
        type=provider.type,
        base_url=provider.base_url,
        username=provider.username,
        has_password=provider.password_encrypted is not None,
        country_code=provider.country_code,
        status=provider.status,
        monthly_budget_limit=provider.monthly_budget_limit,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


# --- endpoints -------------------------------------------------------------


@router.post("", response_model=ProxyProviderResponse, status_code=201)
def create_proxy_provider(
    payload: ProxyProviderCreate,
    principal_ctx: tuple = Depends(require_scopes("proxy_providers:write")),
) -> ProxyProviderResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    try:
        validate_competitor_url(payload.base_url)
    except UnsafeUrlError as exc:
        raise _unsafe_url(exc) from exc

    password_encrypted: str | None = None
    password_key_version: int | None = None
    if payload.password is not None:
        secret = encrypt_secret(payload.password)
        password_encrypted = secret.ciphertext
        password_key_version = secret.key_version

    provider = ProxyProvider(
        workspace_id=principal.workspace_id,
        name=payload.name,
        type=payload.type,
        base_url=payload.base_url,
        username=payload.username,
        password_encrypted=password_encrypted,
        password_key_version=password_key_version,
        country_code=payload.country_code,
        status=payload.status,
        monthly_budget_limit=payload.monthly_budget_limit,
    )
    session.add(provider)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_provider(
            "A proxy provider with this name already exists in this workspace "
            "(unique(workspace_id, name))."
        ) from exc

    return _to_response(provider)


@router.get("", response_model=ProxyProviderListResponse)
def list_proxy_providers(
    limit: int | None = None,
    cursor: str | None = None,
    principal_ctx: tuple = Depends(require_scopes("proxy_providers:read")),
) -> ProxyProviderListResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    page_limit = clamp_limit(limit)
    stmt = visible_providers_select(principal.workspace_id)
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc
        stmt = stmt.where(keyset_predicate(ProxyProvider, after))
    stmt = stmt.order_by(ProxyProvider.created_at, ProxyProvider.id).limit(page_limit + 1)

    rows = session.execute(stmt).scalars().all()
    envelope = paginate(rows, page_limit)
    items = [_to_response(p) for p in envelope["items"]]
    return ProxyProviderListResponse(items=items, next_cursor=envelope["next_cursor"])


@router.get("/{provider_id}", response_model=ProxyProviderResponse)
def get_proxy_provider(
    provider_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("proxy_providers:read")),
) -> ProxyProviderResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    stmt = visible_providers_select(principal.workspace_id).where(ProxyProvider.id == provider_id)
    provider = session.execute(stmt).scalar_one_or_none()
    if provider is None:
        raise _not_found("Proxy provider not found.")

    return _to_response(provider)


@router.patch("/{provider_id}", response_model=ProxyProviderResponse)
def update_proxy_provider(
    provider_id: uuid.UUID,
    payload: ProxyProviderUpdate,
    principal_ctx: tuple = Depends(require_scopes("proxy_providers:write")),
) -> ProxyProviderResponse:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    # Own-only (FR-006): a global or other-workspace id 404s via the
    # tenant write path, never editable through this endpoint.
    provider = owned_provider_get(session, provider_id, principal.workspace_id)
    if provider is None:
        raise _not_found("Proxy provider not found.")

    updates = payload.model_dump(exclude_unset=True)

    if "base_url" in updates:
        try:
            validate_competitor_url(updates["base_url"])
        except UnsafeUrlError as exc:
            raise _unsafe_url(exc) from exc

    # `password` needs bespoke handling: omitted -> unchanged (not in
    # `updates`); `None` -> clear both ciphertext columns; a non-null
    # string -> re-encrypt under the primary keyring version.
    if "password" in updates:
        plaintext = updates.pop("password")
        if plaintext is None:
            provider.password_encrypted = None
            provider.password_key_version = None
        else:
            secret = encrypt_secret(plaintext)
            provider.password_encrypted = secret.ciphertext
            provider.password_key_version = secret.key_version

    for field, value in updates.items():
        setattr(provider, field, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _duplicate_provider(
            "A proxy provider with this name already exists in this workspace "
            "(unique(workspace_id, name))."
        ) from exc

    return _to_response(provider)


@router.delete("/{provider_id}", response_model=DeleteOutcome)
def delete_proxy_provider(
    provider_id: uuid.UUID,
    principal_ctx: tuple = Depends(require_scopes("proxy_providers:write")),
) -> DeleteOutcome:
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    # Own-only (FR-006): a global or other-workspace id 404s via the
    # tenant write path — the tenant can never delete a global provider.
    provider = owned_provider_get(session, provider_id, principal.workspace_id)
    if provider is None:
        raise _not_found("Proxy provider not found.")

    session.delete(provider)
    session.flush()

    return DeleteOutcome(id=provider_id, outcome="hard_deleted")
