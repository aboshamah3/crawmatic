"""Authentication endpoints (`contracts/api-auth.md`) — US1 (Sign in).

``POST /v1/auth/login`` / ``POST /v1/auth/refresh`` / ``POST /v1/auth/logout``.
All failures use the uniform auth error (``app.errors``) — no factor
disclosure (FR-006). Credential lookups on RLS'd tables (``users``) run
through the BYPASSRLS ``get_auth_session()`` path (research D4) since they
inherently occur before any workspace context exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select, text

from app_shared.config import get_settings
from app_shared.database import get_auth_session, get_session
from app_shared.enums import UserStatus, WorkspaceStatus
from app_shared.models import RefreshToken, User, Workspace
from app_shared.redis_client import get_redis_client
from app_shared.security.jwt import encode_access_token
from app_shared.security.passwords import dummy_verify, verify_password
from app_shared.security.rate_limit import check_and_increment_login
from app_shared.security.tokens import (
    ROTATE_REFRESH_TOKEN_SQL,
    REVOKE_REFRESH_TOKEN_SQL,
    generate_refresh_token,
    hash_token,
)

from app.errors import auth_failed_exception, rate_limited_exception

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _issue_pair(*, user: User) -> TokenPairResponse:
    """Issue a fresh access+refresh pair for ``user`` and persist the refresh hash."""
    settings = get_settings()
    access_token = encode_access_token(
        user_id=user.id,
        workspace_id=user.workspace_id,
        role=str(user.role),
        secret=settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
        ttl_seconds=settings.ACCESS_TOKEN_TTL_SECONDS,
    )
    raw_refresh, refresh_hash = generate_refresh_token()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.REFRESH_TOKEN_TTL_SECONDS)

    # refresh_tokens carries NO RLS (reachable only by unforgeable
    # token_hash) — the ordinary app-role session is sufficient, no
    # workspace context needed for this insert.
    with get_session() as session:
        session.add(
            RefreshToken(
                user_id=user.id,
                token_hash=refresh_hash,
                expires_at=expires_at,
                created_at=now,
            )
        )
        session.commit()

    return TokenPairResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=settings.ACCESS_TOKEN_TTL_SECONDS,
    )


@router.post("/login", response_model=TokenPairResponse)
def login(payload: LoginRequest, request: Request) -> TokenPairResponse:
    settings = get_settings()

    # 1. Rate-limit gate FIRST — before any credential work (FR-007/SC-009).
    redis_client = get_redis_client()
    result = check_and_increment_login(
        redis_client,
        email=payload.email,
        source_ip=_client_ip(request),
        max_attempts=settings.LOGIN_RATE_LIMIT_MAX_ATTEMPTS,
        window_seconds=settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS,
    )
    if not result.allowed:
        raise rate_limited_exception(result.retry_after_seconds)

    # 2. Look up user by email via the BYPASSRLS pre-auth path. This is
    #    the fixed, credential-filtered lookup research D4 carves out of
    #    ordinary workspace scoping: identity isn't known yet, so there is
    #    no workspace_id to filter on. Runs through get_auth_session()
    #    (BYPASSRLS crawmatic_auth role), never the app-role session.
    with get_auth_session() as auth_session:
        user = auth_session.execute(
            select(User).where(User.email == payload.email)  # noqa: workspace-scope
        ).scalar_one_or_none()

        # 3. ALWAYS perform a hash comparison — dummy-verify on unknown
        #    email — so timing is uniform whether or not the account
        #    exists (FR-006).
        if user is None:
            dummy_verify(payload.password)
            raise auth_failed_exception()

        if not verify_password(user.password_hash, payload.password):
            raise auth_failed_exception()

        # 4. Cached/DB status check — user must be active, and its
        #    workspace (if bound) must be active too.
        if user.status != UserStatus.ACTIVE:
            raise auth_failed_exception()

        if user.workspace_id is not None:
            workspace = auth_session.execute(
                select(Workspace).where(Workspace.id == user.workspace_id)
            ).scalar_one_or_none()
            if workspace is None or workspace.status != WorkspaceStatus.ACTIVE:
                raise auth_failed_exception()

    # 5. Issue the pair.
    return _issue_pair(user=user)


@router.post("/refresh", response_model=TokenPairResponse)
def refresh(payload: RefreshRequest) -> TokenPairResponse:
    presented_hash = hash_token(payload.refresh_token)

    # Atomic single-statement rotation (research D3): one row -> this
    # caller won the race; zero rows -> already rotated/expired/revoked
    # (covers FR-009/FR-010/FR-011) -> uniform 401.
    with get_session() as session:
        row = session.execute(
            text(ROTATE_REFRESH_TOKEN_SQL), {"token_hash": presented_hash}
        ).mappings().first()
        session.commit()

    if row is None:
        raise auth_failed_exception()

    user_id = row["user_id"]

    # The winning caller must resolve the associated user's current
    # role/workspace to mint the new pair. This user lookup is on an
    # RLS'd table reached before any workspace context is known for
    # this request (analogous to the login lookup) -> BYPASSRLS path.
    with get_auth_session() as auth_session:
        user = auth_session.execute(
            select(User).where(User.id == user_id)  # noqa: workspace-scope
        ).scalar_one_or_none()

    if user is None or user.status != UserStatus.ACTIVE:
        raise auth_failed_exception()

    return _issue_pair(user=user)


@router.post("/logout", status_code=204)
def logout(payload: LogoutRequest) -> None:
    presented_hash = hash_token(payload.refresh_token)
    with get_session() as session:
        session.execute(text(REVOKE_REFRESH_TOKEN_SQL), {"token_hash": presented_hash})
        session.commit()
    # Idempotent: 0 rows affected (already revoked/unknown) still -> 204.
    return None
