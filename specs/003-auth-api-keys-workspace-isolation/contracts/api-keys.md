# Contract: API-key endpoints (`/v1/api-keys`)

Router: `apps/api/app/routers/api_keys.py`. Base path `/v1` (§24). These endpoints are **human-administered** (a WORKSPACE_ADMIN or SUPER_ADMIN, authenticated via access JWT) and operate **within the caller's workspace context** (RLS-scoped). Management endpoints require role `WORKSPACE_ADMIN`+ (`require_role`).

## `POST /v1/api-keys` — create

- **Auth**: WORKSPACE_ADMIN or SUPER_ADMIN (assuming a workspace).
- **Request**: `{ "name": str, "scopes": [str, ...] }` — each scope MUST be in the `Scope` vocabulary (`security-scopes.md`); an unknown scope → `422`.
- **Success `201`**: `{ "id": uuid, "name": str, "key_prefix": str, "scopes": [...], "status": "active", "created_at": ts, "api_key": "<full secret — shown once>" }`
- **Behavior**: `generate_api_key()` → `(full_secret, key_prefix, key_hash)` (research D1 / `security-tokens.md`). Persist `key_prefix`, `key_hash`, `scopes`, `status=active`, `workspace_id = context`. Return the **full secret exactly once**; it is never retrievable again (FR-012/SC-004).
- **Guarantee**: only `key_prefix` + metadata are stored retrievably; the secret is never persisted or re-shown.

## `GET /v1/api-keys` — list

- **Auth**: WORKSPACE_ADMIN+.
- **Success `200`**: `{ "items": [ { "id", "name", "key_prefix", "scopes", "status", "last_used_at", "created_at", "revoked_at" }, ... ] }` — **never** the secret or `key_hash` (FR-012, spec US2-3).
- **Scoping**: results are workspace-scoped (RLS + scoped helper) — a caller sees only their workspace's keys.

## `DELETE /v1/api-keys/{id}` — revoke

- **Auth**: WORKSPACE_ADMIN+.
- **Success `204`** (or `200` with the updated record).
- **Behavior**: workspace-scoped update `status=revoked`, `revoked_at=now()` (scoped by `workspace_id` — a caller cannot revoke another workspace's key; RLS also blocks). Idempotent.
- **Guarantee (SC-004)**: after revocation the key authenticates 0 requests.

## Authenticating **with** an API key (machine clients)

Not an endpoint here but the auth path these keys drive (see `workspace-context.md`): a request presents the key in `Authorization: Bearer <ck_...>`. The dependency parses `key_prefix`, looks up the key via the BYPASSRLS auth path, verifies `hmac.compare_digest(sha256(secret), key_hash)` (prefix-collision safe, FR-016), checks `status=active` + cached workspace status, resolves the key's `workspace_id` as the request context, exposes its `scopes` for `require_scopes()`, and fires the Redis last-used throttle (≤1 write/key/min, FR-015).

- **Scope enforcement (FR-013/SC-004)**: an endpoint declaring `require_scopes("products:read")` refuses (`403`) a key lacking that scope.
- **Revoked / suspended-workspace key** authenticates **nothing** (within the status-cache TTL, spec Edge Case).

## Tests

- Unit: create/serialize shapes hide the secret on list; scope validation rejects unknown scopes.
- Live (`test_api_key_flow.py`): create (secret once) → authenticate → scope-limited (out-of-scope refused) → list (no secret) → revoke → subsequent auth fails; last-used throttle ≤1 write/key/min.
