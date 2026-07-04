"""Unit tests for the Fernet credential-encryption keyring (SPEC-10 T012,
`contracts/encryption.md`, FR-003, SC-003).

Pure unit tests — no infra. Exercises `app_shared.security.encryption`
against a real `Settings` instance (env-driven, mirroring
`tests/unit/test_config.py`'s `REQUIRED_ENV` pattern) rather than a fake,
since the keyring is built directly from `get_settings()`. Both
`get_settings` (config.py) and the module-local `_keyring` (encryption.py)
are process-wide `lru_cache` singletons, so every test clears both caches
before and after running to avoid cross-test leakage.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app_shared import config as config_module
from app_shared.security import encryption as enc

REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
}

KEY_V1 = Fernet.generate_key().decode("ascii")
KEY_V2 = Fernet.generate_key().decode("ascii")


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    """Clear both lru_cache singletons before AND after each test.

    `get_settings()` is cached in `app_shared.config`; `_keyring()` is
    cached in `app_shared.security.encryption`. Without clearing both, a
    test that sets env vars via `monkeypatch` would see a stale
    `Settings`/keyring built by an earlier test.
    """
    config_module.get_settings.cache_clear()
    enc._keyring.cache_clear()
    yield
    config_module.get_settings.cache_clear()
    enc._keyring.cache_clear()


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def _set_single_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENCRYPTION_KEYS", f"1:{KEY_V1}")
    monkeypatch.setenv("ENCRYPTION_PRIMARY_KEY_VERSION", "1")


def _set_two_key_env(monkeypatch: pytest.MonkeyPatch, *, primary_version: int) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENCRYPTION_KEYS", f"1:{KEY_V1},2:{KEY_V2}")
    monkeypatch.setenv("ENCRYPTION_PRIMARY_KEY_VERSION", str(primary_version))


# --- round-trip -------------------------------------------------------------


def test_decrypt_of_encrypt_round_trips_for_plain_and_unicode_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_single_key_env(monkeypatch)

    for plaintext in ("hunter2", "s3cr3t-pässwörd-éè", "", "a" * 500):
        secret = enc.encrypt_secret(plaintext)
        assert enc.decrypt_secret(secret.ciphertext, secret.key_version) == plaintext


def test_encrypt_secret_uses_the_primary_key_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_two_key_env(monkeypatch, primary_version=2)

    secret = enc.encrypt_secret("proxy-password")

    assert secret.key_version == 2
    assert enc.decrypt_secret(secret.ciphertext, 2) == "proxy-password"


def test_encrypt_twice_yields_different_ciphertext_but_both_decrypt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_single_key_env(monkeypatch)

    first = enc.encrypt_secret("same-plaintext")
    second = enc.encrypt_secret("same-plaintext")

    assert first.ciphertext != second.ciphertext
    assert enc.decrypt_secret(first.ciphertext, first.key_version) == "same-plaintext"
    assert enc.decrypt_secret(second.ciphertext, second.key_version) == "same-plaintext"


# --- decrypt failure modes ---------------------------------------------------


def test_decrypt_with_unknown_key_version_raises_and_never_returns_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_single_key_env(monkeypatch)
    secret = enc.encrypt_secret("payload")

    with pytest.raises(enc.SecretDecryptionError):
        enc.decrypt_secret(secret.ciphertext, key_version=999)


def test_decrypt_with_invalid_token_raises_secret_decryption_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_single_key_env(monkeypatch)

    with pytest.raises(enc.SecretDecryptionError):
        enc.decrypt_secret("not-a-valid-fernet-token", key_version=1)


# --- rotation -----------------------------------------------------------------


def test_reencrypt_secret_moves_a_v1_token_to_the_primary_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_two_key_env(monkeypatch, primary_version=1)
    old_secret = enc.encrypt_secret("rotate-me")
    assert old_secret.key_version == 1

    # Promote v2 to primary (simulating the operational rotation step).
    monkeypatch.setenv("ENCRYPTION_PRIMARY_KEY_VERSION", "2")
    config_module.get_settings.cache_clear()
    enc._keyring.cache_clear()

    rotated = enc.reencrypt_secret(old_secret.ciphertext, old_secret.key_version)

    assert rotated.key_version == 2
    assert enc.decrypt_secret(rotated.ciphertext, rotated.key_version) == "rotate-me"


def test_two_key_ring_decrypts_old_while_writing_new(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_two_key_env(monkeypatch, primary_version=1)
    old_secret = enc.encrypt_secret("legacy-value")

    monkeypatch.setenv("ENCRYPTION_PRIMARY_KEY_VERSION", "2")
    config_module.get_settings.cache_clear()
    enc._keyring.cache_clear()

    new_secret = enc.encrypt_secret("new-value")

    assert new_secret.key_version == 2
    # The old v1 ciphertext still decrypts even though the primary moved to v2.
    assert enc.decrypt_secret(old_secret.ciphertext, old_secret.key_version) == "legacy-value"
    assert enc.decrypt_secret(new_secret.ciphertext, new_secret.key_version) == "new-value"


# --- no plaintext leakage ------------------------------------------------------


def test_encrypted_secret_repr_never_contains_the_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_single_key_env(monkeypatch)
    plaintext = "super-secret-plaintext-marker"

    secret = enc.encrypt_secret(plaintext)

    assert plaintext not in repr(secret)
    assert plaintext not in secret.ciphertext
