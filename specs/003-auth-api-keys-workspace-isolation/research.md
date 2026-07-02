# Phase 0 Research: Auth, API Keys & Workspace Isolation

All decisions below were resolved from `PROJECT_SPEC.md` (§4/§6/§22/§24/§32/§33/§34), the constitution (Principles I/II/VIII), the SPEC-01/02 foundation, and the spec's Clarifications. Numeric lifetimes/thresholds are sensible, env-tunable defaults chosen within the doc's security constraints. No item required a stakeholder decision.

Format per decision: **Decision** / **Rationale** / **Alternatives considered**.

---

## D1 — Pinned libraries

**Decision**:
- **Password hashing**: `argon2-cffi>=23.1,<24` (argon2id). Framework-agnostic → lives in `app_shared/security/passwords.py`.
- **Access-token JWT**: `pyjwt>=2.9,<3`. Framework-agnostic → `app_shared/security/jwt.py`.
- **API-key & refresh-token generation + hashing**: **stdlib only** — `secrets.token_urlsafe()` for high-entropy secrets, `hashlib.sha256` for the at-rest hash, `hmac.compare_digest` for constant-time verification. No third-party dependency.
- **Redis client (rate-limit / status cache / last-used throttle)**: `redis>=5.0,<6` (redis-py, **sync**), added to `app_shared` (cross-service; workers already use Redis via Celery, `apps/api` uses it directly). Framework-agnostic helpers take a `redis.Redis` client.

Add `argon2-cffi`, `pyjwt`, `redis` to `libs/shared/app_shared/pyproject.toml`; run `uv lock`. `apps/api` gains no new direct deps beyond `app_shared` (fastapi already present). All three imports are permitted under the boundary rules (not scrapy/twisted/playwright; not fastapi).

**Rationale**: argon2id is §33's first-listed/recommended password KDF; argon2-cffi is the de-facto maintained binding and handles per-hash random salt + encoded parameters internally (`$argon2id$...` string embeds salt+params, so no separate salt column). PyJWT is the standard, minimal JWT lib (HS256). §12/§33 require a **fast** hash (not a KDF) for API keys/refresh tokens — SHA-256 from stdlib is exactly that and needs no dependency. redis-py sync matches the codebase's sync SQLAlchemy style and FastAPI's current sync endpoints.

**Alternatives considered**: `bcrypt` (allowed §33 fallback, but 72-byte truncation + weaker memory-hardness → argon2id preferred); `passlib` (heavier, maintenance concerns, unneeded wrapper over argon2-cffi); `python-jose` (larger surface, CVE history — PyJWT preferred); async redis / aioredis (rejected — no async DB path in this codebase yet; keep one concurrency model). Using a password KDF for API keys — **explicitly forbidden** by FR-012 (keys are high-entropy already; a slow KDF on every request would wreck the hot path).

---

## D2 — Access & refresh token design

**Decision**:
- **Access token** = stateless **signed JWT** (HS256, key from `JWT_SECRET`). Claims: `sub` = user_id, `workspace_id` (the resolved context; may be absent for a not-yet-scoped SUPER_ADMIN token), `role`, `scopes` (optional; primarily an API-key concept — user authorization is by `role`), `type="access"`, `iat`, `exp`, `jti`. Default TTL **`ACCESS_TOKEN_TTL_SECONDS = 900`** (15 min).
- **Refresh token** = **opaque, high-entropy random** (`secrets.token_urlsafe(32)` → 256-bit), returned to the client raw, stored server-side **only as `sha256(raw)`** in `refresh_tokens.token_hash`. Default TTL **`REFRESH_TOKEN_TTL_SECONDS = 2592000`** (30 days).
- **API-key auth carries scopes directly** (from the `api_keys.scopes` column) — API-key requests never mint a JWT; they authenticate per-request by prefix+hash lookup.

**Rationale**: A short-lived stateless JWT lets the request pipeline resolve identity + workspace + role/scope with **no DB read on the hot path beyond the cached status check** (FR-024, §32/§35). Refresh tokens are opaque + hashed so a DB/backup leak never yields a usable credential (FR-008, §33), and being server-side records they can be rotated/revoked (JWTs can't be individually revoked before expiry — hence the *short* access TTL). Splitting "role for humans, scopes for keys" keeps the authorization model simple and matches §22 (users have `role`; api_keys have `scopes`).

**Alternatives considered**: stateful/opaque access tokens (rejected — a DB read per request defeats §32/§35 hot-path goal); long-lived access tokens (rejected — no revocation window); storing refresh tokens with a password KDF (rejected — unnecessary CPU; a 256-bit random needs only a fast hash); JWT refresh tokens (rejected — can't rotate/revoke server-side atomically). Signing alg RS256 (rejected for v1 — single service signs+verifies, HS256 with a shared secret is simpler; alg is a config knob `JWT_ALGORITHM` if this changes).

---

## D3 — Atomic refresh-token rotation (pooler-safe)

**Decision**: Rotate in a **single row-level statement** inside the request transaction:

```sql
UPDATE refresh_tokens
   SET revoked_at = now()
 WHERE token_hash = :presented_hash
   AND revoked_at IS NULL
   AND expires_at > now()
RETURNING id, user_id;
```

- **One row returned** → this caller won; it then INSERTs a new `refresh_tokens` row and issues a new access+refresh pair.
- **Zero rows returned** → the token was already rotated (`revoked_at` set), revoked (logout), expired, or never existed → reject with the uniform auth error. This covers rotated-reuse (FR-009), expiry (FR-011), and logout-revocation (FR-011) in one predicate.
- **Concurrency (FR-010/SC-002)**: two simultaneous exchanges of the same token race on the `UPDATE ... WHERE revoked_at IS NULL`; Postgres row locking guarantees **exactly one** UPDATE matches the still-NULL row and returns; the other sees the row already `revoked_at`-set and returns zero. No application coordination, **no session advisory lock** — so it is correct under PgBouncer transaction pooling (the whole thing is one statement in one transaction).

**Rationale**: `UPDATE ... RETURNING` with a `WHERE revoked_at IS NULL` guard is the canonical compare-and-swap for exactly-once rotation; it is atomic at the row level and needs no session-scoped state, satisfying the constitution's "design for `SET LOCAL` / xact-scoped locks only" rule (§4/§6, Principle VIII).

**Alternatives considered**: `SELECT ... FOR UPDATE` then `UPDATE` (rejected — two round-trips, larger lock window, same outcome); `pg_advisory_xact_lock` on the token (works but adds a lock and is unnecessary given the CAS UPDATE); session `pg_advisory_lock` (rejected — session-scoped locks are unsafe under transaction pooling). **Optional hardening** noted for a later spec: on detected rotated-reuse, revoke the entire token family — deferred because §22's `refresh_tokens` shape has no `family_id` column; v1 rejects the reuse (which is sufficient for the spec's acceptance criteria).

---

## D4 — RLS on `users`/`api_keys` with a nullable-workspace SUPER_ADMIN

**Decision**: A layered approach that keeps fail-closed RLS fully intact:

1. **Policies unchanged**: `emit_rls_policy("users")` and `emit_rls_policy("api_keys")` are called in the same migration that creates each table — ENABLE + FORCE + `USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)`. No carve-out is added to the predicate.
2. **SUPER_ADMIN is not a bypass**: `users.workspace_id` is NULL for a SUPER_ADMIN, and a NULL-workspace row **never** matches the policy predicate (`NULL = anything` → NULL → not true). This is *desired*: a SUPER_ADMIN has no implicit home workspace and cannot be read via ordinary workspace-scoped access. Per the spec Edge Case, a SUPER_ADMIN request **must supply an explicit target workspace** (via the `X-Workspace-Id` header or a workspace path segment); the request sets `set_config('app.workspace_id', <chosen>, true)` and then operates **under normal fail-closed RLS**, seeing exactly that one workspace. Authorization to *assume* a workspace is a role check: SUPER_ADMIN may assume any workspace; WORKSPACE_ADMIN/READ_ONLY may assume only their own `workspace_id`. Assuming a workspace is **not** a wildcard RLS bypass.
3. **Pre-authentication credential lookups run through a narrow privileged path**: the three lookups that inherently occur *before* any workspace context exists and that target an RLS'd table —
   - login: find `users` row by unique `email`;
   - API-key auth: find `api_keys` row by `key_prefix`;
   (refresh-token lookup is by `token_hash` on `refresh_tokens`, which is **not** RLS'd — so it needs no special path)
   — are executed by a dedicated **`crawmatic_auth` database role that has `BYPASSRLS`**, used **only** by the authentication repository for this fixed, credential-filtered set of queries (never for arbitrary or user-supplied filters). Config exposes `AUTH_DATABASE_URL` (this role's connection, still through the pooler). The normal request-serving role (`crawmatic_app`, from `DATABASE_URL`) has **no** BYPASSRLS and remains under FORCE RLS for everything.
4. **Bootstrap** (D6) inserts the first workspace + SUPER_ADMIN via the migration/seed direct connection (a privileged role), avoiding any chicken-and-egg.

**Justification against fail-closed (Principle II)**: the only RLS bypass in the system is confined to the **authentication boundary** — unique-credential lookups (`email`, `key_prefix`) that are the very act of establishing identity, performed by a *distinct* role that serves no application data. The request-serving role can never bypass RLS, so a forgotten `workspace_id` filter or an unset context still yields zero cross-workspace rows. The bypass returns exactly one credential row for verification; it does not expose bulk data and cannot be reached with attacker-controlled filters.

**Alternatives considered**:
- **`SECURITY DEFINER` SQL functions** (`auth_find_user_by_email`, `auth_find_api_key_by_prefix`) owned by a BYPASSRLS role, `EXECUTE` granted to the app role — self-contained in migration DDL, avoids a second app connection. Rejected as *primary* only because the owning role still needs BYPASSRLS (a cluster-level grant outside the migration) and SECURITY DEFINER functions are easy to over-grant; kept as a documented alternative for environments that prefer no second connection.
- **A permissive per-role RLS policy** (`CREATE POLICY ... TO crawmatic_auth USING (true)`) instead of BYPASSRLS — equivalent effect, slightly finer-grained; usable but BYPASSRLS on a dedicated role is simpler to reason about. Either is acceptable; BYPASSRLS chosen.
- **Giving the app role BYPASSRLS and relying only on app-level filters** — **rejected outright**: destroys the non-negotiable defense-in-depth (a single missing filter becomes a breach).
- **Making `workspace_id` NOT NULL and modelling SUPER_ADMIN with a sentinel/"platform" workspace** — rejected: §22 mandates nullable `workspace_id`; a sentinel workspace would need its own bypass to act cross-tenant anyway.

In this no-live-DB environment: the `emit_rls_policy` render for `users`/`api_keys` is unit-tested; the two-role behavior (SUPER_ADMIN assuming a workspace; auth-role lookup succeeding while app-role is blocked) is authored and deferred to a PG host.

---

## D5 — Workspace context flow (pooler-safe) & the FastAPI dependency

**Decision**:
- **Transaction context helper** in `app_shared/database.py`:
  `set_workspace_context(session, workspace_id)` executes
  `session.execute(text("SELECT set_config('app.workspace_id', :wsid, true)"), {"wsid": str(workspace_id)})`.
  `set_config(name, value, is_local=true)` is the **bind-parameterizable** equivalent of `SET LOCAL` — transaction-scoped (so it is correct under PgBouncer transaction pooling) and avoids interpolating the UUID into SQL text (no injection surface). It composes with the SPEC-02 `get_session()`/engine (the app role, `DATABASE_URL` → pooler).
- **FastAPI dependency** (`apps/api/app/deps.py`) resolves context per request, in order:
  1. Extract the credential from `Authorization: Bearer <jwt>` **or** `Authorization: Bearer <api_key>` (API keys carry a recognizable `ck_`-prefixed shape → routed to key auth; otherwise decoded as JWT).
  2. **JWT path**: `decode_access_token()` (verify signature + `exp`); read `sub`, `workspace_id`, `role`. **API-key path**: parse `key_prefix`, look up the key via the auth-role path (D4), `hmac.compare_digest(sha256(secret), key_hash)`; read `workspace_id`, `scopes`; check `status == ACTIVE`; fire the Redis last-used throttle (D8).
  3. **Cached status check** (D8): read `status:user:{id}` and `status:ws:{wsid}` from Redis; if suspended → reject; **fail-safe deny** if the cache is unavailable. No per-request status DB read in steady state.
  4. **Resolve + authorize the workspace context**: normal principal → its own `workspace_id`; SUPER_ADMIN (JWT `workspace_id` absent) → the explicit `X-Workspace-Id` from the request, role-authorized (D4).
  5. Open the request-scoped session/transaction and call `set_workspace_context(session, wsid)` **before any workspace-owned query**; the dependency yields the scoped session + principal.
- **Authorization guards**: `require_scopes(*scopes)` (API-key requests) and `require_role(*roles)` (human endpoints, e.g. api-key management is WORKSPACE_ADMIN+).

**Rationale**: `set_config(...,true)` is the documented pooler-safe way to feed the RLS GUC with a bound parameter; doing it at the start of the request transaction guarantees RLS sees the right workspace for every subsequent query in that transaction (FR-017). Putting the dependency in `apps/api` keeps `app_shared` FastAPI-free.

**Alternatives considered**: literal `SET LOCAL app.workspace_id = '<uuid>'` (works but can't bind-parameter safely → string interpolation; `set_config` preferred); setting the GUC at connection checkout via an event listener (rejected — under transaction pooling the connection isn't stably request-bound; must be per-transaction); a middleware instead of a dependency (dependency chosen — integrates with FastAPI's DI, per-route opt-in, testable).

---

## D6 — Bootstrap: first workspace + SUPER_ADMIN

**Decision**: A standalone **seed script** `scripts/seed_bootstrap.py`, run via the direct migration connection (`MIGRATION_DATABASE_URL`), that is idempotent and reads credentials from env (`BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD`, optional `BOOTSTRAP_WORKSPACE_NAME`/`_SLUG`). It creates the first `workspaces` row (if none) and a SUPER_ADMIN `users` row (`workspace_id = NULL`, argon2id-hashed password). No public self-service signup endpoint exists (out of scope per spec Assumptions / §38).

**Rationale**: Secrets must not live in migration history; a seed script reading env keeps the admin password out of committed SQL and is re-runnable. Running via the direct/privileged connection sidesteps RLS during bootstrap. Matches the Clarification (seed/admin path, not signup).

**Alternatives considered**: an Alembic **data migration** (rejected — would bake a password/hash into version history, and re-running/rotating is awkward); a public signup endpoint (rejected — explicitly out of scope, §38 "what not to build").

---

## D7 — CI unscoped-query guard

**Decision**: `scripts/check_workspace_scoping.py` — a **stdlib `ast`-based** scanner over `apps/` and `libs/` (`.py` files) that fails (exit 1) when it finds, on a **workspace-owned model** (`User`, `ApiKey` — set is centrally defined and extensible):
- `session.get(User, ...)` / `<session>.get(ApiKey, ...)` — unscoped fetch-by-id;
- `select(User)` / `select(ApiKey)` (and legacy `session.query(User)`) **not** accompanied by a `workspace_id` predicate (`.where(...workspace_id...)` / `.filter_by(workspace_id=...)`) in the same call chain.

Runs with no DB/Redis (pure static analysis), invoked as a CI step exactly like `scripts/check_single_head.sh`. **False-positive handling**: the sanctioned scoped-helper module (`libs/shared/app_shared/repository.py`, where scoped selects are legitimately *constructed* generically) is path-allowlisted; individual lines may carry an explicit `# noqa: workspace-scope` pragma (discouraged, must be justified in review). A unit test (`test_workspace_scoping_guard.py`) plants a violation in a temp file and asserts the guard flags it, and asserts a properly-scoped snippet passes.

**Rationale**: AST (not grep) so it understands call structure (attribute call `session.get`, `select(<Name>)`) and won't false-positive on strings/comments; satisfies FR-020/SC-006 ("fails the build on 100% of introduced unscoped fetch-by-id / unscoped selects"). Reuses the established single-head-guard CI pattern.

**Alternatives considered**: a regex/grep guard (rejected — brittle, matches comments/strings, can't see call shape); a runtime SQLAlchemy event that blocks unscoped emits (useful as an additional *runtime* safety net but is not a *build* gate — RLS already provides the runtime net; the CI guard is the static gate the spec asks for). Both could coexist later; the static AST guard is the FR-020 deliverable.

---

## D8 — Redis key design (correctness-critical, noeviction instance)

**Decision** (all keys on the `noeviction` Redis instance per §4; helpers **fail-safe deny/challenge** when Redis is unavailable):

- **Login rate limit** (FR-007/SC-009): per-account key `rl:login:acct:{sha256(email)}` and per-source key `rl:login:src:{ip}`. Each is an `INCR` with a window `EXPIRE` (`LOGIN_RATE_LIMIT_WINDOW_SECONDS`, default 60) plus a progressive-backoff lock key `rl:login:lock:{...}` whose TTL grows with the violation count (`LOGIN_RATE_LIMIT_MAX_ATTEMPTS`, default 5, then exponential backoff cap). A login is refused if **either** the account or the source is over threshold. Cache down → **deny/challenge** (never silently allow unlimited attempts).
- **Status cache** (FR-022/SC-007): `status:user:{user_id}` and `status:ws:{workspace_id}` → the status string, `EXPIRE STATUS_CACHE_TTL_SECONDS` (default 30, env-tunable ~30–60). On miss, read the single row from DB once and repopulate; steady-state reads are cache hits → **0 per-request status DB reads**. A suspension takes effect within one TTL. Cache down → **fail-safe deny**.
- **API-key last-used throttle** (FR-015/SC-008): a gate key `apikey:lastused:{key_id}` set with `SET key 1 NX EX API_KEY_LAST_USED_THROTTLE_SECONDS` (default 60). If the `SET NX` **succeeds** (gate was absent) the request performs the **single** `UPDATE api_keys SET last_used_at = now()` for this window; if it fails (gate present) no write occurs → **≤1 write/key/min** regardless of volume. Cache down → skip the update (usage tracking is best-effort; never blocks or duplicates writes).

**Rationale**: These three are the "correctness-critical counters must not be evicted" set (§4, spec Edge Cases) — hence the noeviction instance and fail-safe-deny for the security-sensitive two (rate-limit, status). The `SET NX EX` gate is the minimal coalescing primitive that guarantees the once-per-minute write bound without a background flusher.

**Alternatives considered**: a token-bucket Lua script for rate limiting (more precise; deferred — INCR+EXPIRE + backoff lock meets SC-009 and is simpler); buffering the actual `last_used_at` timestamp in Redis and flushing via a periodic job (rejected for v1 — the `SET NX` gate already bounds writes to ≤1/min without extra machinery); relying on DB reads for status with a short app-memory cache (rejected — doesn't propagate suspensions across processes and reintroduces per-request DB reads).

---

## D9 — Config additions (`Settings`)

**Decision** — add to `app_shared/config.py` (`Settings`):

| Field | Type / default | Purpose |
|-------|----------------|---------|
| `JWT_SECRET` | `str` (**required**) | HS256 signing key for access tokens |
| `JWT_ALGORITHM` | `str = "HS256"` | Access-token algorithm |
| `ACCESS_TOKEN_TTL_SECONDS` | `int = 900` | ~15 min access lifetime |
| `REFRESH_TOKEN_TTL_SECONDS` | `int = 2592000` | ~30 day refresh lifetime |
| `STATUS_CACHE_TTL_SECONDS` | `int = 30` | Suspension propagation window (~30–60s) |
| `LOGIN_RATE_LIMIT_MAX_ATTEMPTS` | `int = 5` | Attempts before backoff |
| `LOGIN_RATE_LIMIT_WINDOW_SECONDS` | `int = 60` | Rate-limit window |
| `API_KEY_LAST_USED_THROTTLE_SECONDS` | `int = 60` | ≤1 last_used write/key per this window |
| `AUTH_DATABASE_URL` | `str \| None = None` | BYPASSRLS auth-role connection (D4); falls back to `DATABASE_URL` only in single-role dev (documented caveat: true isolation testing requires the two-role setup) |
| `ARGON2_TIME_COST` / `ARGON2_MEMORY_COST` / `ARGON2_PARALLELISM` | optional `int` | argon2id tuning; default to argon2-cffi's recommended params when unset |

**Rationale**: `JWT_SECRET` is required so a missing signing key fails fast at construction (SPEC-01 fail-fast pattern). All lifetimes/thresholds are env-tunable defaults per the Clarifications (SC-007 asserts "within configured TTL", not a fixed number). `AUTH_DATABASE_URL` is optional so single-role dev still boots; production sets it to the dedicated BYPASSRLS role.

**Alternatives considered**: reusing a single `SECRET_KEY` name (chose the explicit `JWT_SECRET` for clarity; can alias later); hardcoding TTLs (rejected — §35 requires config-tunable).

---

## D10 — What is unit-testable in this environment vs. deferred

**Unit-testable here (no DB/Redis)** — the FR/SC coverage this environment fully validates:
- `hash_password`/`verify_password`: hash ≠ plaintext, round-trip verify, wrong password fails, uniform behavior, `needs_rehash` (FR-005).
- API-key `generate`/`hash`/`prefix`/`verify`: entropy/length, prefix parsing, SHA-256 hash, constant-time verify, **prefix-collision safety** (two keys sharing a prefix resolve by full-hash) (FR-012/FR-016).
- Refresh-token `generate`/`hash` and the **rotation predicate logic** (pure): rotated/expired/revoked → rejected (FR-008/FR-009/FR-011 logic).
- JWT `encode`/`decode`: claims round-trip, expired → reject, bad signature → reject (FR-024).
- Scope vocabulary + `has_scopes`: out-of-scope refused, invalid scope value rejected (FR-013).
- Identity model shapes: `users.workspace_id` nullable, `api_keys` workspace-owned, enum columns render as strings; `emit_rls_policy` DDL render for `users`+`api_keys` (fail-closed predicate present) (FR-002/FR-004/FR-019 render).
- Workspace-scoped helper: rejects unscoped access on a workspace-owned model; requires a `workspace_id` (FR-018).
- The CI guard: flags a planted `session.get(User)` / unscoped `select(ApiKey)`; passes clean code (FR-020/SC-006).
- Uniform-login-error **shape**: unknown-email vs wrong-password produce byte-identical error responses (FR-006/SC-001).
- Offline migration render (`alembic upgrade head --sql`) emits the four tables + RLS; single head (FR-004/FR-023 render).

**Authored + deferred to a PostgreSQL/Redis host** (live services required):
- RLS row denial + cross-workspace blocking + no-context-fail-closed (FR-019/FR-021/SC-005).
- Concurrent refresh rotation "exactly one wins" against real row locking (FR-010/SC-002).
- Rate-limit engagement/backoff + fail-safe deny (FR-007/SC-009); status-cache suspension-within-TTL + 0 per-request status DB reads (FR-022/SC-007); last-used ≤1 write/key/min under burst (FR-015/SC-008).
- Online migration run + bootstrap seed against real Postgres (FR-004/FR-023).
- Full request integration (login→refresh→logout; api-key create→auth→scope-limit→revoke) end-to-end (FR-006/FR-011/FR-012/FR-014).

**Rationale**: mirrors the SPEC-01/02 split — the no-Docker build env validates all pure logic and DDL render; anything needing live row-level enforcement or Redis semantics is written now and gated on `MIGRATION_DATABASE_URL`/Redis availability, matching the spec's Clarification on live acceptance items.
