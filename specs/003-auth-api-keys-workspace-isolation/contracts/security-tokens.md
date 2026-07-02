# Contract: API-key & refresh-token primitives (`app_shared/security/`)

Stdlib-only (`secrets`, `hashlib`, `hmac`) high-entropy generation + fast SHA-256 hashing (FR-008/FR-012/FR-016, §33). A password KDF MUST NOT be used here (FR-012). No FastAPI/DB imports.

## API keys — `app_shared/security/api_keys.py`

```python
API_KEY_PREFIX = "ck_"          # recognizable, routes the auth path to key-auth
def generate_api_key() -> tuple[str, str, str]:
    """Return (full_secret, key_prefix, key_hash)."""
def hash_api_key(full_secret: str) -> str:            # sha256 hex
def verify_api_key(full_secret: str, key_hash: str) -> bool:   # hmac.compare_digest
def parse_prefix(full_secret: str) -> str:            # extract the stored key_prefix
```

**Guarantees**:
- `full_secret` is high-entropy (`secrets.token_urlsafe(32)` → 256-bit) prefixed with `ck_`.
- `key_prefix` is a **short non-secret** slice (e.g. `ck_` + first 6 chars) stored/displayed for lookup (FR-012/FR-016).
- `key_hash = sha256(full_secret)` (fast hash, **not** a KDF, FR-012).
- `verify_api_key` uses `hmac.compare_digest` (constant-time). **Prefix-collision safety (FR-016)**: two keys may share a `key_prefix`; authentication resolves by matching the full-secret hash, so a colliding prefix cannot authenticate the wrong key.

## Refresh tokens — `app_shared/security/tokens.py`

```python
def generate_refresh_token() -> tuple[str, str]:   # (raw, token_hash=sha256(raw))
def hash_token(raw: str) -> str:                   # sha256 hex
```

**Guarantees**:
- `raw` is high-entropy (`secrets.token_urlsafe(32)`), returned to the client; **only** `token_hash` is ever persisted (FR-008).
- Rotation is performed by the caller with the atomic SQL in research D3 / `api-auth.md` (`UPDATE ... WHERE token_hash = :h AND revoked_at IS NULL AND expires_at > now() RETURNING`). This module provides the hashing; the atomicity lives in the single SQL statement (no session locks → pooler-safe, FR-010).

## Tests (unit, no DB)

- API key: prefix present + parseable; `hash_api_key` deterministic sha256; `verify_api_key` true for match / false for mismatch; two keys sharing a forced prefix verify only against their own hash.
- Refresh: `raw` unpredictable/length; `hash_token` deterministic; the rotation predicate logic (rotated/expired/revoked → rejected) exercised as pure logic against an in-memory stand-in.
