"""Access-token JWT primitives (`contracts/security-jwt.md`).

PyJWT-backed, framework-agnostic (FR-024, §32/§35). No FastAPI/DB imports.
A short-lived, stateless, signed JWT lets the request pipeline resolve
identity + workspace + role/scopes with no DB read on the hot path beyond
the cached status check.
"""

from __future__ import annotations

import time
import uuid

import jwt as _pyjwt

ACCESS_TOKEN_TYPE = "access"


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
    ``type="access"``, ``iat``, ``exp``, ``jti`` (a fresh random UUID per
    token, per D2).
    """
    now = int(time.time())
    claims: dict[str, object] = {
        "sub": str(user_id),
        "workspace_id": str(workspace_id) if workspace_id is not None else None,
        "role": role,
        "type": ACCESS_TOKEN_TYPE,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    if scopes is not None:
        claims["scopes"] = scopes
    return _pyjwt.encode(claims, secret, algorithm=algorithm)


def decode_access_token(token: str, *, secret: str, algorithm: str = "HS256") -> dict:
    """Decode + verify an access-token JWT.

    Verifies the signature and ``exp`` (PyJWT raises
    ``jwt.ExpiredSignatureError`` / ``jwt.InvalidTokenError`` on tamper,
    wrong secret, or expiry — the caller maps these to the uniform 401).
    """
    return _pyjwt.decode(token, secret, algorithms=[algorithm])
