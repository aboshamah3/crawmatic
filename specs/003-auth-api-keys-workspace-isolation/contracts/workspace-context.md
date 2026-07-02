# Contract: Workspace context — helper + FastAPI dependency

The per-request isolation seam (FR-017/FR-024, Principle II). The **transaction helper** is framework-agnostic (`app_shared`); the **dependency** is FastAPI (`apps/api`).

## `app_shared/database.py` — `set_workspace_context`

```python
def set_workspace_context(session: Session, workspace_id: uuid.UUID) -> None:
    session.execute(
        text("SELECT set_config('app.workspace_id', :wsid, true)"),
        {"wsid": str(workspace_id)},
    )
```

- `set_config(name, value, is_local=true)` = **bind-parameterizable** `SET LOCAL` → **transaction-scoped** (correct under PgBouncer transaction pooling) and injection-safe (UUID is a bound param, not interpolated).
- MUST be called at the start of the request transaction, **before any workspace-owned query**, so RLS (`emit_rls_policy` predicate) sees the right workspace. Composes with SPEC-02 `get_session()`/engine (the app role via `DATABASE_URL`).

## `app_shared/database.py` — `get_auth_session` (pre-context lookups)

```python
def get_auth_session() -> ContextManager[Session]: ...   # bound to AUTH_DATABASE_URL (BYPASSRLS auth role)
```

- Used **only** by the authentication repository for the fixed credential lookups that precede any workspace context: find-user-by-email (login), find-api-key-by-prefix (API-key auth). Falls back to `DATABASE_URL` when `AUTH_DATABASE_URL` is unset (single-role dev; documented caveat in research D4/D9). Never used for arbitrary/user-supplied filters.

## `apps/api/app/deps.py` — the authentication dependency

Resolves context per request (research D5), in order:

1. **Extract credential** from `Authorization: Bearer <...>`. A `ck_`-prefixed value → API-key path; otherwise → JWT path.
2. **JWT path**: `decode_access_token` (verify signature+exp) → `sub`, `workspace_id`, `role`.
   **API-key path**: `parse_prefix` → look up via `get_auth_session` → `verify_api_key` (prefix-collision safe) → `status=active` → `workspace_id`, `scopes`; fire `should_write_last_used` throttle.
3. **Cached status check**: `get_user_status` / `get_workspace_status` (Redis) → suspended/unavailable ⇒ **fail-safe deny** (`401`/`403`). No per-request status DB read in steady state (FR-022).
4. **Resolve + authorize workspace**: normal principal → own `workspace_id`; SUPER_ADMIN (JWT `workspace_id` null) → explicit `X-Workspace-Id`, **role-authorized** (SUPER_ADMIN may assume any; others only their own — research D4). Assuming a workspace is **not** an RLS bypass.
5. Open the request-scoped session/txn, call `set_workspace_context(session, wsid)`, and yield `(session, principal)`.

**Authorization guards** (also in `deps.py`):
- `require_scopes(*scopes)` — API-key requests; `has_scopes` else `403` (FR-013).
- `require_role(*roles)` — human endpoints (e.g. api-key management is WORKSPACE_ADMIN+).

## Guarantees

- Exactly one workspace context is resolved per request and applied for the whole request transaction (FR-017/SC-005 AS-US3-1).
- No context set on a transaction touching a workspace-owned table → RLS returns zero rows (fail closed, FR-019).

## Tests

- Unit: dependency wiring with fakes (JWT vs api-key branch; missing/expired token → 401; suspended status → deny; SUPER_ADMIN without `X-Workspace-Id` → rejected; non-super assuming another workspace → 403).
- Live (`test_rls_cross_workspace.py`): with context set to ws-A, queries return 0 of ws-B's rows; unset context → 0 rows.
