---
description: "Dependency-ordered tasks for Auth, API Keys & Workspace Isolation"
---

# Tasks: Auth, API Keys & Workspace Isolation

**Feature dir**: `specs/003-auth-api-keys-workspace-isolation/`

**Input**: plan.md, spec.md (US1–US4, FR-001…FR-024 + FR-020a, SC-001…SC-009), research.md (D1–D10), data-model.md, quickstart.md, contracts/ (11 files)

**Tests**: This feature explicitly requests tests (spec Clarifications + plan/quickstart split them into DB/Redis-**independent** unit tests that run HERE and live-DB/Redis **deferred** tests). Tests are therefore included and are first-class tasks.

**Environment reality**: Docker daemon is NOT running — no live Postgres/Redis. Every task marked `⏸ DEFERRED (needs live Postgres/Redis)` is **authored** here but left unchecked (`- [ ]`) and validated on a Postgres/Redis-capable host. All other tasks are fully completable and checkable in this environment.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task).
- **[Story]**: `[US1]`/`[US2]`/`[US3]`/`[US4]` — user story the task serves (Setup/Foundational/Seed/Polish carry no story label).
- Every task names an exact absolute-from-repo-root file path.

---

## Scope Boundary (READ FIRST)

**IN scope** — this feature creates ONLY:

- The **4 identity tables**: `workspaces` (tenant root, no RLS), `users` (workspace-owned, RLS, **nullable** `workspace_id`), `refresh_tokens` (user-owned, no RLS), `api_keys` (workspace-owned, RLS).
- The **auth endpoints** (`POST /v1/auth/login|refresh|logout`) and **api-key endpoints** (`POST/GET /v1/api-keys`, `DELETE /v1/api-keys/{id}`) — nothing else under `/v1`.
- The **isolation tooling**: `emit_rls_policy` applied in the creating migration, `set_workspace_context` transaction helper, the two-role BYPASSRLS pre-auth lookup path, workspace-scoped repository helpers, the AST CI guard, and cross-workspace tests.
- The framework-agnostic `app_shared/security/*` primitives (passwords, tokens, api-keys, jwt, scopes, rate-limit, status-cache, last-used) and the `apps/api` FastAPI auth dependency + routers.
- Config additions, `.env.example`, and the bootstrap seed script.

**OUT of scope** (do NOT create — SPEC-04+): products / variants / competitors / matches / scrape-profiles / access-policies tables or ORM models; any of their endpoints; `schemas/`, `pagination.py`; a public self-service signup endpoint; Fernet/webhook encryption; async DB paths. `workspaces.default_scrape_profile_id` / `default_access_policy_id` exist as **plain nullable UUID columns with NO foreign key** (targets land in later specs). `app_shared` MUST NOT import `fastapi`/`scrapy`/`twisted`/`playwright` — the FastAPI dependency + routers live only in `apps/api`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Pin new dependencies, extend configuration/enums, and create the security package skeleton so every later module has its inputs.

- [X] T001 [P] Add pinned deps `argon2-cffi>=23.1,<24`, `pyjwt>=2.9,<3`, `redis>=5.0,<6` to `libs/shared/pyproject.toml` `[project].dependencies` (framework-agnostic crypto/jwt/redis; NOT scrapy/twisted/playwright/fastapi). `apps/api` needs no new direct dep (it already has `fastapi`; it reaches redis/argon2/pyjwt via `app_shared`).
- [X] T002 Run `uv lock` at repo root so `uv.lock` resolves the three new deps (depends: T001).
- [X] T003 [P] Extend `Settings` in `libs/shared/app_shared/config.py` (per research D9): `JWT_SECRET: str` (**required**, fail-fast), `JWT_ALGORITHM: str = "HS256"`, `ACCESS_TOKEN_TTL_SECONDS: int = 900`, `REFRESH_TOKEN_TTL_SECONDS: int = 2592000`, `STATUS_CACHE_TTL_SECONDS: int = 30`, `LOGIN_RATE_LIMIT_MAX_ATTEMPTS: int = 5`, `LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = 60`, `API_KEY_LAST_USED_THROTTLE_SECONDS: int = 60`, `AUTH_DATABASE_URL: str | None = None` (BYPASSRLS auth-role URL), `ARGON2_TIME_COST`/`ARGON2_MEMORY_COST`/`ARGON2_PARALLELISM: int | None = None`.
- [X] T004 [P] Add the new variables (JWT_SECRET placeholder, all TTL/rate-limit/throttle defaults, AUTH_DATABASE_URL example for the `crawmatic_auth` BYPASSRLS role, ARGON2_* commented) to `.env.example` with safe local-only placeholders and comments.
- [X] T005 [P] Extend `libs/shared/app_shared/enums.py`: add `WorkspaceStatus` (`ACTIVE="active"`, `SUSPENDED="suspended"`), `UserRole` (`SUPER_ADMIN="super_admin"`, `WORKSPACE_ADMIN="workspace_admin"`, `READ_ONLY="read_only"`), `UserStatus` (`ACTIVE`/`SUSPENDED`), `ApiKeyStatus` (`ACTIVE="active"`, `REVOKED="revoked"`) — all extending the existing `StrEnum` (string-backed, app-validated via `enum_column`).
- [X] T006 [P] Create the security package marker `libs/shared/app_shared/security/__init__.py` (empty/exports only; keeps `app_shared` fastapi-free).

**Checkpoint**: deps resolve, config/enums extended, `security/` package exists.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The identity ORM models, their migration + RLS, the transaction/auth-session DB helpers, the workspace-scoped repository helpers, and the AST CI guard — the shared substrate every user story builds on.

**⚠️ CRITICAL**: No user-story work can begin until this phase is complete.

- [X] T007 Create `libs/shared/app_shared/models/identity.py` exactly per data-model.md: `Workspace(Base, TimestampMixin)` (name, slug UNIQUE `uq_workspaces_slug`, `status=enum_column(WorkspaceStatus)`, `default_scrape_profile_id`/`default_access_policy_id` nullable `Uuid` **NO FK**); `User(Base, TimestampMixin)` with its **own nullable** `workspace_id: Mapped[uuid.UUID|None]` + FK `fk_users_workspace_id_workspaces` + index (NOT `WorkspaceScopedBase`, whose column is NOT NULL), `email` Text UNIQUE `uq_users_email`, `password_hash` Text, `role=enum_column(UserRole)`, `status=enum_column(UserStatus)`; `RefreshToken(Base)` with `user_id` FK+index, `token_hash` Text UNIQUE/indexed, `expires_at`/`revoked_at`(nullable) `TZDateTime`, `created_at` `TZDateTime` declared directly (no `updated_at`); `ApiKey(Base, WorkspaceScopedBase, TimestampMixin)` adding FK `fk_api_keys_workspace_id_workspaces` via `__table_args__`, `name`, `key_prefix` Text indexed `ix_api_keys_key_prefix`, `key_hash` Text, `scopes` JSONB, `status=enum_column(ApiKeyStatus)`, `last_used_at`/`revoked_at` nullable `TZDateTime` (depends: T005).
- [X] T008 Re-export `Workspace`, `User`, `RefreshToken`, `ApiKey` from `libs/shared/app_shared/models/__init__.py` (add `from app_shared.models import identity` import + names to `__all__`) so `Base.metadata` sees the four tables for Alembic render (depends: T007).
- [X] T009 [P] Extend `libs/shared/app_shared/database.py`: add `set_workspace_context(session, workspace_id)` executing `text("SELECT set_config('app.workspace_id', :wsid, true)")` (bound param, pooler-safe); add `get_auth_session()` context manager bound to a lazily-built engine on `Settings.AUTH_DATABASE_URL` for pre-context credential lookups only (depends: T003). **[analyze C1] Do NOT silently fall back to `DATABASE_URL`:** under FORCE ROW LEVEL SECURITY (which `emit_rls_policy` sets on `users`/`api_keys`) a non-BYPASSRLS role with no workspace context returns ZERO rows for the pre-auth user-by-email / api-key-by-prefix lookup, which would make login and API-key auth *silently fail closed*. Therefore: if `AUTH_DATABASE_URL` is unset, `get_auth_session()` MUST fail fast with a clear, actionable error (e.g. "AUTH_DATABASE_URL (crawmatic_auth BYPASSRLS role) is required for authentication; pre-auth credential lookups return 0 rows without it under forced RLS") rather than returning a session that silently authenticates nobody. A test asserts this fail-fast (unit; no DB needed — assert the error is raised when the setting is None).
- [X] T010 Create `libs/shared/app_shared/repository.py` (per repository-scoping.md): `WORKSPACE_OWNED_MODELS: frozenset = {User, ApiKey}`, `scoped_select(model, workspace_id)` → `select(model).where(model.workspace_id == workspace_id)`, `scoped_get(session, model, id_, workspace_id)` filtered by BOTH id and workspace_id, `assert_workspace_owned_query_is_scoped(...)` raising `ValueError` when a workspace-owned model is queried without a `workspace_id` (depends: T008).
- [X] T011 [P] Create `scripts/check_workspace_scoping.py` (per ci-scoping-guard.md, stdlib `ast` only): scan `apps/` + `libs/` `.py`, exit non-zero on `<x>.get(User|ApiKey, ...)` and on `select(User|ApiKey)`/`<x>.query(User|ApiKey)` lacking a `workspace_id` predicate in the same chain; import the guarded set from `app_shared.repository.WORKSPACE_OWNED_MODELS`; path-allowlist `libs/shared/app_shared/repository.py` + test files; honor `# noqa: workspace-scope` (depends: T010).
- [X] T012 Create the Alembic revision `alembic/versions/<rev>_auth_identity_tables.py` (per migration-identity.md): `down_revision = "023a24e5717d"`; hand-authored `upgrade()` creating `workspaces`, `users`, `refresh_tokens`, `api_keys` reproducing the T007 ORM shapes + NAMING_CONVENTION; then `from app_shared.models import emit_rls_policy` and `op.execute` each of `emit_rls_policy("users")` + `emit_rls_policy("api_keys")` (ENABLE+FORCE+fail-closed NULLIF predicate) — RLS in the SAME migration; `downgrade()` drops `api_keys`, `refresh_tokens`, `users`, `workspaces` in FK-safe order. Include the two-role note (crawmatic_auth BYPASSRLS / crawmatic_app) as a comment (roles are a cluster ops step, not migration DDL) (depends: T008).
- [X] T013 [P] Extend `tests/unit/test_import_boundaries.py` to also import `app_shared.models.identity` and `app_shared.security` (and the primitive submodules as they land) in the fresh-subprocess check and assert none of `fastapi`/`scrapy`/`twisted`/`playwright` leak into `sys.modules` (depends: T006, T007).

**Checkpoint**: models registered, migration + RLS authored, isolation helpers + guard exist — user stories can proceed.

---

## Phase 3: User Story 1 — Sign in and stay signed in securely (Priority: P1) 🎯 MVP

**Goal**: Human email+password login issuing a short-lived access JWT + rotating opaque refresh token; refresh rotation is atomic and single-use; logout revokes; login is rate-limited and returns a uniform, factor-agnostic error with timing-uniform (dummy-verify) failure.

**Independent Test**: Valid login → access+refresh pair; exchange refresh → new pair and old refresh rejected; logout → refresh revoked; repeated bad logins throttled; unknown-email and wrong-password errors are byte-identical.

### Security primitives (framework-agnostic — `app_shared/security/`)

- [X] T014 [P] [US1] Create `libs/shared/app_shared/security/passwords.py` (per security-passwords.md): `hash_password` / `verify_password` (returns bool, never raises) / `needs_rehash` on `argon2.PasswordHasher` seeded from `Settings.ARGON2_*`; expose a module-level dummy hash for the unknown-email timing-uniform path (FR-005/FR-006).
- [X] T015 [P] [US1] Create `libs/shared/app_shared/security/tokens.py` (per security-tokens.md): `generate_refresh_token() -> (raw, sha256_hash)`, `hash_token(raw)`; document the atomic rotation SQL (`UPDATE refresh_tokens SET revoked_at=now() WHERE token_hash=:h AND revoked_at IS NULL AND expires_at>now() RETURNING id, user_id`) as the caller contract (FR-008/FR-009/FR-010/FR-011).
- [X] T016 [P] [US1] Create `libs/shared/app_shared/security/jwt.py` (per security-jwt.md): `encode_access_token(...)` / `decode_access_token(...)` (PyJWT, HS256, verify signature+exp), claims `sub, workspace_id(nullable), role, scopes?, type="access", iat, exp, jti` (FR-024).
- [X] T017 [P] [US1] Create `libs/shared/app_shared/security/rate_limit.py` (per security-cache.md): `check_and_increment_login(redis, *, email, source_ip, max_attempts, window_seconds)` — INCR+EXPIRE per-account `rl:login:acct:{sha256(email)}` AND per-source `rl:login:src:{ip}`, progressive-backoff lock key, refuse if either over threshold, **fail-safe deny** on any redis error, no factor disclosure in result (FR-007/SC-009).

### API layer (`apps/api`)

- [X] T018 [P] [US1] Create `apps/api/app/errors.py`: the uniform auth error builder `{ "error": { "code": "AUTH_FAILED", "message": "Authentication failed." } }` (HTTP 401) and structured `RATE_LIMITED` (429) helpers, so every failure path emits an identical, factor-agnostic body (FR-006/SC-001).
- [X] T019 [US1] Create `apps/api/app/routers/__init__.py` and `apps/api/app/routers/auth.py` with `POST /v1/auth/login` (rate-limit gate first → look up user by email via `get_auth_session` → `verify_password` always, dummy-verify on unknown email → check user/workspace status → issue access JWT + persist refresh `token_hash`), `POST /v1/auth/refresh` (atomic `UPDATE…RETURNING` rotation → new pair, else uniform 401), `POST /v1/auth/logout` (revoke by `token_hash`, idempotent 204) — all failures via `errors.py` (depends: T009, T014, T015, T016, T017, T018).
- [X] T020 [US1] Register the auth router in `apps/api/app/main.py` (`app.include_router(auth.router)`) (depends: T019).

### Tests for User Story 1

Independent (run HERE):

- [X] T021 [P] [US1] `tests/unit/test_passwords.py`: `hash_password(x) != x`; two hashes of same input differ (random salt); verify round-trip True; wrong password False; `needs_rehash` False on fresh hash (FR-005).
- [X] T022 [P] [US1] `tests/unit/test_refresh_tokens.py`: `generate_refresh_token` entropy/length; `hash_token` deterministic sha256; rotation predicate logic (rotated/expired/revoked → rejected) against an in-memory stand-in (FR-008/FR-009/FR-011).
- [X] T023 [P] [US1] `tests/unit/test_jwt.py`: encode→decode round-trips all claims; expired token → decode raises; wrong-secret token → decode raises; `type == "access"` (FR-024).
- [X] T024 [P] [US1] `tests/unit/test_uniform_login_error.py`: the login error builder produces byte-identical bodies/status for unknown-email vs wrong-password (FR-006/SC-001).
- [X] T025 [P] [US1] `tests/unit/test_rate_limit.py`: with a fake/in-memory redis, refuses over `max_attempts` (per-account and per-source independently), and **fail-safe denies** on an injected client error (FR-007/SC-009).

Deferred (authored, unchecked):

- [ ] T026 [US1] `tests/integration/test_auth_flow.py` — ⏸ DEFERRED (needs live Postgres/Redis): login → refresh (rotate) → reuse rejected → 2 concurrent rotations exactly one wins → logout revokes (FR-006/FR-009/FR-010/FR-011/SC-001/SC-002/SC-003).
- [ ] T027 [US1] `tests/integration/test_rate_limit.py` — ⏸ DEFERRED (needs live Redis): per-account + per-source backoff engages after threshold; fail-safe deny when the noeviction instance is down (FR-007/SC-009).

**Checkpoint**: human login/refresh/logout works end-to-end (unit-validated here; live flow deferred).

---

## Phase 4: User Story 2 — Authenticate machine clients with scoped API keys (Priority: P1)

**Goal**: A WORKSPACE_ADMIN issues a scoped API key whose full secret is shown once; machine clients authenticate with it (prefix lookup + full-hash verify, collision-safe), confined to the key's scopes and workspace; list hides the secret; revoke kills it; `last_used_at` is Redis-throttled to ≤1 write/key/min.

**Independent Test**: Create key with a scope subset → secret returned once; authenticate with it → limited to scopes; list → no secret; revoke → subsequent auth fails.

### Security primitives (framework-agnostic — `app_shared/security/`)

- [X] T028 [P] [US2] Create `libs/shared/app_shared/security/api_keys.py` (per security-tokens.md): `API_KEY_PREFIX="ck_"`, `generate_api_key() -> (full_secret, key_prefix, key_hash)` (`secrets.token_urlsafe(32)`), `hash_api_key` (sha256 hex, NOT a KDF), `verify_api_key` (`hmac.compare_digest`), `parse_prefix` — prefix-collision safe (FR-012/FR-016).
- [X] T029 [P] [US2] Create `libs/shared/app_shared/security/scopes.py` (per security-scopes.md): `Scope(StrEnum)` full 14-value vocabulary (`products:read`…`webhooks:write`), `validate_scopes(values)` (raises `ValueError` on unknown), `has_scopes(granted, required)` (FR-013).
- [X] T030 [P] [US2] Create `libs/shared/app_shared/security/last_used.py` (per security-cache.md): `should_write_last_used(redis, *, key_id, throttle_seconds)` using `SET apikey:lastused:{key_id} 1 NX EX throttle_seconds` → `True` only when the gate was absent, else `False`; **fail-safe `False`** on redis error (best-effort, never blocks/duplicates) (FR-015/SC-008).

### API layer (`apps/api`)

- [X] T031 [US2] Create `apps/api/app/routers/api_keys.py` (per api-keys.md): `POST /v1/api-keys` (validate scopes → `generate_api_key` → persist `key_prefix`/`key_hash`/`scopes`/`status=active`/`workspace_id=context` → return full secret **once**, 201), `GET /v1/api-keys` (workspace-scoped list via `scoped_select`, **never** the secret/`key_hash`; api-keys are low-volume per workspace so cursor pagination per §24 is deferred to the high-volume resource list endpoints in SPEC-04 — [analyze P1]), `DELETE /v1/api-keys/{id}` (scoped update `status=revoked`,`revoked_at=now()`, idempotent 204) — all guarded by `require_role(WORKSPACE_ADMIN, SUPER_ADMIN)` and operating under the request workspace context (depends: T010, T028, T029, and the auth dependency T038 from Phase 5 for `require_role` — see Dependencies note).
- [X] T032 [US2] Register the api-keys router in `apps/api/app/main.py` (`app.include_router(api_keys.router)`) (depends: T031).

### Tests for User Story 2

Independent (run HERE):

- [X] T033 [P] [US2] `tests/unit/test_api_key_security.py`: generation entropy/length + `ck_` prefix; `parse_prefix` round-trip; `hash_api_key` deterministic sha256; `verify_api_key` True for match / False for mismatch; **two keys sharing a forced prefix verify only against their own hash** (FR-012/FR-016).
- [X] T034 [P] [US2] `tests/unit/test_scopes.py`: full vocabulary matches §22/data-model.md; `validate_scopes(["products:read"])` ok, `validate_scopes(["bogus:read"])` raises; `has_scopes(["a","b"],["a"]) is True`, `has_scopes(["a"],["a","b"]) is False` (FR-013).
- [X] T035 [P] [US2] `tests/unit/test_last_used.py`: with a fake redis, `should_write_last_used` returns `True` once then `False` within the window; returns `False` fail-safe on an injected error (FR-015/SC-008).

Deferred (authored, unchecked):

- [ ] T036 [US2] `tests/integration/test_api_key_flow.py` — ⏸ DEFERRED (needs live Postgres/Redis): create (secret shown once) → authenticate → list (no secret) → revoke → subsequent auth denied; `last_used_at` written ≤1/key/min under burst (FR-012/FR-014/FR-015/SC-004-revocation/SC-008). **[analyze I1] Note:** end-to-end *scope refusal* (out-of-scope → 403) has no scope-gated resource endpoint in SPEC-03 (api-key CRUD is role-gated administrative); scope enforcement is proven here at the dependency level (T034 unit on `has_scopes` + T045 on `require_scopes`) and exercised end-to-end when scope-gated resource endpoints land in SPEC-04. This deferred flow asserts the auth/revocation/last-used paths that DO have endpoints here.

**Checkpoint**: api-key issuance/serialization/scope logic validated here; live key auth + throttle deferred.

---

## Phase 5: User Story 3 — Every request is confined to exactly one workspace (Priority: P1)

**Goal**: Every authenticated request resolves exactly one workspace context and applies it (`set_config(...,true)`) for its transaction; workspace-owned access is guarded by app-layer scoped helpers AND fail-closed RLS; SUPER_ADMIN (nullable workspace) must assume an explicit, role-authorized workspace (not an RLS bypass); the pre-auth credential lookup's BYPASSRLS is confined to credential resolution; the CI guard blocks unscoped access.

**Independent Test**: Two populated workspaces → a ws-A request reads/writes 0 of ws-B's rows, including with the app filter omitted (RLS) and with no context set (fail closed, 0 rows); the CI guard rejects an unscoped fetch/select on a workspace-owned model.

### Implementation

- [X] T037 [P] [US3] Create `libs/shared/app_shared/security/status_cache.py` (per security-cache.md): `get_user_status` / `get_workspace_status` (keys `status:user:{id}` / `status:ws:{wsid}`, TTL `STATUS_CACHE_TTL_SECONDS`; hit → cached, miss → single DB read + repopulate → 0 per-request DB reads in steady state); `invalidate_user` / `invalidate_workspace`; **fail-safe deny** (treat as not-active) on redis error (FR-022).
- [X] T038 [US3] Create `apps/api/app/deps.py` (per workspace-context.md, research D5): the authentication dependency — extract `Authorization: Bearer`, `ck_`-prefixed → api-key path (`parse_prefix` → `get_auth_session` lookup → `verify_api_key` → `status=active` → fire `should_write_last_used`), else JWT path (`decode_access_token`); cached status check (`status_cache`, fail-safe deny → 401/403); resolve+authorize workspace (own `workspace_id`, or SUPER_ADMIN's explicit `X-Workspace-Id` role-authorized — assuming a workspace is NOT an RLS bypass); open the request txn + `set_workspace_context`; expose `require_scopes(*scopes)` (403 via `has_scopes`) and `require_role(*roles)` guards (depends: T009, T016, T028, T029, T030, T037).

### Tests for User Story 3

Independent (run HERE):

- [ ] T039 [P] [US3] `tests/unit/test_identity_models.py`: table names/columns match data-model.md; `users.workspace_id` is nullable (and NOT via `WorkspaceScopedBase`); `api_keys.workspace_id` NOT NULL; enum columns render as plain `VARCHAR` and coerce to strings; `refresh_tokens` has `created_at` but no `updated_at` (FR-002/FR-003).
- [ ] T040 [P] [US3] `tests/unit/test_rls_identity.py`: `emit_rls_policy("users")` and `emit_rls_policy("api_keys")` each render ENABLE + FORCE + the fail-closed `USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)` predicate (FR-004/FR-019).
- [ ] T041 [P] [US3] `tests/unit/test_repository_scoping.py`: `scoped_select(User, ws)` renders a `WHERE ... workspace_id = ...` clause; `scoped_get` raises `ValueError` when `workspace_id` is missing/None on a workspace-owned model; `WORKSPACE_OWNED_MODELS == {User, ApiKey}` (FR-018).
- [ ] T042 [P] [US3] `tests/unit/test_workspace_scoping_guard.py`: the guard flags a planted `session.get(User, id)` and a planted unscoped `select(ApiKey)` (exit non-zero) and passes a properly `scoped_select(...)` / `.where(User.workspace_id == ws)` snippet (exit 0) (FR-020/SC-006).
- [ ] T043 [P] [US3] `tests/unit/test_migration_offline_auth.py`: `alembic upgrade head --sql` (offline, no DB) renders the four `CREATE TABLE`s + the six RLS statements; `alembic heads` shows exactly one head (FR-004/FR-023).
- [ ] T044 [US3] Run `uv run python scripts/check_workspace_scoping.py` against the current tree and confirm exit 0; then confirm it exits non-zero on a planted-violation fixture (used by T042). This is the executable validation of SC-006 in this environment (FR-020/SC-006) (depends: T011, T042).
- [ ] T045 [P] [US3] `tests/unit/test_deps.py`: with fakes/monkeypatch (no live services) exercise the dependency wiring — JWT vs api-key branch selection; missing/expired token → 401; suspended cached status → deny; SUPER_ADMIN without `X-Workspace-Id` → rejected; non-super assuming another workspace → 403 (FR-017/FR-020a/FR-024).

Deferred (authored, unchecked):

- [ ] T046 [US3] `tests/integration/test_rls_cross_workspace.py` — ⏸ DEFERRED (needs live Postgres): with context set to ws-A, reads/writes affect 0 of ws-B's rows; an app-unscoped query still returns 0 of B's rows (RLS); no context set → 0 rows (fail closed); confirms the two-role path (auth role finds credential, app role blocked) (FR-019/FR-021/FR-020a/SC-005).

**Checkpoint**: isolation is structural and CI-enforced here; live cross-workspace denial deferred.

---

## Phase 6: User Story 4 — Suspending a workspace or user cuts off access promptly (Priority: P2)

**Goal**: A suspended workspace/user's credentials stop working within the status-cache TTL, with 0 per-request status DB reads in steady state.

**Independent Test**: Suspend a workspace → within the TTL its authenticated requests are rejected; steady-state requests perform no per-request status DB read.

- [ ] T047 [P] [US4] `tests/unit/test_status_cache.py`: with a fake redis, a cache hit returns the cached status with **no** DB read; a miss triggers a single DB read then repopulates with TTL; a redis error **fail-safe denies** (treated as not-active). Also assert `invalidate_user`/`invalidate_workspace` clear the keys for immediate propagation (FR-022/SC-007).

Deferred (authored, unchecked):

- [ ] T048 [US4] `tests/integration/test_status_cache.py` — ⏸ DEFERRED (needs live Postgres/Redis): suspend a workspace/user → its credentials rejected within `STATUS_CACHE_TTL_SECONDS`; under sustained authenticated load, 0 per-request status DB reads (served from cache) (FR-022/SC-007).

**Checkpoint**: suspension-propagation logic validated here; live TTL/zero-DB-read behavior deferred.

---

## Phase 7: Seed & Bootstrap (No public signup)

**Purpose**: The administrative bootstrap path for the first workspace + SUPER_ADMIN (spec Assumptions / research D6). No self-service signup endpoint.

- [ ] T049 [P] Create `scripts/seed_bootstrap.py`: idempotent, run via the **direct** privileged connection (`MIGRATION_DATABASE_URL`, bypasses RLS during bootstrap); reads `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD`, optional `BOOTSTRAP_WORKSPACE_NAME`/`_SLUG`; creates the first `workspaces` row (if absent) and a `SUPER_ADMIN` `users` row (`workspace_id=NULL`, argon2id-hashed password via `hash_password`) (depends: T007, T014).

Deferred (authored, unchecked):

- [ ] T050 `tests/integration/test_seed_bootstrap.py` — ⏸ DEFERRED (needs live Postgres): online `alembic upgrade head` creates the four tables with RLS enabled on `users`+`api_keys`; `seed_bootstrap.py` creates exactly one workspace + one SUPER_ADMIN and is idempotent on re-run (FR-004/FR-023) (depends: T012, T049).

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: CI wiring, full local validation, and documentation of the two-role/deferred setup.

- [ ] T051 [P] Wire `scripts/check_workspace_scoping.py` into CI alongside `scripts/check_single_head.sh` (add the step to the CI workflow / document the command in the repo's CI config), per ci-scoping-guard.md (FR-020/SC-006).
- [ ] T052 Run the full local validation gate (quickstart §A): `uv run pytest tests/unit -q`, `uv run python scripts/check_workspace_scoping.py`, `bash scripts/check_single_head.sh`, `uv run alembic upgrade head --sql | head -60` — confirm all green and the offline render shows the 4 tables + RLS (depends: all Phase 1–7 non-deferred tasks).
- [ ] T053 [P] Document in `quickstart.md` (or a short note) the deferred-run setup: create `crawmatic_app` (no BYPASSRLS) + `crawmatic_auth` (BYPASSRLS) roles, set `AUTH_DATABASE_URL`, run online migration + seed, then `uv run pytest tests/integration -q` — mapping which SC/FR each deferred test closes.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (P1)**: no dependencies.
- **Foundational (P2)**: depends on Setup — BLOCKS all user stories.
- **US1 (P3)**: depends on Foundational. Fully independent of US2/US3/US4 (login endpoints are unauthenticated). **This is the MVP.**
- **US2 (P4)**: depends on Foundational; its api-key **management router (T031/T032)** additionally depends on the auth dependency **T038 (US3)** for `require_role` — inherent to the feature (every protected endpoint shares the one auth seam). US2's unit-testable primitives (T028–T030, T033–T035) are fully independent.
- **US3 (P5)**: depends on Foundational + US1's `jwt.py` (T016) + US2's `api_keys.py`/`scopes.py`/`last_used.py` (T028–T030) — because `deps.py` (T038) unifies both credential paths. `status_cache.py` (T037) precedes `deps.py`.
- **US4 (P6)**: depends on `status_cache.py` (T037) + `deps.py` (T038).
- **Seed (P7)**: depends on models (T007) + `passwords.py` (T014).
- **Polish (P8)**: depends on all prior non-deferred tasks.

### Build order note (resolving the US2↔US3 seam)

Because `deps.py` (T038) must unify the JWT path (US1) and the api-key path (US2) and the status cache, the correct primitive-build order is: US1 primitives (T014–T017) → US2 primitives (T028–T030) → `status_cache` (T037) → `deps.py` (T038) → the api-keys **router** (T031/T032) → US3/US4 tests. The phase numbering is priority-ordered (US1–US4 all P1 except US4=P2); follow the explicit `depends:` notes for build sequencing.

### Parallel opportunities

- **Setup**: T001, T003, T004, T005, T006 are all `[P]` (distinct files); T002 (`uv lock`) waits on T001.
- **Foundational**: T009, T011, T013 are `[P]`; T007→T008→(T010, T012) chain on model definitions.
- **Security primitives** across US1/US2/US3 (T014, T015, T016, T017, T028, T029, T030, T037) are all `[P]` — distinct files under `app_shared/security/`.
- **All unit tests** (T021–T025, T033–T035, T039–T043, T045, T047) are `[P]` — distinct files under `tests/unit/`.

### Parallel example (security primitives)

```bash
# After Foundational, launch the framework-agnostic primitives together:
Task T014: app_shared/security/passwords.py
Task T015: app_shared/security/tokens.py
Task T016: app_shared/security/jwt.py
Task T017: app_shared/security/rate_limit.py
Task T028: app_shared/security/api_keys.py
Task T029: app_shared/security/scopes.py
Task T030: app_shared/security/last_used.py
Task T037: app_shared/security/status_cache.py
```

---

## Implementation Strategy

### MVP first (User Story 1)

1. Phase 1 Setup → Phase 2 Foundational → Phase 3 US1.
2. **STOP & VALIDATE**: `uv run pytest tests/unit -q` (passwords/tokens/jwt/uniform-error/rate-limit) + offline migration render + single-head + scoping guard. Human login/refresh/logout is the deliverable increment.

### Incremental delivery

1. Foundational → US1 (login) — MVP.
2. US2 (api keys) — machine auth.
3. US3 (workspace context + isolation enforcement) — wires `deps.py`, closes the isolation guarantees; run the CI guard (T044) and full unit suite.
4. US4 (suspension) — status-cache behavior.
5. Seed + Polish — bootstrap path, CI wiring, full local gate.
6. On a Postgres/Redis host: run the six deferred integration suites to close SC-002/SC-004/SC-005/SC-007/SC-008/SC-009 + live FR-006/010/011/012/014/015/019/021/022 and the online migration+seed.

---

## Requirements Coverage (FR → tasks)

| Requirement | Tasks |
|-------------|-------|
| FR-001 `workspaces` tenant root | T007, T012 |
| FR-002 `users` (nullable workspace_id) | T007, T012, T039 |
| FR-003 roles string-backed | T005, T039 |
| FR-004 users+api_keys RLS in creating migration | T012, T040, T043 |
| FR-005 argon2id password KDF + per-user salt | T014, T021 |
| FR-006 uniform login error + dummy-verify timing | T014, T018, T019, T024 |
| FR-007 login rate-limit per-account+source, non-evicting | T017, T019, T025, T027 |
| FR-008 refresh stored only as hash | T015, T022 |
| FR-009 rotate on exchange, reject rotated | T015, T019, T026 |
| FR-010 atomic rotation under concurrency | T019, T026 |
| FR-011 refresh expiry + logout revoke | T015, T019, T026 |
| FR-012 api-key sha256 hash + prefix, shown once | T028, T031, T033 |
| FR-013 scopes + enforcement | T029, T031, T034, T038 |
| FR-014 revocation kills the key | T031, T036 |
| FR-015 last_used throttle ≤1/key/min | T030, T035, T036 |
| FR-016 lookup by prefix then verify hash (collision-safe) | T028, T033, T038 |
| FR-017 one workspace context per request txn | T009, T038 |
| FR-018 workspace-scoped repository helpers | T010, T041 |
| FR-019 RLS deny cross-workspace + fail closed | T012, T040, T046 |
| FR-020 CI unscoped-query guard | T011, T042, T044, T051 |
| FR-020a pre-auth BYPASSRLS lookup confined | T009, T038, T045, T046 |
| FR-021 automated cross-workspace tests | T046 |
| FR-022 status cache, 0 per-request status DB read | T037, T038, T047, T048 |
| FR-023 `/v1` endpoints (login/refresh/logout + api-key CRUD) | T019, T020, T031, T032 |
| FR-024 short-lived access token with claims | T016, T038 |

## Success Criteria Coverage (SC → tasks)

| Success Criterion | Tasks | Live? |
|-------------------|-------|-------|
| SC-001 valid login pair / invalid uniform | T019, T024, T026 | partial (T026 deferred) |
| SC-002 refresh once; concurrent one wins | T019, T026 | deferred (T026) |
| SC-003 post-logout/revoke 0 auth | T019, T026, T036 | deferred (T026/T036) |
| SC-004 revoked key 0; scope-confined | T031, T036, T038 | deferred (T036) |
| SC-005 two-workspace 0 rows incl unscoped + no-context | T046 | deferred (T046) |
| SC-006 CI guard fails on 100% unscoped | T011, T042, T044 | runs HERE |
| SC-007 suspend within TTL; 0 per-request status DB reads | T037, T038, T048 | deferred (T048) |
| SC-008 last_used ≤1/key/min | T030, T035, T036 | deferred (T036) |
| SC-009 rate limit engages per-account+source, no disclosure | T017, T025, T027 | deferred (T027) |

---

## Notes

- **6 DEFERRED tasks** (`⏸ needs live Postgres/Redis`): T026 (auth flow — SC-001/002/003), T027 (rate limit — SC-009), T036 (api-key flow — SC-004/008), T046 (cross-workspace RLS — SC-005), T048 (status cache — SC-007), T050 (migration+seed — FR-004/023). All authored here, run on a PG/Redis host.
- `app_shared` stays framework-agnostic: no `fastapi`/`scrapy`/`twisted`/`playwright` (asserted by T013). FastAPI dependency + routers live only under `apps/api`.
- Commit after each task or logical group (orchestrator commits per step).
- `[P]` = different files, no incomplete dependency. `[US#]` maps a task to its user story for traceability.
