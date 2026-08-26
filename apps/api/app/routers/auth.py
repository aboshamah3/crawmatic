"""Authentication endpoints (`contracts/api-auth.md`) — US1 (Sign in).

``POST /v1/auth/login`` / ``POST /v1/auth/refresh`` / ``POST /v1/auth/logout``.
All failures use the uniform auth error (``app.errors``) — no factor
disclosure (FR-006). Credential lookups on RLS'd tables (``users``,
``refresh_tokens``) run through the BYPASSRLS ``get_auth_session()`` path
(research D4) since they inherently occur before any workspace context
exists.

READY-007 / P0.5 (EPA B8b): ``refresh_tokens`` gained transitive RLS
through ``users.user_id`` at alembic head ``b6d94c2f1a70``, so the three
statements this module issues against it — issue, rotate, revoke — moved
off the ordinary ``get_session()`` app-role connection and onto the auth
seam. None of them can run under a workspace context, and that is a
property of the flows, not an oversight:

* **rotate** (``POST /refresh``) and **revoke** (``POST /logout``) are
  keyed by an unforgeable ``token_hash`` and resolve the principal — no
  ``app.workspace_id`` exists yet to scope them with. Under the new
  policy the app role would match zero rows and every refresh and logout
  would fail closed as "wrong credentials".
* **issue** (``_issue_pair``) knows the user, but a ``SUPER_ADMIN`` has
  ``workspace_id IS NULL`` and therefore satisfies no policy context at
  all — the INSERT's ``WITH CHECK`` could never pass for one.

The RLS policy is thus a confinement of ``crawmatic_app`` (which now has
no reason to touch this table) rather than a filter any auth path relies
on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select, text

from app_shared.config import get_settings
from app_shared.database import get_auth_session
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

from app.client_ip import client_ip
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
    """The rate-limit source for this login attempt.

    EPA W5.5-L1 item 1: this used to be the raw socket peer, which behind
    Railway is the edge proxy — so the per-source half of
    `check_and_increment_login` was one global counter for the entire
    internet. `app.client_ip` honours the declared proxy depth instead, and
    ignores an `X-Forwarded-For` that did not come through that chain.
    """
    return client_ip(request)


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

    # refresh_tokens carries transitive RLS through users.user_id
    # (b6d94c2f1a70). This INSERT runs with no workspace context — and
    # cannot be given one, since a SUPER_ADMIN's `workspace_id` is NULL
    # — so it goes through the sanctioned BYPASSRLS auth seam, like the
    # credential lookups above it.
    with get_auth_session() as session:  # noqa: workspace-scope
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
    #
    # PRE-AUTH: the presented hash is the only thing known about this
    # request; no principal, and therefore no `app.workspace_id`, exists
    # yet. Under refresh_tokens' transitive RLS (b6d94c2f1a70) the
    # app-role session would match zero rows here and every refresh
    # would fail closed as a bad token, so this runs on the BYPASSRLS
    # auth seam — the same carve-out the login lookup uses.
    with get_auth_session() as session:  # noqa: workspace-scope
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
    # Same pre-auth shape as the rotation above: keyed by token_hash
    # alone, with no workspace context to scope it with -> auth seam.
    # A logout that silently revoked nothing would leave a live token
    # outstanding while answering 204.
    with get_auth_session() as session:  # noqa: workspace-scope
        session.execute(text(REVOKE_REFRESH_TOKEN_SQL), {"token_hash": presented_hash})
        session.commit()
    # Idempotent: 0 rows affected (already revoked/unknown) still -> 204.
    return None
