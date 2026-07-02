"""The authentication + authorization dependency (`contracts/workspace-context.md`, research D5).

This module is the **single auth seam** for every protected `/v1`
endpoint. ``get_current_principal`` resolves exactly one authenticated
principal and exactly one authorized workspace context per request, in
order:

1. Extract the bearer credential. A ``ck_``-prefixed value routes to the
   API-key path; otherwise it is decoded as a JWT.
2. **API-key path**: ``parse_prefix`` → look up candidate keys by prefix
   via the BYPASSRLS ``get_auth_session()`` path (prefix is a short,
   non-unique lookup slice — several rows may share it) → verify each
   candidate's hash with ``hmac.compare_digest`` (FR-016, prefix-collision
   safe) → require ``status == ACTIVE`` → fire the best-effort
   ``last_used_at`` throttle.
   **JWT path**: ``decode_access_token`` (verify signature + ``exp``).
3. Cached status check (``status_cache``, fail-safe deny on a Redis
   error → 401) — no per-request status DB read in steady state
   (FR-022).
4. Resolve + authorize the workspace context: a principal with its own
   (non-null) ``workspace_id`` may only ever act in that workspace (an
   explicit ``X-Workspace-Id`` that disagrees is a 403 — assuming a
   workspace is a role-authorized act, never a bypass); a SUPER_ADMIN
   (JWT ``workspace_id`` null) MUST supply an explicit, role-authorized
   ``X-Workspace-Id``.
5. Open the request transaction and call ``set_workspace_context`` before
   any workspace-owned query runs.

The pre-auth credential lookups (api-key-by-prefix, user-status-by-id)
run through ``get_auth_session()`` (BYPASSRLS) — the same narrow,
fixed-filter boundary as the login/refresh lookups in
``apps/api/app/routers/auth.py`` (research D4), annotated
``# noqa: workspace-scope``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app_shared.config import get_settings
from app_shared.database import get_auth_session, get_session, set_workspace_context
from app_shared.enums import ApiKeyStatus, UserRole
from app_shared.models import ApiKey
from app_shared.redis_client import get_redis_client
from app_shared.security.api_keys import API_KEY_PREFIX, parse_prefix, verify_api_key
from app_shared.security.jwt import decode_access_token
from app_shared.security.last_used import should_write_last_used
from app_shared.security.scopes import has_scopes
from app_shared.security.status_cache import get_user_status, get_workspace_status

from app.errors import auth_failed_exception

ACTIVE_STATUS = "active"


def _forbidden(message: str) -> HTTPException:
    """A ``403`` for an authorization (role/scope/workspace-assumption) failure.

    Distinct from :func:`app.errors.auth_failed_exception` (``401``,
    "who are you" failures) — this is a "you are known but not allowed"
    failure, so it is safe (and useful) to say why.
    """
    return HTTPException(
        status_code=403, detail={"error": {"code": "FORBIDDEN", "message": message}}
    )


@dataclass(frozen=True)
class Principal:
    """The authenticated caller for this request, with its authorized workspace."""

    kind: str  # "user" | "api_key"
    id: uuid.UUID
    role: UserRole | None
    scopes: list[str] = field(default_factory=list)
    workspace_id: uuid.UUID | None = None


def _extract_bearer_credential(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise auth_failed_exception()
    credential = authorization[len("Bearer ") :].strip()
    if not credential:
        raise auth_failed_exception()
    return credential


def _parse_workspace_header(x_workspace_id: str | None) -> uuid.UUID | None:
    if x_workspace_id is None:
        return None
    try:
        return uuid.UUID(x_workspace_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "INVALID_WORKSPACE_HEADER",
                    "message": "X-Workspace-Id must be a valid UUID.",
                }
            },
        ) from exc


def _lookup_api_key_candidates(key_prefix: str) -> list[ApiKey]:
    """Look up every ``ApiKey`` sharing ``key_prefix`` via the BYPASSRLS auth path.

    ``key_prefix`` is a short, non-unique lookup slice (FR-016) — several
    rows may share it; the caller verifies each candidate's full-secret
    hash to resolve the actual match (collision-safe).
    """
    with get_auth_session() as auth_session:
        rows = (
            auth_session.execute(
                select(ApiKey).where(ApiKey.key_prefix == key_prefix)  # noqa: workspace-scope
            )
            .scalars()
            .all()
        )
    return list(rows)


def _fire_last_used_throttle(api_key_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Best-effort ``last_used_at`` write, gated by the Redis throttle (FR-015/SC-008).

    Never raises and never blocks authentication on failure — the
    throttle gate itself already fails safe to "skip" on a Redis error
    (:func:`app_shared.security.last_used.should_write_last_used`).
    """
    settings = get_settings()
    redis_client = get_redis_client()
    if not should_write_last_used(
        redis_client,
        key_id=api_key_id,
        throttle_seconds=settings.API_KEY_LAST_USED_THROTTLE_SECONDS,
    ):
        return
    try:
        with get_auth_session() as auth_session:
            auth_session.execute(
                update(ApiKey)
                .where(ApiKey.id == api_key_id, ApiKey.workspace_id == workspace_id)
                .values(last_used_at=datetime.now(timezone.utc))
            )
            auth_session.commit()
    except Exception:
        # Usage tracking is best-effort — never fail the request over it.
        pass


def _authenticate_api_key(credential: str) -> Principal:
    key_prefix = parse_prefix(credential)
    candidates = _lookup_api_key_candidates(key_prefix)

    matched: ApiKey | None = None
    for candidate in candidates:
        if verify_api_key(credential, candidate.key_hash):
            matched = candidate
            break

    if matched is None:
        raise auth_failed_exception()

    if matched.status != ApiKeyStatus.ACTIVE:
        raise auth_failed_exception()

    redis_client = get_redis_client()
    ws_status = get_workspace_status(redis_client, get_auth_session, matched.workspace_id)
    if ws_status != ACTIVE_STATUS:
        raise auth_failed_exception()

    _fire_last_used_throttle(matched.id, matched.workspace_id)

    return Principal(
        kind="api_key",
        id=matched.id,
        role=None,
        scopes=list(matched.scopes),
        workspace_id=matched.workspace_id,
    )


def _authenticate_jwt(credential: str) -> Principal:
    settings = get_settings()
    try:
        claims = decode_access_token(
            credential, secret=settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM
        )
    except Exception as exc:
        raise auth_failed_exception() from exc

    if claims.get("type") != "access":
        raise auth_failed_exception()

    try:
        user_id = uuid.UUID(str(claims["sub"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise auth_failed_exception() from exc

    role_claim = claims.get("role")
    try:
        role = UserRole(role_claim) if role_claim is not None else None
    except ValueError as exc:
        raise auth_failed_exception() from exc

    workspace_claim = claims.get("workspace_id")
    home_workspace_id = uuid.UUID(str(workspace_claim)) if workspace_claim else None

    redis_client = get_redis_client()
    user_status = get_user_status(redis_client, get_auth_session, user_id)
    if user_status != ACTIVE_STATUS:
        raise auth_failed_exception()

    if home_workspace_id is not None:
        ws_status = get_workspace_status(redis_client, get_auth_session, home_workspace_id)
        if ws_status != ACTIVE_STATUS:
            raise auth_failed_exception()

    return Principal(
        kind="user",
        id=user_id,
        role=role,
        scopes=list(claims.get("scopes") or []),
        workspace_id=home_workspace_id,
    )


def _resolve_workspace(principal: Principal, requested_workspace_id: uuid.UUID | None) -> uuid.UUID:
    """Resolve + authorize the single workspace context for this request.

    - A principal with its own (non-null) ``workspace_id`` may only ever
      act in that workspace — an explicit ``X-Workspace-Id`` that
      disagrees is refused (403): assuming a workspace is a
      role-authorized act, never a wildcard bypass.
    - A principal with no home workspace (a SUPER_ADMIN JWT not yet
      bound) MUST supply an explicit ``X-Workspace-Id``; any other
      role/kind with a null workspace is a hard failure (only
      SUPER_ADMIN may have one).
    """
    if principal.workspace_id is not None:
        if requested_workspace_id is not None and requested_workspace_id != principal.workspace_id:
            raise _forbidden(
                "This principal may not assume a workspace other than its own."
            )
        return principal.workspace_id

    if principal.kind != "user" or principal.role != UserRole.SUPER_ADMIN:
        # Only a SUPER_ADMIN user may have a null home workspace; anyone
        # else reaching here is a data/claims inconsistency — fail closed.
        raise auth_failed_exception()

    if requested_workspace_id is None:
        raise _forbidden(
            "SUPER_ADMIN must supply an explicit X-Workspace-Id to act on a workspace."
        )
    return requested_workspace_id


def get_current_principal(
    authorization: str | None = Header(default=None),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
) -> Iterator[tuple[Session, Principal]]:
    """FastAPI dependency: resolve the principal, open the request txn, set context.

    Yields ``(session, principal)`` where ``principal.workspace_id`` is
    the single, authorized workspace context already applied to
    ``session`` via :func:`app_shared.database.set_workspace_context`
    (FR-017). Route handlers perform all workspace-owned reads/writes
    through this session, never a fresh one, so RLS sees the resolved
    context.
    """
    credential = _extract_bearer_credential(authorization)

    if credential.startswith(API_KEY_PREFIX):
        principal = _authenticate_api_key(credential)
    else:
        principal = _authenticate_jwt(credential)

    requested_workspace_id = _parse_workspace_header(x_workspace_id)
    workspace_id = _resolve_workspace(principal, requested_workspace_id)
    principal = Principal(
        kind=principal.kind,
        id=principal.id,
        role=principal.role,
        scopes=principal.scopes,
        workspace_id=workspace_id,
    )

    with get_session() as session:
        set_workspace_context(session, workspace_id)
        yield session, principal
        session.commit()


def require_role(*roles: UserRole):
    """Dependency factory: require ``principal.role`` to be one of ``roles``.

    For human (JWT) endpoints only — an API-key principal has no
    ``role`` and is always refused by this guard (e.g. api-key
    management is WORKSPACE_ADMIN+, contracts/api-keys.md).
    """

    def _check(
        principal_ctx: tuple[Session, Principal] = Depends(get_current_principal),
    ) -> tuple[Session, Principal]:
        _session, principal = principal_ctx
        if principal.kind != "user" or principal.role not in roles:
            raise _forbidden("This role is not authorized for this endpoint.")
        return principal_ctx

    return _check


def require_scopes(*scopes: str):
    """Dependency factory: require every scope in ``scopes`` to be granted (FR-013).

    Primarily for API-key requests; a JWT (human) principal with no
    ``scopes`` claim is refused by a scope-gated endpoint.
    """

    def _check(
        principal_ctx: tuple[Session, Principal] = Depends(get_current_principal),
    ) -> tuple[Session, Principal]:
        _session, principal = principal_ctx
        if not has_scopes(principal.scopes, scopes):
            raise _forbidden("This principal lacks a required scope.")
        return principal_ctx

    return _check
