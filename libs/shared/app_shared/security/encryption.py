"""Versioned Fernet credential encryption (`contracts/encryption.md`, SPEC-10 FR-003, §33).

Pure helper — depends only on ``cryptography.fernet`` + ``get_settings()``
(no FastAPI/Scrapy). Protects ``proxy_providers.password_encrypted``: a
plaintext proxy password is encrypted with the *primary* keyring version
on write and decrypted by looking up whichever version encrypted it,
never falling back to plaintext. The keyring supports rotation
(``reencrypt_secret``) so an old key can keep decrypting existing rows
while new writes use the new primary version (§33 rotation story).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app_shared.config import _parse_encryption_keys, get_settings


@dataclass(frozen=True)
class EncryptedSecret:
    """A ciphertext plus the keyring version that produced it."""

    ciphertext: str
    key_version: int


class SecretDecryptionError(RuntimeError):
    """Raised when a ciphertext cannot be decrypted — missing key version or
    an invalid/tampered token. Operational: never swallowed into a blank or
    the raw ciphertext being returned instead."""


@lru_cache
def _keyring() -> dict[int, Fernet]:
    """Build ``{version: Fernet(key)}`` once per process from ``Settings``.

    ``Settings`` already validates (at construction time) that the
    primary version is present in ``ENCRYPTION_KEYS`` — this just turns
    each urlsafe-base64 key string into a ``Fernet`` instance.
    """
    settings = get_settings()
    raw_keyring = _parse_encryption_keys(settings.ENCRYPTION_KEYS)
    return {version: Fernet(key.encode("utf-8")) for version, key in raw_keyring.items()}


def _primary_version() -> int:
    return get_settings().ENCRYPTION_PRIMARY_KEY_VERSION


def encrypt_secret(plaintext: str) -> EncryptedSecret:
    """Encrypt ``plaintext`` with the PRIMARY keyring version.

    Fernet includes a random IV/nonce per call, so encrypting the same
    plaintext twice yields different ciphertext — both remain decryptable.
    """
    version = _primary_version()
    fernet = _keyring()[version]
    token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return EncryptedSecret(ciphertext=token, key_version=version)


def decrypt_secret(ciphertext: str, key_version: int) -> str:
    """Decrypt ``ciphertext`` using ``key_version`` from the keyring.

    Raises :class:`SecretDecryptionError` — never returns the raw
    ciphertext or a blank string — when ``key_version`` is absent from
    the keyring (retired/unknown) or the token fails to decrypt (wrong
    key, corrupted/tampered token).
    """
    fernet = _keyring().get(key_version)
    if fernet is None:
        raise SecretDecryptionError(
            f"no encryption key registered for key_version={key_version!r}"
        )
    try:
        return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptionError(
            f"ciphertext failed to decrypt under key_version={key_version!r}"
        ) from exc


def reencrypt_secret(ciphertext: str, key_version: int) -> EncryptedSecret:
    """Rotation primitive: decrypt under ``key_version``, re-encrypt under PRIMARY.

    Used by an out-of-band rotation job (§33 step 2) walking rows still
    on an old key version so they move to the new primary version ahead
    of the old key's retirement.
    """
    plaintext = decrypt_secret(ciphertext, key_version)
    return encrypt_secret(plaintext)
