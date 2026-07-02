# Contract: Authentication endpoints (`/v1/auth`)

Router: `apps/api/app/routers/auth.py`. Base path `/v1` (§24). All bodies JSON. Failures use the **uniform auth error** (below) — no factor disclosure (FR-006).

## `POST /v1/auth/login`

Sign in with email + password.

- **Request**: `{ "email": str, "password": str }`
- **Success `200`**: `{ "access_token": str, "refresh_token": str, "token_type": "bearer", "expires_in": <ACCESS_TOKEN_TTL_SECONDS> }`
- **Behavior**:
  1. Rate-limit gate **before** any credential work: per-account (`sha256(email)`) AND per-source (client IP) counters; over threshold → `429` `RATE_LIMITED` with progressive backoff (FR-007/SC-009). Cache unavailable → fail-safe deny (`429`/`503`, never allow).
  2. Look up user by unique `email` via the BYPASSRLS auth path (research D4). Verify password with argon2id `verify_password`. **Always** perform a hash comparison (dummy hash when the email is unknown) so timing/response are uniform.
  3. On success: check `status == active` (and workspace status if bound); issue a signed access JWT (claims per `security-jwt.md`) + a fresh refresh token (persist only its `sha256`, `expires_at = now + REFRESH_TOKEN_TTL_SECONDS`).
  4. On any failure (unknown email, wrong password, suspended): return the **uniform `401`** (FR-006/SC-001) and count the attempt toward both rate-limit counters.
- **Guarantee (SC-001)**: valid login returns a pair 100% of the time; invalid returns the identical uniform error with no indication of which factor failed.

## `POST /v1/auth/refresh`

Exchange a refresh token for a new pair (rotation).

- **Request**: `{ "refresh_token": str }`
- **Success `200`**: same shape as login (new access + new refresh).
- **Behavior**: atomic rotation (research D3) —
  `UPDATE refresh_tokens SET revoked_at = now() WHERE token_hash = sha256(:presented) AND revoked_at IS NULL AND expires_at > now() RETURNING id, user_id`.
  One row → issue a new pair (insert new refresh row). Zero rows → **`401`** uniform (covers rotated-reuse FR-009, expired FR-011, revoked/logout FR-011, unknown).
- **Guarantees**: a refresh token works **exactly once** (SC-002); an already-rotated token is rejected 100% (FR-009); of two concurrent exchanges at most one succeeds (FR-010/SC-002).

## `POST /v1/auth/logout`

Revoke the current session's refresh token.

- **Request**: `{ "refresh_token": str }` (or the session's token).
- **Success `204`**: no body.
- **Behavior**: `UPDATE refresh_tokens SET revoked_at = now() WHERE token_hash = sha256(:presented) AND revoked_at IS NULL`. Idempotent (already-revoked → still `204`).
- **Guarantee (SC-003)**: after logout the token authenticates 0 subsequent requests.

## Uniform auth error

All authentication failures (login, refresh, expired/invalid access token) return the **same** structured body and status so no code path leaks which factor was wrong:

```json
{ "error": { "code": "AUTH_FAILED", "message": "Authentication failed." } }
```

- `401` for bad/expired/rotated credentials; `429` `RATE_LIMITED` for throttled login (no factor disclosure in the throttled response either — FR-006/SC-009). Structured error codes align with §34.

## Tests

- Unit (`test_uniform_login_error.py`): unknown-email and wrong-password produce byte-identical error responses.
- Live (`test_auth_flow.py`): login → refresh (rotate) → reuse rejected → concurrent rotate (one wins) → logout revokes.
