"""Argon2id password hashing primitives (`contracts/security-passwords.md`).

Framework-agnostic (no FastAPI/DB imports) — backed by ``argon2-cffi``'s
``argon2.PasswordHasher``. Parameters come from ``Settings.ARGON2_*`` when
set, else argon2-cffi's recommended defaults (FR-005, §33).

``dummy_verify()`` exists purely for timing uniformity (FR-006): the login
handler must spend the same argon2 work on an unknown-email attempt as it
does on a real one, so it always calls either ``verify_password`` (known
user) or ``dummy_verify`` (unknown user) — never skips the hash comparison
entirely.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from app_shared.config import get_settings


def _build_hasher() -> PasswordHasher:
    """Build a ``PasswordHasher`` from ``Settings.ARGON2_*`` when configured."""
    settings = get_settings()
    kwargs: dict[str, int] = {}
    if settings.ARGON2_TIME_COST is not None:
        kwargs["time_cost"] = settings.ARGON2_TIME_COST
    if settings.ARGON2_MEMORY_COST is not None:
        kwargs["memory_cost"] = settings.ARGON2_MEMORY_COST
    if settings.ARGON2_PARALLELISM is not None:
        kwargs["parallelism"] = settings.ARGON2_PARALLELISM
    return PasswordHasher(**kwargs)


def hash_password(plaintext: str) -> str:
    """Hash ``plaintext`` with argon2id, returning the encoded hash string.

    Each call embeds a fresh random salt (and the tuning parameters) in the
    returned string — no separate salt column is needed (FR-005). The
    plaintext is never logged.
    """
    return _build_hasher().hash(plaintext)


def verify_password(stored_hash: str, plaintext: str) -> bool:
    """Return ``True`` iff ``plaintext`` matches ``stored_hash``.

    Never raises: any mismatch, malformed hash, or verification error is
    treated as ``False`` so callers get uniform behavior (FR-006).
    """
    try:
        return _build_hasher().verify(stored_hash, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Return ``True`` iff ``stored_hash`` used weaker-than-current parameters."""
    return _build_hasher().check_needs_rehash(stored_hash)


# A fixed, module-level dummy hash used for the unknown-email login path
# (FR-006). It is a *real*, validly-encoded argon2id hash (produced by
# hash_password on a fixed, non-secret plaintext) so verifying against it
# costs the genuine argon2 computation — not a short-circuited parse
# error — keeping response timing uniform whether or not the email
# exists. Lazily built on first use (module import must stay cheap and
# must not require Settings to already be configured).
_DUMMY_PLAINTEXT = "dummy-password-for-timing-uniformity"
_dummy_hash: str | None = None


def _get_dummy_hash() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(_DUMMY_PLAINTEXT)
    return _dummy_hash


def dummy_verify(plaintext: str) -> None:
    """Run an argon2 verify against a fixed dummy hash; discard the result.

    Called on the unknown-email login path so the total work (and thus
    timing) matches the known-email path, which always calls
    :func:`verify_password`. The outcome is intentionally ignored — the
    caller already knows this attempt must fail (no such user).
    """
    try:
        _build_hasher().verify(_get_dummy_hash(), plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass
