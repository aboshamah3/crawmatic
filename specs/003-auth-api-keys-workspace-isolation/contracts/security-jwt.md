# Contract: Access-token JWT (`app_shared/security/jwt.py`)

PyJWT-backed, framework-agnostic (FR-024, §32/§35). No FastAPI/DB imports.

## Exposed symbols

```python
def encode_access_token(
    *, user_id: uuid.UUID, workspace_id: uuid.UUID | None,
    role: str, scopes: list[str] | None = None,
    secret: str, algorithm: str = "HS256", ttl_seconds: int,
) -> str: ...

def decode_access_token(token: str, *, secret: str, algorithm: str = "HS256") -> dict: ...
```

## Claims

`{ "sub": <user_id>, "workspace_id": <uuid|null>, "role": <role>, "scopes": [...]|omitted, "type": "access", "iat": <int>, "exp": <int>, "jti": <uuid> }`

- `workspace_id` may be **null** for a SUPER_ADMIN token not yet bound to a workspace (the request then supplies an explicit workspace, research D4/D5).
- User authorization is by `role`; `scopes` are primarily an API-key concept and optional here.

## Guarantees

- Signed with `secret`/`algorithm` from `Settings` (`JWT_SECRET`/`JWT_ALGORITHM`); default HS256.
- `decode_access_token` verifies signature **and** `exp` (PyJWT raises `ExpiredSignatureError` / `InvalidTokenError`), which the auth dependency maps to the uniform `401`. Resolving identity/workspace/role from a valid token needs **no DB read** beyond the cached status check (FR-024, hot-path goal §32/§35).

## Tests (unit, no DB)

- Encode→decode round-trips all claims.
- A token past `exp` → decode raises → rejected.
- A token signed with a different secret → decode raises → rejected.
- `type` claim is `"access"`.
