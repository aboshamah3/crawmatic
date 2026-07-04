# Contract: Fernet credential encryption (`app_shared.security.encryption`)

Pure helper (depends only on `cryptography.fernet` + `Settings`). Protects
`proxy_providers.password_encrypted` (FR-003, §33). Never falls back to plaintext.

## Settings (append to `app_shared/config.py`)

```python
# --- Secret encryption (SPEC-10 FR-003, §33) ---
# Comma-separated "version:key" pairs; key is a urlsafe-base64 Fernet key.
# e.g. "1:kZ...=,2:9p...=". Required once a proxy password is stored.
ENCRYPTION_KEYS: str
ENCRYPTION_PRIMARY_KEY_VERSION: int = 1
```

(Declared required so a misconfigured deployment fails fast; a `field_validator` parses the
pairs into `{version: key}` and asserts the primary version is present.)

## API

```python
@dataclass(frozen=True)
class EncryptedSecret:
    ciphertext: str
    key_version: int

class SecretDecryptionError(RuntimeError): ...      # missing/unreadable key version — operational

def encrypt_secret(plaintext: str) -> EncryptedSecret
    # encrypt with the PRIMARY key; returns (token, primary_version).

def decrypt_secret(ciphertext: str, key_version: int) -> str
    # look up key_version in the keyring; Fernet.decrypt; raise SecretDecryptionError if the
    # version is absent or the token is invalid — NEVER return ciphertext or a blank.

def reencrypt_secret(ciphertext: str, key_version: int) -> EncryptedSecret
    # decrypt(old) then encrypt(primary) — the rotation primitive (decrypt-old / re-encrypt).
```

Keyring is built once per process (module-level `lru_cache`) from `get_settings()`.

## Rotation story (§33)

1. Add the new key as the highest version in `ENCRYPTION_KEYS`, set
   `ENCRYPTION_PRIMARY_KEY_VERSION` to it. New writes use it; old rows still decrypt (old key
   retained).
2. Batch job (out of scope here; an operational script) walks providers, `reencrypt_secret`
   each, persisting the new `(ciphertext, key_version)`.
3. Once no row references the old version, remove it from `ENCRYPTION_KEYS` (retire).

## Acceptance (unit, no infra)

- `decrypt_secret(*encrypt_secret(p)) == p` round-trip for arbitrary strings incl. unicode.
- `encrypt_secret` twice on the same plaintext yields different ciphertext (Fernet IV) but
  both decrypt back.
- `decrypt_secret` with an unknown `key_version` raises `SecretDecryptionError` (never returns
  a value).
- `reencrypt_secret` on a v1 token returns a `key_version == primary` token that decrypts to
  the original plaintext; a two-key ring proves decrypt-old-while-writing-new.
- No code path logs or returns the plaintext (grep guard in the router test).
