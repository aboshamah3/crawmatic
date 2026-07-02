# Implementation Plan: Auth, API Keys & Workspace Isolation

**Branch**: `003-auth-api-keys-workspace-isolation` | **Date**: 2026-07-02 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/003-auth-api-keys-workspace-isolation/spec.md`

## Summary

Deliver the secure API foundation: the first **real workspace-owned tables** (`users`, `api_keys`) plus the tenant root (`workspaces`) and per-user `refresh_tokens`, the human login/refresh/logout flows, scoped API-key issuance and authentication, the per-request **workspace context**, and — most importantly — the **structural multi-tenant isolation** every later workspace-owned table inherits: fail-closed row-level security applied in the creating migration (via SPEC-02 `emit_rls_policy`), transaction-scoped `SET LOCAL app.workspace_id`, workspace-scoped repository helpers that forbid unscoped access, a CI guard that fails the build on any unscoped fetch/select of a workspace-owned model, and cross-workspace tests.

Concretely, this feature extends `app_shared` with: identity ORM models (`models/identity.py`), string-backed auth enums (roles/statuses), a `security/` package of framework-agnostic primitives (argon2id passwords, SHA-256 API-key and refresh-token hashing + high-entropy generation, PyJWT access tokens, the scope vocabulary, and Redis-backed rate-limit / status-cache / last-used-throttle helpers), workspace-scoped query helpers + a `set_workspace_context()` transaction helper, new `Settings` fields (JWT secret/TTLs, cache TTLs, rate-limit tunables, optional auth-role URL), the Alembic migration creating the four tables and enabling RLS on `users`+`api_keys`, and a bootstrap seed script. `apps/api` gains the FastAPI authentication dependency, the workspace-context request wiring, and the `/v1` auth + api-key routers. A `scripts/check_workspace_scoping.py` AST guard enforces isolation in CI.

Everything DB/Redis-independent (password + token hashing and rotation logic, API-key generation/hash/scope checks, JWT encode/decode, RLS DDL render, scoped-helper rejection, the CI guard itself, scope/enum validation, uniform-login-error shape) is fully unit-tested in this environment. Live items (RLS row denial, cross-workspace blocking, rate-limit/status-TTL/last-used behavior, migration online run, full request integration) are authored and marked for a PostgreSQL/Redis-capable host — no Docker daemon / no live services here.

## Technical Context

**Language/Version**: Python 3.13 (`requires-python = ">=3.13,<3.14"`; uv workspace).

**Primary Dependencies**:
- Existing: SQLAlchemy 2.0 (sync), psycopg 3, Alembic, pydantic-settings, uuid6, FastAPI (`apps/api`).
- **New (pinned)**: `argon2-cffi>=23.1,<24` (argon2id password hashing), `pyjwt>=2.9,<3` (access-token encode/decode), `redis>=5.0,<6` (redis-py: rate-limit / status cache / last-used throttle). Stdlib `secrets` + `hashlib` for API-key/refresh-token generation and SHA-256 hashing (no third-party lib).
- `app_shared` MUST NOT import scrapy/twisted/playwright (unchanged) and MUST NOT import FastAPI (framework-agnostic). The FastAPI dependency + routers live in `apps/api`.

**Storage**: PostgreSQL 17. App requests connect through PgBouncer (`pgbouncer:6432`, transaction pooling); the migration job / seed connect **directly** to `postgres:5432` (`MIGRATION_DATABASE_URL`). Workspace context is set per-transaction with `SELECT set_config('app.workspace_id', :wsid, true)` (transaction-local, bind-parameterizable, pooler-safe). Correctness-critical Redis keys (rate-limit, status, last-used) live on the **noeviction** instance.

**Testing**: pytest. DB/Redis-independent logic unit-tested here; live-DB/Redis items authored and skipped when no reachable Postgres (`MIGRATION_DATABASE_URL`) / Redis is present.

**Target Platform**: Linux server / containers (compose locally, Railway-style platform in prod). Only `apps/api` is publicly exposed.

**Project Type**: Backend monorepo (uv workspace). This feature spans the `app_shared` library (models, security, scoping) and `apps/api` (FastAPI dependency + routers), plus repo-root Alembic and CI scripts.

**Performance Goals**: Authentication is a hot path. Design target: **zero per-request DB read** for status (served from a short-TTL Redis cache, FR-022/SC-007) and **zero per-request DB write** for API-key usage (Redis-throttled `last_used_at`, ≤1 write/key/min, FR-015/SC-008). Access tokens are stateless JWTs carrying identity+workspace+role/scope claims so context/authorization resolve without a DB read beyond the cached status check (FR-024).

**Constraints**: Transaction-pooling-safe only — no server-side prepared statements (already `prepare_threshold=None`), only `SET LOCAL` / `set_config(...,true)` / row-level `UPDATE...RETURNING` (no session advisory locks). Refresh rotation must be atomic under concurrency and survive PgBouncer transaction pooling. RLS fails closed (zero rows) when no workspace context is set. No live Postgres/Redis in this build env.

**Scale/Scope**: Foundation for 2,000 products / 10k–20k matches per workspace (§39), many workspaces. This spec adds exactly **4 tables** (`workspaces`, `users`, `refresh_tokens`, `api_keys`), the auth/api-key `/v1` endpoints, and the isolation tooling. **No** products/variants/competitors/matches or their endpoints (SPEC-04+).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Relevance | How this plan satisfies it |
|-----------|-----------|----------------------------|
| **I. API-First / Service boundaries** | New `app_shared` modules + FastAPI in `apps/api` | Identity models, `security/` primitives, and scoping helpers live in `app_shared` and import only sqlalchemy/psycopg/argon2-cffi/pyjwt/redis/stdlib — never scrapy/twisted/playwright and **never fastapi**. The FastAPI auth dependency + `/v1` routers live in `apps/api`, importing `app_shared` one-way. The import-boundary test is extended to cover `app_shared.models.identity` and `app_shared.security.*`. Only `apps/api` is publicly exposed. **PASS** |
| **II. Workspace Isolation (NON-NEGOTIABLE)** | The core of this spec | `users` and `api_keys` are workspace-owned; `emit_rls_policy()` (ENABLE + FORCE + fail-closed `NULLIF(current_setting('app.workspace_id', true),'')::uuid`) is called in the **same migration** that creates them. Every authenticated request resolves exactly one workspace and issues `set_config('app.workspace_id', :wsid, true)` (transaction-scoped, PgBouncer-safe) before any workspace-owned query. Workspace-scoped repository helpers require a `workspace_id` and forbid `session.get()`/unscoped `select()` on workspace-owned models. `scripts/check_workspace_scoping.py` (AST) fails CI on any introduced unscoped fetch/select. Cross-workspace + no-context (fail-closed) tests authored (live-DB). SUPER_ADMIN (nullable `workspace_id`) is **not** an RLS bypass — it must assume an explicit, role-authorized workspace context per request; NULL-workspace rows never match the policy (fail-closed by construction). **PASS** |
| **III. Variant-level pricing** | N/A this spec | No pricing/matching/variants. Strictly identity + auth. **PASS (N/A)** |
| **IV. Database-driven config** | Light | Behavior is config-driven via `Settings` (TTLs, rate-limit thresholds, JWT secret/alg) not hardcoded; `workspaces.default_scrape_profile_id`/`default_access_policy_id` exist as plain nullable ids (no FK — targets are later specs). **PASS** |
| **V. Disciplined Scraping Runtime (NON-NEGOTIABLE)** | Import boundary only | No scraping code. `app_shared` stays scrapy-free (see I). **PASS (N/A)** |
| **VI. Internal-only / legal** | N/A this spec | No scraping/access code. **PASS (N/A)** |
| **VII. Monetary correctness (NON-NEGOTIABLE)** | N/A this spec | No money columns in the identity tables. **PASS (N/A)** |
| **VIII. Scale-safe data & concurrency (NON-NEGOTIABLE)** | Hot-path & pooler rules | **No per-request hot-row writes**: `last_used_at` is Redis-throttled to ≤1 write/key/min (FR-015); status served from a short-TTL Redis cache → 0 per-request status DB reads (FR-022). UUIDv7 PKs (via `Base`), `TIMESTAMPTZ` everywhere (via `TimestampMixin`/`TZDateTime`). All app traffic through PgBouncer; refresh rotation uses an atomic row-level `UPDATE ... WHERE revoked_at IS NULL RETURNING` (no session advisory locks) so exactly one of two concurrent exchanges wins under transaction pooling. Correctness-critical Redis keys on the noeviction instance; cache-unavailable → fail-safe deny. Single linear migration history (existing CI head guard). **PASS** |

**Technology & Security Constraints (§33/§24/§34)**: Stack lock-in honored (SQLAlchemy+Alembic, PostgreSQL, psycopg, Redis, FastAPI). API keys + refresh tokens stored only as hashes; full API key shown once; scopes + revocation + `last_used_at` (§33). Roles `SUPER_ADMIN`/`WORKSPACE_ADMIN`/`READ_ONLY` (string-backed, app-validated). Public API versioned under `/v1` (§24). Structured error codes reused where relevant (e.g. `RATE_LIMITED`); login failures use a single uniform error with no factor disclosure (§34/FR-006). New secrets (`JWT_SECRET`, bootstrap admin creds) are env vars, never committed. (Proxy/webhook Fernet encryption from §33 is out of scope — no such fields here.)

**Gate result**: PASS — no violations. Complexity Tracking table intentionally empty. Re-checked post-Phase-1 (see end of plan): still PASS.

## Project Structure

### Documentation (this feature)

```text
specs/003-auth-api-keys-workspace-isolation/
├── plan.md              # This file
├── research.md          # Phase 0 — pinned libs, token design, atomic rotation, RLS+nullable-workspace, CI guard, bootstrap, Redis keys
├── data-model.md        # Phase 1 — 4 tables, enums, scopes, validation, RLS/isolation model, state transitions
├── quickstart.md        # Phase 1 — how to validate (unit here; live RLS/rate-limit/migration on a PG+Redis host)
├── contracts/           # Phase 1 — the interfaces this feature exposes
│   ├── api-auth.md            # POST /v1/auth/login|refresh|logout
│   ├── api-keys.md            # POST/GET /v1/api-keys, DELETE /v1/api-keys/{id}
│   ├── security-passwords.md  # hash_password / verify_password (argon2id)
│   ├── security-tokens.md     # refresh-token gen/hash + atomic rotation; API-key gen/hash/prefix/verify
│   ├── security-jwt.md         # access-token encode/decode + claims
│   ├── security-scopes.md      # Scope vocabulary + has_scopes()
│   ├── security-cache.md       # Redis rate-limit / status-cache / last-used-throttle helpers + key design
│   ├── workspace-context.md    # set_workspace_context() + FastAPI dependency flow
│   ├── repository-scoping.md   # workspace-scoped query helpers (require workspace_id; forbid unscoped)
│   ├── ci-scoping-guard.md      # scripts/check_workspace_scoping.py (AST) contract
│   └── migration-identity.md    # the identity migration + RLS + bootstrap seed
└── tasks.md             # Phase 2 (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
libs/shared/app_shared/
├── config.py            # EXTEND: JWT_SECRET, JWT_ALGORITHM, ACCESS_TOKEN_TTL_SECONDS,
│                        #   REFRESH_TOKEN_TTL_SECONDS, STATUS_CACHE_TTL_SECONDS,
│                        #   LOGIN_RATE_LIMIT_* , API_KEY_LAST_USED_THROTTLE_SECONDS,
│                        #   AUTH_DATABASE_URL (optional; BYPASSRLS auth role), ARGON2_* (optional)
├── enums.py             # EXTEND: WorkspaceStatus, UserRole, UserStatus, ApiKeyStatus (StrEnum)
├── database.py          # EXTEND (minor): set_workspace_context(session, workspace_id) via set_config(...,true);
│                        #   get_auth_session() (AUTH_DATABASE_URL role) for pre-context credential lookups
├── repository.py        # NEW: workspace-scoped query helpers — scoped_select(Model, ws_id),
│                        #   scoped_get(session, Model, id, ws_id); forbid unscoped access on workspace-owned models
├── models/
│   ├── __init__.py      # EXTEND: re-export Workspace, User, RefreshToken, ApiKey
│   └── identity.py      # NEW: Workspace (tenant root), User (nullable workspace_id + FK),
│                        #   RefreshToken (created_at only, no RLS), ApiKey (WorkspaceScopedBase + FK)
└── security/
    ├── __init__.py      # NEW
    ├── passwords.py     # NEW: hash_password / verify_password / needs_rehash (argon2id, per-hash salt, uniform)
    ├── api_keys.py      # NEW: generate_api_key -> (secret, prefix, sha256_hash); hash/verify; prefix parse
    ├── tokens.py        # NEW: generate_refresh_token -> (raw, sha256_hash); hash_token; rotation SQL contract
    ├── jwt.py           # NEW: encode_access_token / decode_access_token (PyJWT, exp/signature verify)
    ├── scopes.py        # NEW: Scope StrEnum vocabulary + has_scopes(granted, required)
    ├── rate_limit.py    # NEW: Redis per-account + per-source login limiter (progressive backoff, fail-safe deny)
    ├── status_cache.py  # NEW: Redis user/workspace status cache (short TTL; miss → single DB read; fail-safe deny)
    └── last_used.py     # NEW: Redis SET-NX throttle gate → ≤1 api_key last_used_at write/key/min

apps/api/app/
├── main.py              # EXTEND: include the /v1 auth + api-keys routers
├── deps.py              # NEW: authenticate (JWT or API key) → principal; load cached status (fail-safe deny);
│                        #   resolve + authorize workspace context; open request txn + set_workspace_context;
│                        #   require_scopes() / require_role() guards
├── errors.py            # NEW: uniform auth error + structured error-code responses (RATE_LIMITED, ...)
└── routers/
    ├── __init__.py      # NEW
    ├── auth.py          # NEW: POST /v1/auth/login|refresh|logout
    └── api_keys.py      # NEW: POST/GET /v1/api-keys, DELETE /v1/api-keys/{id}

alembic/versions/
└── <rev>_auth_identity_tables.py   # NEW: create workspaces, users, refresh_tokens, api_keys;
                                    #   emit_rls_policy on users + api_keys; downgrade; down_revision=023a24e5717d

scripts/
├── check_workspace_scoping.py      # NEW: AST guard — fail on unscoped session.get()/select() of workspace-owned models
└── seed_bootstrap.py               # NEW: seed first workspace + SUPER_ADMIN from env (no public signup)

tests/unit/
├── test_import_boundaries.py       # EXTEND: cover app_shared.models.identity + app_shared.security.* (no fastapi/scrapy)
├── test_passwords.py               # NEW: hash≠plaintext, verify round-trip, wrong pw fails, uniform, needs_rehash
├── test_api_key_security.py        # NEW: gen entropy, prefix parse, sha256 hash, verify, prefix-collision safety
├── test_refresh_tokens.py          # NEW: gen/hash, rotation-SQL shape, rotated-reuse rejected (pure logic)
├── test_jwt.py                     # NEW: encode/decode round-trip, claims, expired → reject, bad-sig → reject
├── test_scopes.py                  # NEW: Scope vocabulary, has_scopes, out-of-scope refused, invalid scope rejected
├── test_identity_models.py         # NEW: table shapes, users.workspace_id nullable, api_keys RLS-ready, enums
├── test_rls_identity.py            # NEW: emit_rls_policy render for users + api_keys (fail-closed DDL string)
├── test_repository_scoping.py      # NEW: scoped helpers require ws_id; unscoped access on workspace-owned model raises
├── test_workspace_scoping_guard.py # NEW: guard flags a planted session.get(User)/select(ApiKey) violation; clean passes
├── test_migration_offline_auth.py  # NEW: `alembic upgrade head --sql` renders the 4 tables + RLS; single head
└── test_uniform_login_error.py     # NEW: unknown-email and wrong-password produce identical error shape
tests/integration/  (marked live-DB / live-Redis)
├── test_rls_cross_workspace.py     # NEW: ws-A request reads/writes 0 rows of ws-B; unscoped query still 0 (RLS); no-context → 0
├── test_auth_flow.py               # NEW: login → refresh (rotate) → reuse rejected → concurrent rotate (1 wins) → logout revokes
├── test_api_key_flow.py            # NEW: create (secret once) → auth → scope-limited → list (no secret) → revoke → denied
├── test_rate_limit.py              # NEW: per-account + per-source backoff engages; fail-safe deny on cache down
├── test_status_cache.py           # NEW: suspend → rejected within TTL; steady-state 0 per-request status DB reads
└── test_last_used_throttle.py      # NEW: ≤1 last_used_at write/key/min under burst
```

**Structure Decision**: Extend the existing `app_shared` package rather than fork it. Identity ORM models go in `app_shared/models/identity.py` (built on SPEC-02 `Base`/`TimestampMixin`/`WorkspaceScopedBase`/`emit_rls_policy`); auth enums extend `app_shared/enums.py`; the framework-agnostic crypto + Redis helpers form a new `app_shared/security/` package (master §5 tree); workspace-scoped query helpers go in `app_shared/repository.py` and the transaction-context helper in `app_shared/database.py`. The FastAPI authentication **dependency** and `/v1` **routers** live in `apps/api` (never in `app_shared` — framework-agnostic boundary). The Alembic migration lives at repo root and creates all four tables + RLS in one revision. CI scripts (`check_workspace_scoping.py`, `seed_bootstrap.py`) live in `scripts/`. `schemas/`, `pagination.py`, and any product/competitor/match tables are explicitly **out of scope**.

## Phase 0 / Phase 1 outputs

- Phase 0 research: [research.md](./research.md)
- Phase 1 data model: [data-model.md](./data-model.md)
- Phase 1 contracts: [contracts/](./contracts/)
- Phase 1 quickstart: [quickstart.md](./quickstart.md)

**Agent context update**: The repo does not use GitHub Copilot — `.github/copilot-instructions.md` does not exist and the `after_plan` agent-context hook is disabled in `.specify/extensions.yml` (see user memory "No GitHub Copilot"). No agent-context file was written; this step is intentionally skipped.

## Post-Design Constitution Re-Check

Re-evaluated after Phase 1 artifacts: no new violations. The design keeps `app_shared` framework-agnostic (fastapi only in `apps/api`), applies fail-closed RLS to `users`+`api_keys` in their creating migration, resolves exactly one workspace per request via pooler-safe `set_config(...,true)`, forbids unscoped access through scoped helpers + an AST CI guard, keeps the hot path free of per-request status DB reads and per-request `last_used_at` writes (Redis cache + throttle on the noeviction instance), and makes refresh rotation atomic via a single row-level `UPDATE ... RETURNING`. SUPER_ADMIN's nullable workspace is handled without weakening fail-closed RLS (explicit per-request workspace assumption; NULL rows never match). **Gate: PASS.**

## Complexity Tracking

> No Constitution Check violations — table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |
