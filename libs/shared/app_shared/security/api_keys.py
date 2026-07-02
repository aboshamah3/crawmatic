"""API-key primitives (`contracts/security-tokens.md`).

Stdlib-only (``secrets``, ``hashlib``, ``hmac``) high-entropy generation +
fast SHA-256 hashing (FR-012/FR-016, §33). A password KDF MUST NOT be
used here — a 256-bit random secret needs only a fast hash, not argon2.
No FastAPI/DB imports.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# A recognizable, non-secret prefix on every issued key. The auth
# dependency (apps/api/app/deps.py) uses this shape to route a bearer
# credential to the api-key path rather than attempting a JWT decode.
API_KEY_PREFIX = "ck_"

# Length (in characters, after the `ck_` prefix) of the short, non-secret
# `key_prefix` slice stored/displayed for lookup (FR-012/FR-016).
_KEY_PREFIX_LOOKUP_CHARS = 6


def hash_api_key(full_secret: str) -> str:
    """Return the deterministic sha256 hex digest of ``full_secret``.

    A fast hash — **not** a KDF (FR-012): the secret is already a
    256-bit random value, so a slow password hash would only add
    needless cost to every machine-client request.
    """
    return hashlib.sha256(full_secret.encode("utf-8")).hexdigest()


def parse_prefix(full_secret: str) -> str:
    """Extract the stored ``key_prefix`` from a presented ``full_secret``.

    The prefix is ``API_KEY_PREFIX`` plus the first
    ``_KEY_PREFIX_LOOKUP_CHARS`` characters following it — a short,
    non-secret slice used purely for the DB lookup (FR-016); it never
    substitutes for the full-secret hash comparison.
    """
    remainder = full_secret[len(API_KEY_PREFIX) :]
    return API_KEY_PREFIX + remainder[:_KEY_PREFIX_LOOKUP_CHARS]


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(full_secret, key_prefix, key_hash)`` for a fresh API key.

    ``full_secret`` is ``API_KEY_PREFIX`` + ``secrets.token_urlsafe(32)``
    (256-bit entropy), shown to the caller **exactly once**. ``key_prefix``
    is the short, non-secret lookup slice (:func:`parse_prefix`).
    ``key_hash = sha256(full_secret)`` is the only secret-derived value
    ever persisted (FR-012).
    """
    full_secret = API_KEY_PREFIX + secrets.token_urlsafe(32)
    key_prefix = parse_prefix(full_secret)
    key_hash = hash_api_key(full_secret)
    return full_secret, key_prefix, key_hash


def verify_api_key(full_secret: str, key_hash: str) -> bool:
    """Constant-time comparison of ``sha256(full_secret)`` against ``key_hash``.

    **Prefix-collision safety (FR-016)**: two keys may share a
    ``key_prefix`` (a short, non-unique lookup slice); authentication
    resolves by matching the *full-secret* hash via
    ``hmac.compare_digest``, so a colliding prefix can never authenticate
    the wrong key.
    """
    return hmac.compare_digest(hash_api_key(full_secret), key_hash)
