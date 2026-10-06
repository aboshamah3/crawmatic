"""Access-token JWT primitives (`contracts/security-jwt.md`).

PyJWT-backed, framework-agnostic (FR-024, §32/§35). No FastAPI/DB imports.
A short-lived, stateless, signed JWT lets the request pipeline resolve
identity + workspace + role/scopes with no DB read on the hot path beyond
the cached status check.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import jwt as _pyjwt

ACCESS_TOKEN_TYPE = "access"

#: Audience and issuer stamped on, and required of, every engine token
#: (security plan 2026-10-02, E8). A token minted by another system that
#: happens to share ``JWT_SECRET`` -- e.g. a SaaS-shaped ``{email, exp}``
#: token -- carries neither, so it can no longer pass as an engine token.
ENGINE_AUDIENCE = "crawmatic-engine"
ENGINE_ISSUER = "crawmatic-engine"

#: The only algorithms the engine signs or verifies with (shared-secret
#: HMAC). ``none`` and every asymmetric family are refused at startup by
#: ``Settings`` and again here, so a mis-set ``JWT_ALGORITHM`` can never
#: widen what a decode accepts.
ALLOWED_JWT_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})


def encode_access_token(
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    role: str,
    scopes: list[str] | None = None,
    secret: str,
    algorithm: str = "HS256",
    ttl_seconds: int,
) -> str:
    """Encode a signed access-token JWT.

    Claims: ``sub`` (user_id), ``workspace_id`` (nullable — a SUPER_ADMIN
    token not yet bound to a workspace), ``role``, ``scopes`` (optional —
    primarily an API-key concept; user authorization is by ``role``),
    ``type="access"``, ``aud``/``iss`` (both ``crawmatic-engine``),
    ``iat``, ``exp``, ``jti`` (a fresh random UUID per token, per D2).
    """
    if algorithm not in ALLOWED_JWT_ALGORITHMS:
        raise ValueError(f"unsupported JWT algorithm {algorithm!r}")
    now = int(time.time())
    claims: dict[str, object] = {
        "sub": str(user_id),
        "workspace_id": str(workspace_id) if workspace_id is not None else None,
        "role": role,
        "type": ACCESS_TOKEN_TYPE,
        "aud": ENGINE_AUDIENCE,
        "iss": ENGINE_ISSUER,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    if scopes is not None:
        claims["scopes"] = scopes
    return _pyjwt.encode(claims, secret, algorithm=algorithm)


def decode_access_token(
    token: str,
    *,
    secret: str,
    algorithm: str = "HS256",
    expected_type: str = ACCESS_TOKEN_TYPE,
    legacy_aud_grace_until: datetime | None = None,
    now: datetime | None = None,
) -> dict:
    """Decode + verify an engine JWT.

    Verifies the signature, ``exp``, ``aud`` and ``iss`` (both
    ``crawmatic-engine``) and that ``type == expected_type``. PyJWT raises
    ``jwt.InvalidTokenError`` subclasses on every failure -- the caller
    maps them all to the uniform 401.

    Tokens minted before ``aud``/``iss`` existed carry neither. They are
    accepted ONLY while ``now < legacy_aud_grace_until`` (operator-set
    ``JWT_LEGACY_AUD_GRACE_UNTIL``, one access TTL after the deploy that
    starts stamping ``aud``); ``None`` means no grace at all. Even inside
    the grace window such a token must still carry ``type ==
    expected_type`` and a ``sub``, so a foreign ``{email, exp}`` token
    never passes. A token that carries a WRONG ``aud`` is never graced.
    """
    if algorithm not in ALLOWED_JWT_ALGORITHMS:
        raise _pyjwt.InvalidAlgorithmError(f"unsupported JWT algorithm {algorithm!r}")

    try:
        claims = _pyjwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            audience=ENGINE_AUDIENCE,
            issuer=ENGINE_ISSUER,
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
    except _pyjwt.MissingRequiredClaimError as exc:
        if exc.claim not in ("aud", "iss") or not _legacy_grace_open(
            legacy_aud_grace_until, now
        ):
            raise
        claims = _pyjwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            options={"require": ["exp", "sub"], "verify_aud": False},
        )
        # Grace covers tokens that predate aud/iss entirely -- not a token
        # that has one of them with some other value.
        if "aud" in claims or "iss" in claims:
            raise

    if claims.get("type") != expected_type:
        raise _pyjwt.InvalidTokenError("unexpected token type")
    return claims


def _legacy_grace_open(grace_until: datetime | None, now: datetime | None) -> bool:
    if grace_until is None:
        return False
    current = now if now is not None else datetime.now(timezone.utc)
    if grace_until.tzinfo is None:
        grace_until = grace_until.replace(tzinfo=timezone.utc)
    return current < grace_until
