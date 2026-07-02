# Phase 1 Data Model: Auth, API Keys & Workspace Isolation

Four tables — the first **real** application tables in the system, and the first **workspace-owned** ones. All extend the SPEC-02 `Base` (UUIDv7 PK, shared `metadata`, naming convention) and, where they carry timestamps, `TimestampMixin` (`TIMESTAMPTZ` via `TZDateTime`). Enum-like columns use `app_shared.enums.enum_column` (string-backed, app-validated — never a Postgres ENUM). Money/pricing types do not appear here.

Source authority: `PROJECT_SPEC.md` §22 (table shapes), §32 (isolation), §33 (secrets), §24 (roles/API surface); constitution Principles II/VIII; spec FR-001…FR-024.

All items live in `libs/shared/app_shared` (`models/identity.py`, `enums.py`, `security/scopes.py`).

---

## Enums (string-backed, app-validated)

Added to `app_shared/enums.py` (extending the existing `StrEnum` base + `enum_column`):

- **`WorkspaceStatus`**: `ACTIVE = "active"`, `SUSPENDED = "suspended"`.
- **`UserRole`**: `SUPER_ADMIN = "super_admin"`, `WORKSPACE_ADMIN = "workspace_admin"`, `READ_ONLY = "read_only"` (FR-003, §33).
- **`UserStatus`**: `ACTIVE = "active"`, `SUSPENDED = "suspended"`.
- **`ApiKeyStatus`**: `ACTIVE = "active"`, `REVOKED = "revoked"`.

**`Scope`** (in `app_shared/security/scopes.py`, `StrEnum`) — the API-key capability vocabulary (§22):
`products:read`, `products:write`, `variants:read`, `variants:write`, `competitors:read`, `competitors:write`, `matches:read`, `matches:write`, `jobs:run`, `jobs:read`, `results:read`, `alerts:read`, `webhooks:read`, `webhooks:write`.

**Validation**: every enum value is coerced/validated at the ORM boundary (`enum_column` raises `ValueError` on an out-of-set value); `Scope` membership is validated when an API key is created (each requested scope must be a `Scope`). No DB-level `CHECK`/ENUM — rejection is deterministically application-layer (per SPEC-02 [analyze A2]).

---

## Entity: Workspace (tenant root — NOT workspace-scoped, NO RLS)

**`app_shared/models/identity.py` → `Workspace(Base, TimestampMixin)`**, table **`workspaces`**.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` PK | UUIDv7 (Base). This is the isolation boundary id referenced by every workspace-owned row. |
| `name` | `Text NOT NULL` | Human label. |
| `slug` | `Text NOT NULL` | **UNIQUE** (`uq_workspaces_slug`). URL/lookup handle. |
| `status` | `enum_column(WorkspaceStatus) NOT NULL` | `active`/`suspended`; drives the status cache (FR-022). |
| `default_scrape_profile_id` | `UUID NULL` | **No FK** — target table is a later spec (§22, spec Assumptions). Plain nullable id. |
| `default_access_policy_id` | `UUID NULL` | **No FK** — same. |
| `created_at`, `updated_at` | `TIMESTAMPTZ NOT NULL` | TimestampMixin. |

**Isolation model (FR-001/FR-004, §32)**: `workspaces` is the **tenant root** — it has no `workspace_id` and gets **no** `emit_rls_policy` call. It is not workspace-scoped by itself; access to workspace rows is governed at the application/authorization layer (a principal may read its own workspace; SUPER_ADMIN may enumerate). It uses plain `Base` (not `WorkspaceScopedBase`).

**Invariants**: `slug` unique; `status` ∈ WorkspaceStatus.

---

## Entity: User (workspace-owned, RLS — with NULLABLE workspace_id)

**`app_shared/models/identity.py` → `User(Base, TimestampMixin)`**, table **`users`**.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` PK | UUIDv7 (Base). |
| `workspace_id` | `UUID NULL` | **FK → workspaces.id** (`fk_users_workspace_id_workspaces`), **NULLABLE**: a cross-workspace `SUPER_ADMIN` has no home workspace (FR-002, §22). Indexed. |
| `email` | `Text NOT NULL` | **UNIQUE** globally (`uq_users_email`) — login handle; also the rate-limit account key (hashed). |
| `password_hash` | `Text NOT NULL` | argon2id encoded string (salt+params embedded); **never plaintext** (FR-005). |
| `role` | `enum_column(UserRole) NOT NULL` | SUPER_ADMIN/WORKSPACE_ADMIN/READ_ONLY (FR-003). |
| `status` | `enum_column(UserStatus) NOT NULL` | `active`/`suspended`; drives status cache (FR-022). |
| `created_at`, `updated_at` | `TIMESTAMPTZ NOT NULL` | TimestampMixin. |

**Isolation model (FR-004, §32, research D4)**: `users` **is workspace-owned** and gets `emit_rls_policy("users")` in its creating migration (ENABLE + FORCE + fail-closed `USING (workspace_id = NULLIF(current_setting('app.workspace_id', true),'')::uuid)`). Because `workspace_id` is nullable, this model **cannot** use `WorkspaceScopedBase` (whose column is `NOT NULL`); it declares its own nullable `workspace_id` + FK. The nullable column interacts with the policy **correctly and by design**: a NULL-workspace SUPER_ADMIN row never matches any workspace context (`NULL = x` → not true) → invisible to ordinary scoped access (fail-closed). SUPER_ADMIN acts by assuming an explicit, role-authorized workspace context per request; pre-auth login lookup (by unique `email`) runs via the BYPASSRLS auth role (research D4).

**Invariants**: `email` globally unique; `role`/`status` in their enums; `password_hash` present and non-plaintext; SUPER_ADMIN rows have `workspace_id IS NULL`, non-SUPER_ADMIN rows have `workspace_id` set (enforced in application/seed logic, not a DB constraint in v1).

**State transitions (status)**: `active → suspended` (offboarding/abuse) → credentials rejected within the status-cache TTL (FR-022/SC-007); `suspended → active` (reinstatement). No hard delete in scope.

---

## Entity: RefreshToken (user-owned, NO RLS)

**`app_shared/models/identity.py` → `RefreshToken(Base)`**, table **`refresh_tokens`**.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` PK | UUIDv7 (Base). |
| `user_id` | `UUID NOT NULL` | **FK → users.id** (`fk_refresh_tokens_user_id_users`). Indexed. |
| `token_hash` | `Text NOT NULL` | `sha256(raw_token)` — **raw value never persisted** (FR-008). **UNIQUE/indexed** for O(1) lookup on exchange. |
| `expires_at` | `TIMESTAMPTZ NOT NULL` | Absolute expiry (FR-011). |
| `revoked_at` | `TIMESTAMPTZ NULL` | Set on rotation or logout; the atomic-rotation guard column (FR-009/FR-010/FR-011). |
| `created_at` | `TIMESTAMPTZ NOT NULL` | TimestampMixin **not** used (no `updated_at` per §22 shape); `created_at` declared directly as `TZDateTime`. |

**Isolation model (FR-004, §32, research D4)**: **not** workspace-owned → **no** `emit_rls_policy`. Reasoning: a refresh token is reached only by its unforgeable `token_hash` and is tied to `user_id`; the owning user carries the workspace. There is no cross-workspace read surface — a caller can only present a token they physically hold, and lookup is by hash equality (not an enumerable/filterable workspace scan). Rotation is by the atomic `UPDATE ... WHERE token_hash = :h AND revoked_at IS NULL RETURNING` (research D3), which is safe without RLS because the `token_hash` predicate is the authorization.

**State transitions**: `active (revoked_at IS NULL, expires_at > now)` → **rotated/revoked** (`revoked_at = now()`, on refresh exchange or logout) → terminal (any later presentation returns zero rows → rejected). Expiry (`expires_at <= now`) is also terminal for exchange.

---

## Entity: ApiKey (workspace-owned, RLS)

**`app_shared/models/identity.py` → `ApiKey(Base, WorkspaceScopedBase, TimestampMixin)`**, table **`api_keys`**.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` PK | UUIDv7 (Base). |
| `workspace_id` | `UUID NOT NULL` | From `WorkspaceScopedBase` (indexed, NOT NULL). **FK → workspaces.id** added via `__table_args__` (`fk_api_keys_workspace_id_workspaces`) — SPEC-02's mixin intentionally omitted the FK until `workspaces` existed; this spec adds it. |
| `name` | `Text NOT NULL` | Admin-facing label. |
| `key_prefix` | `Text NOT NULL` | Short non-secret prefix (e.g. `ck_ab12cd`), **indexed** (`ix_api_keys_key_prefix`) for lookup; shown in listings (FR-012/FR-016). |
| `key_hash` | `Text NOT NULL` | `sha256(full_secret)` — the full secret is shown **once** at creation and never stored (FR-012). |
| `scopes` | `JSONB NOT NULL` | List of `Scope` strings; validated against the vocabulary on create (FR-013). |
| `status` | `enum_column(ApiKeyStatus) NOT NULL` | `active`/`revoked` (FR-014). |
| `last_used_at` | `TIMESTAMPTZ NULL` | Throttled to ≤1 write/key/min via Redis gate (FR-015/SC-008); never written per-request. |
| `created_at`, `updated_at` | `TIMESTAMPTZ NOT NULL` | TimestampMixin. |
| `revoked_at` | `TIMESTAMPTZ NULL` | Set on revocation. |

**Isolation model (FR-004, §32)**: **workspace-owned** → `emit_rls_policy("api_keys")` in the creating migration (ENABLE + FORCE + fail-closed predicate). Uses `WorkspaceScopedBase` (mandatory NOT NULL `workspace_id`). Authentication looks up a key **by `key_prefix`** (a pre-context lookup, run via the BYPASSRLS auth role — research D4), then verifies `hmac.compare_digest(sha256(presented_secret), key_hash)` so a **prefix collision cannot authenticate the wrong key** (FR-016). All subsequent workspace-owned access uses the resolved `workspace_id` + `set_config` context under FORCE RLS.

**Invariants**: `key_hash`/`key_prefix` present; `scopes` ⊆ `Scope` vocabulary; a `revoked`/`suspended-workspace` key authenticates **nothing** (status + cached workspace status checked, FR-014/spec Edge Case).

**State transitions (status)**: `active → revoked` (`status=revoked`, `revoked_at=now()`) → authenticates 0 requests thereafter (SC-004). No un-revoke.

---

## Relationships

```text
workspaces (tenant root, no RLS)
  ├── 1..* users        (users.workspace_id → workspaces.id, NULLABLE; RLS on users)
  │        └── 1..* refresh_tokens (refresh_tokens.user_id → users.id; no RLS)
  └── 1..* api_keys     (api_keys.workspace_id → workspaces.id, NOT NULL; RLS on api_keys)
```

- SUPER_ADMIN users sit *outside* the `workspaces → users` FK path (`workspace_id IS NULL`) yet the FK still permits NULL.
- `default_scrape_profile_id` / `default_access_policy_id` on `workspaces` are **danging ids** (no FK) until their target tables land in later specs.

---

## Isolation & RLS summary (FR-004/FR-017/FR-019, Principle II)

| Table | Workspace-owned? | RLS in creating migration | Base/mixin |
|-------|------------------|---------------------------|------------|
| `workspaces` | No (tenant root) | No | `Base` |
| `users` | Yes (nullable ws_id) | **Yes** — `emit_rls_policy("users")` | `Base` + own nullable `workspace_id`+FK |
| `refresh_tokens` | No (via owning user) | No | `Base` |
| `api_keys` | Yes | **Yes** — `emit_rls_policy("api_keys")` | `Base` + `WorkspaceScopedBase` |

- Context is set per request transaction with `set_config('app.workspace_id', :wsid, true)` (pooler-safe, FR-017).
- No workspace context set → RLS returns **zero rows** (fail closed, FR-019/SC-005).
- Application-layer defense: workspace-scoped repository helpers (`app_shared/repository.py`) require a `workspace_id` and forbid `session.get()`/unscoped `select()` on `User`/`ApiKey`; the AST CI guard (`scripts/check_workspace_scoping.py`) fails the build on any introduced unscoped access (FR-018/FR-020/SC-006).
