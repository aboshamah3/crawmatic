"""Unit tests for access-token JWT primitives (SPEC-03 T023, FR-024).

`app_shared.security.jwt` — no DB/Redis required; secret/algorithm are
passed explicitly by the caller (no ``Settings`` dependency here).
"""

from __future__ import annotations

import uuid

import jwt as pyjwt
import pytest

from app_shared.security.jwt import decode_access_token, encode_access_token

SECRET = "test-secret"
OTHER_SECRET = "a-different-secret"


def _encode(**overrides: object) -> str:
    kwargs: dict[str, object] = dict(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role="workspace_admin",
        secret=SECRET,
        ttl_seconds=900,
    )
    kwargs.update(overrides)
    return encode_access_token(**kwargs)  # type: ignore[arg-type]


def test_encode_decode_round_trips_all_claims() -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    token = encode_access_token(
        user_id=user_id,
        workspace_id=workspace_id,
        role="workspace_admin",
        scopes=["products:read"],
        secret=SECRET,
        ttl_seconds=900,
    )

    claims = decode_access_token(token, secret=SECRET)

    assert claims["sub"] == str(user_id)
    assert claims["workspace_id"] == str(workspace_id)
    assert claims["role"] == "workspace_admin"
    assert claims["scopes"] == ["products:read"]
    assert claims["type"] == "access"
    assert "iat" in claims and "exp" in claims and "jti" in claims


def test_workspace_id_may_be_null_for_unscoped_super_admin() -> None:
    token = _encode(workspace_id=None)
    claims = decode_access_token(token, secret=SECRET)
    assert claims["workspace_id"] is None


def test_type_claim_is_access() -> None:
    token = _encode()
    claims = decode_access_token(token, secret=SECRET)
    assert claims["type"] == "access"


def test_expired_token_raises_on_decode() -> None:
    token = _encode(ttl_seconds=-1)  # already expired
    with pytest.raises(pyjwt.ExpiredSignatureError):
        decode_access_token(token, secret=SECRET)


def test_wrong_secret_raises_on_decode() -> None:
    token = _encode()
    with pytest.raises(pyjwt.InvalidTokenError):
        decode_access_token(token, secret=OTHER_SECRET)


def test_tampered_token_raises_on_decode() -> None:
    token = _encode()
    # Flip a character in the middle of the signature segment (not the
    # very last character of the token, whose trailing base64 bits can be
    # non-significant padding and might not change the decoded bytes).
    header, payload, signature = token.split(".")
    mid = len(signature) // 2
    flipped_char = "A" if signature[mid] != "A" else "B"
    tampered_signature = signature[:mid] + flipped_char + signature[mid + 1 :]
    tampered = f"{header}.{payload}.{tampered_signature}"

    with pytest.raises(pyjwt.InvalidTokenError):
        decode_access_token(tampered, secret=SECRET)


def test_jti_is_unique_per_token() -> None:
    token_a = _encode()
    token_b = _encode()
    claims_a = decode_access_token(token_a, secret=SECRET)
    claims_b = decode_access_token(token_b, secret=SECRET)
    assert claims_a["jti"] != claims_b["jti"]
