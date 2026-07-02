# Contract: Password hashing (`app_shared/security/passwords.py`)

Framework-agnostic argon2id primitives (FR-005, §33). Backed by `argon2-cffi` (`argon2.PasswordHasher`). No FastAPI/DB imports.

## Exposed symbols

```python
def hash_password(plaintext: str) -> str: ...
def verify_password(stored_hash: str, plaintext: str) -> bool: ...
def needs_rehash(stored_hash: str) -> bool: ...
```

## Guarantees

- `hash_password` returns an argon2id encoded string (`$argon2id$v=19$m=...,t=...,p=...$<salt>$<hash>`) with a **per-hash random salt** and parameters embedded — no separate salt column is needed (FR-005). Parameters come from `Settings.ARGON2_*` when set, else argon2-cffi's recommended defaults.
- The result is **never** the plaintext; plaintext is never logged (FR-005).
- `verify_password` is constant-time within argon2 and returns `False` (never raises) on mismatch, so callers get **uniform** behavior; unknown-user login paths compare against a dummy hash to keep timing uniform (see `api-auth.md`).
- `needs_rehash` reports whether a stored hash used weaker-than-current parameters, enabling transparent upgrade on next successful login.

## Tests (unit, no DB)

- `hash_password(x) != x`; two hashes of the same input differ (random salt).
- `verify_password(hash_password(x), x) is True`; `verify_password(hash_password(x), y) is False`.
- `needs_rehash` returns `False` for a freshly-created hash.
