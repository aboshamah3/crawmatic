"""Refresh-token primitives (`contracts/security-tokens.md`).

Stdlib-only (``secrets``, ``hashlib``, ``hmac``) high-entropy generation +
fast SHA-256 hashing (FR-008/FR-009/FR-010/FR-011, §33). A password KDF
MUST NOT be used here — a 256-bit random value needs only a fast hash, not
argon2 (that would make legitimate refresh exchanges needlessly
expensive). No FastAPI/DB imports.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# The atomic, single-statement rotation predicate (research D3): the
# caller (apps/api/app/routers/auth.py) executes this exact SQL shape
# inside the request transaction. One row returned -> this caller won the
# race and must INSERT a new refresh_tokens row; zero rows -> the
# presented token was already rotated, expired, or revoked (or never
# existed) -> reject with the uniform auth error. No session-scoped lock
# is used (correct under PgBouncer transaction pooling, FR-010/SC-002).
#
# Security plan 2026-10-02 (E10): every refresh token belongs to a
# `family_id` (one login = one family; each rotation inherits it). A row
# from before the column existed has a NULL family; rotating it adopts the
# caller-supplied fresh `:new_family_id`, so the rotated row and its
# successor share a family from then on.
ROTATE_REFRESH_TOKEN_SQL = """
UPDATE refresh_tokens
   SET revoked_at = now(),
       family_id = COALESCE(family_id, :new_family_id)
 WHERE token_hash = :token_hash
   AND revoked_at IS NULL
   AND expires_at > now()
RETURNING id, user_id, family_id
"""

# Reuse detection (E10): run when the rotation above matched zero rows. If
# the presented token exists but was ALREADY revoked (rotated or logged
# out), someone is replaying a superseded token -- revoke every still-live
# token in its family, so whichever party holds the current one (the
# legitimate client or the thief) has to log in again. An expired or
# unknown token matches nothing here (no family is touched).
REVOKE_REUSED_REFRESH_FAMILY_SQL = """
UPDATE refresh_tokens
   SET revoked_at = now()
 WHERE family_id = (
         SELECT family_id
           FROM refresh_tokens
          WHERE token_hash = :token_hash
            AND revoked_at IS NOT NULL
            AND family_id IS NOT NULL
       )
   AND revoked_at IS NULL
"""

# The idempotent revocation predicate used by POST /v1/auth/logout: revokes
# the presented token if it is still live; a no-op (0 rows) if it was
# already revoked/rotated/never existed -- logout is always 204 either way.
REVOKE_REFRESH_TOKEN_SQL = """
UPDATE refresh_tokens
   SET revoked_at = now()
 WHERE token_hash = :token_hash
   AND revoked_at IS NULL
"""


def hash_token(raw: str) -> str:
    """Return the deterministic sha256 hex digest of ``raw``."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_refresh_token() -> tuple[str, str]:
    """Return ``(raw, token_hash)`` for a fresh refresh token.

    ``raw`` is a high-entropy ``secrets.token_urlsafe(32)`` value (256-bit)
    returned to the client; only ``token_hash = sha256(raw)`` is ever
    persisted (FR-008).
    """
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def verify_token_hash(raw: str, token_hash: str) -> bool:
    """Constant-time comparison of ``sha256(raw)`` against ``token_hash``."""
    return hmac.compare_digest(hash_token(raw), token_hash)
