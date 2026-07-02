# Feature Specification: Auth, API Keys & Workspace Isolation

**Feature Branch**: `003-auth-api-keys-workspace-isolation`

**Created**: 2026-07-02

**Status**: Draft

**Input**: SPEC-03 from PROJECT_SPEC.md §35 — secure API foundation: identity, authentication, API keys, and enforced multi-tenant isolation.

## Clarifications

### Session 2026-07-02

All items below were resolved from the master specification (`PROJECT_SPEC.md` §22/§24/§32/§33) and the SPEC-01/02 foundation; no open ambiguity required a stakeholder decision. Numeric lifetimes are sensible, config-tunable defaults chosen at planning time within the doc's security constraints.

- Q: Password hashing algorithm? → A: argon2id with a per-user salt (the doc's first-listed, recommended option; bcrypt is an allowed fallback) (source: doc §33).
- Q: Access-token vs refresh-token lifetimes? → A: short-lived signed access token (default ~15 min) + longer-lived opaque refresh token (default ~30 days), both env-tunable; exact values are a plan/config detail within "short-lived access + rotating refresh" (source: doc §33 + §24; defaults).
- Q: Status-cache TTL (suspension propagation window)? → A: a short TTL (default ~30–60 s), env-tunable; SC-007 asserts rejection within the configured TTL, not a fixed number (source: §35 "within cache TTL"; default).
- Q: Login rate-limit thresholds / backoff? → A: per-account AND per-source-address counters with progressive backoff; concrete thresholds are config-tunable defaults (source: doc §24/§33; defaults).
- Q: How is the first workspace + SUPER_ADMIN created (no public signup)? → A: via a seed/bootstrap path (migration/admin seed), NOT a public self-service signup flow (out of scope) (source: doc §37 demo seed, §38 "what not to build"; spec Assumptions).
- Q: Which tables are workspace-owned (get RLS now)? → A: `users` and `api_keys` (RLS in their creating migration via SPEC-02 emit_rls_policy); `workspaces` is the tenant root (no workspace_id, no RLS-by-workspace); `refresh_tokens` is reached only via its owning user (source: §22/§32).
- Q: JWT claims / hot-path reads? → A: access token carries identity + workspace_id + role/scope claims so context/authorization resolve without a DB read beyond the cached status check (source: §32/§35; default).
- Q: Live-DB/Redis acceptance items here? → A: DB/Redis-independent logic unit-tested in this env; live RLS-denial, cross-workspace blocking, rate-limit/status-TTL, migration run authored + deferred to a Postgres/Redis host (source: no-docker-daemon constraint).

## User Scenarios & Testing *(mandatory)*

The users of this feature are: **human operators** who sign in to administer a workspace; **machine clients** (future integrations) that authenticate with API keys; and the **platform itself**, which must guarantee that no request ever reads or writes another workspace's data. This feature delivers the identity tables, the login/refresh/logout flows, API-key issuance and authentication, the per-request workspace context, and — most importantly — the structural isolation (row-level security + query guards + tests) that every later workspace-owned table inherits. It is the first spec to create real workspace-owned tables.

### User Story 1 - Sign in and stay signed in securely (Priority: P1)

A human operator signs in with email + password and receives a short-lived access credential plus a longer-lived refresh credential; when the access credential expires they exchange the refresh credential for a new pair; on sign-out their session is revoked. Sign-in is rate-limited and never reveals which factor was wrong.

**Why this priority**: Authentication is the entry point to the entire API; nothing else can be exercised by a human without it.

**Independent Test**: Sign in with valid credentials → receive access + refresh credentials; exchange the refresh credential → receive a new pair and the old refresh credential no longer works; sign out → the session is revoked; repeated bad sign-ins are throttled.

**Acceptance Scenarios**:

1. **Given** a valid account, **When** the operator signs in with correct credentials, **Then** they receive a short-lived access credential and a refresh credential.
2. **Given** repeated failed sign-in attempts for an account or from a source address, **When** the threshold is exceeded, **Then** further attempts are rate-limited with progressive backoff, and every failure returns a uniform error that does not disclose whether the email or the password was wrong.
3. **Given** a valid refresh credential, **When** it is exchanged, **Then** a new access + refresh pair is issued and the presented refresh credential is rotated (invalidated).
4. **Given** a refresh credential that has already been rotated, **When** it is presented again, **Then** it is rejected.
5. **Given** two simultaneous exchanges of the same refresh credential, **When** they race, **Then** at most one succeeds (atomic rotation).
6. **Given** a signed-in session, **When** the operator signs out, **Then** the refresh credential is revoked and can no longer be exchanged.

### User Story 2 - Authenticate machine clients with scoped API keys (Priority: P1)

A workspace administrator issues an API key scoped to specific capabilities; the full secret is shown only once at creation; machine clients present the key to authenticate; the administrator can list keys (without the secret) and revoke a key, after which it authenticates nothing. Key usage is tracked without a database write on every request.

**Why this priority**: API keys are how all non-human integrations authenticate; they are co-equal P1 with human login for a machine-first, API-first product.

**Independent Test**: Create a key with a subset of scopes → the full secret is returned once; authenticate a request with it → succeeds and is limited to its scopes; list keys → secrets are not shown; revoke the key → subsequent authentication fails.

**Acceptance Scenarios**:

1. **Given** an authenticated administrator, **When** they create an API key with a chosen set of scopes, **Then** the full secret is returned exactly once and never retrievable again; only a non-secret prefix and metadata are stored in retrievable form.
2. **Given** a valid API key, **When** a machine client authenticates with it, **Then** the request is authenticated into the key's workspace and limited to the key's scopes; a request requiring a scope the key lacks is refused.
3. **Given** an existing key, **When** the administrator lists keys, **Then** each key's prefix, name, scopes, status, and last-used indicator are shown but the secret is not.
4. **Given** a key, **When** it is revoked, **Then** any subsequent authentication with it fails.
5. **Given** many authenticated requests with the same key, **When** usage is recorded, **Then** the "last used" indicator is updated at most once per key per minute (no per-request database write).

### User Story 3 - Every request is confined to exactly one workspace (Priority: P1)

Every authenticated request resolves a single workspace context, and all data access is confined to that workspace both by application query scoping AND by database row-level security — so even a query that forgets to filter by workspace cannot return another workspace's rows.

**Why this priority**: Multi-tenant isolation is a non-negotiable safety property; a single leak is a data breach. It must be structural from the very first workspace-owned table.

**Independent Test**: With two workspaces populated, confirm a request in workspace A cannot read or write workspace B's rows; confirm that even a deliberately unscoped query returns zero of B's rows because row-level security fails closed; confirm the CI guard rejects an unscoped query on a workspace-owned model.

**Acceptance Scenarios**:

1. **Given** an authenticated request, **When** it is processed, **Then** exactly one workspace context is resolved for it and applied to the data layer for the duration of that request's transaction.
2. **Given** two workspaces with data, **When** a request authenticated to workspace A attempts to read or write a row belonging to workspace B, **Then** the operation returns/affects zero of B's rows.
3. **Given** a query that omits the workspace filter, **When** it runs on a workspace-owned table, **Then** row-level security still returns zero rows from other workspaces (defense-in-depth), and zero rows when no workspace context is set (fail closed).
4. **Given** the codebase, **When** the continuous-integration guard runs, **Then** it fails if any unscoped fetch-by-id or unscoped select on a workspace-owned model is introduced.

### User Story 4 - Suspending a workspace or user cuts off access promptly (Priority: P2)

When a workspace or user is suspended, their existing credentials stop working within a bounded, predictable delay — without the system performing a database lookup on every request.

**Why this priority**: Operational safety (offboarding, abuse response) matters, but it builds on the authentication and context machinery.

**Independent Test**: Suspend a workspace; confirm that within the status-cache TTL, requests authenticated to that workspace are rejected; confirm normal requests do not incur a per-request status database read.

**Acceptance Scenarios**:

1. **Given** an active session/key, **When** its workspace (or user) is suspended, **Then** within the status-cache TTL its credentials are rejected.
2. **Given** normal operation, **When** requests are authenticated, **Then** user/workspace status is served from a short-lived cache rather than a database read per request.

### Edge Cases

- What happens on a login with a correct email but wrong password vs. an unknown email? Both return the same uniform failure (no factor disclosure) and both count toward rate limits.
- What happens when a refresh credential is expired (not just rotated)? It is rejected the same as an invalid one.
- What happens when an API key's workspace is suspended? The key authenticates nothing within the status-cache TTL, even before its own status changes.
- What happens when a SUPER_ADMIN (no fixed workspace) acts? The user's `workspace_id` may be absent; such an actor must still resolve an explicit workspace context per request (it is not a wildcard bypass of row-level security).
- What happens if the workspace context is never set for a transaction touching a workspace-owned table? Row-level security fails closed — zero rows — rather than exposing all rows.
- What happens when two API keys share a prefix? Lookup by prefix must still resolve to the correct key by verifying the full-secret hash; a prefix collision must not authenticate the wrong key.
- What happens to `last_used_at` updates under bursty traffic? They are coalesced (buffered) so at most one write occurs per key per minute; correctness-critical counters must not be evicted from the cache.
- What happens when the status/rate-limit cache is briefly unavailable? Login rate-limiting and status checks must fail safe (deny/challenge) rather than silently allowing unlimited attempts.
- What happens during the pre-authentication lookup, before any workspace context exists (resolving a user by email / an API key by prefix)? That path may require crossing the row-level-security boundary to find the credential, but that elevated access MUST be confined to the credential lookup and unreachable by request-serving business-data queries (FR-020a) — it is not a general isolation bypass.
- What happens on a login attempt for a non-existent email? The system performs an equivalent credential-verification cost (dummy verification) so timing does not reveal whether the email exists (FR-006).

## Requirements *(mandatory)*

### Functional Requirements

**Identity & data**
- **FR-001**: The system MUST provide a `workspaces` tenant-root entity (unique slug, status, name, timestamps) that is the isolation boundary; it is not itself scoped by `workspace_id`.
- **FR-002**: The system MUST provide a `users` entity (unique email, password hash, role, status, nullable `workspace_id` so a cross-workspace SUPER_ADMIN can exist, timestamps).
- **FR-003**: The system MUST support the roles SUPER_ADMIN, WORKSPACE_ADMIN, READ_ONLY as string-backed, application-validated values.
- **FR-004**: `users` and `api_keys` MUST be workspace-owned tables created with row-level security enabled in the same migration that creates them; `workspaces` and `refresh_tokens` follow their documented isolation model (workspaces = tenant root; refresh_tokens reached only via their owning user).

**Passwords & login**
- **FR-005**: Passwords MUST be stored using a password key-derivation function (argon2id, or bcrypt) with a per-user salt; plaintext passwords MUST never be stored or logged.
- **FR-006**: Sign-in MUST verify credentials and, on success, issue a short-lived access credential and a refresh credential; on failure it MUST return a uniform error that does not reveal which factor was wrong. The failure path MUST also avoid a timing side-channel that discloses account existence — an unknown email MUST incur an equivalent credential-verification cost (e.g. a dummy password-hash verification) so response timing does not distinguish "unknown email" from "wrong password".
- **FR-007**: Sign-in MUST be rate-limited per account AND per source address using shared counters with progressive backoff; the rate-limit state MUST live in the correctness-critical (non-evicting) cache.

**Refresh tokens**
- **FR-008**: Refresh credentials MUST be stored only as hashes; the raw value is never persisted.
- **FR-009**: Each refresh exchange MUST rotate the credential (issue a new one and invalidate the presented one); presenting an already-rotated credential MUST be rejected.
- **FR-010**: Refresh rotation MUST be atomic under concurrency: of two simultaneous exchanges of the same credential, at most one succeeds.
- **FR-011**: Refresh credentials MUST expire, and expired credentials MUST be rejected; sign-out MUST revoke the credential so it can no longer be exchanged.

**API keys**
- **FR-012**: API keys MUST be high-entropy random secrets stored as a fast (SHA-256) hash plus a short non-secret prefix for lookup/display; the full secret MUST be shown only once at creation and never retrievable afterward. (A password KDF MUST NOT be used for API keys.)
- **FR-013**: API keys MUST carry a set of scopes drawn from the defined scope vocabulary; an authenticated API-key request MUST be limited to its scopes, and a request needing a scope the key lacks MUST be refused. (Scope-refusal is enforced by the shared authentication dependency's scope-guard and is proven here at the dependency/unit level; the first *scope-gated resource endpoints* that exercise it end-to-end arrive with the resource APIs in SPEC-04+. The API-key management endpoints in this spec are role-gated administrative operations, not scope-gated.)
- **FR-014**: API keys MUST support revocation; a revoked (or non-active) key MUST authenticate nothing.
- **FR-015**: API-key `last_used_at` MUST be tracked in a throttled manner — at most one write per key per minute, buffered in the cache — never a database write per request.
- **FR-016**: API-key lookup MUST resolve by prefix and then verify the full-secret hash, so a prefix collision cannot authenticate the wrong key.

**Workspace context & isolation**
- **FR-017**: Every authenticated request MUST resolve exactly one workspace context and apply it to the data layer for the duration of that request's transaction (transaction-scoped, pooler-safe), so row-level security sees the correct workspace.
- **FR-018**: The system MUST provide workspace-scoped repository/query helpers that require a workspace context and forbid fetching a workspace-owned row by id alone.
- **FR-019**: Row-level security policies on workspace-owned tables MUST deny cross-workspace rows even when an application-level filter is missing, and MUST fail closed (zero rows) when no workspace context is set.
- **FR-020**: The system MUST include a continuous-integration guard that fails the build when an unscoped fetch-by-id or unscoped select on a workspace-owned model is introduced.
- **FR-020a**: The pre-authentication credential-lookup path (which necessarily runs before any workspace context can exist — resolving a user by email or an API key by prefix) MUST be limited to resolving the credential itself. Any elevated privilege it requires to read across workspaces (e.g. a row-level-security bypass) MUST be confined to that credential lookup and MUST NOT be reachable by request-serving/business-data queries, which always run under the normal workspace-scoped, non-bypassing path.
- **FR-021**: The system MUST include automated tests proving cross-workspace reads and writes are blocked, including the case where the application filter is omitted (row-level security still blocks).

**Status & endpoints**
- **FR-022**: User and workspace status MUST be served from a short-lived cache so a suspended workspace or user's credentials stop authenticating within the cache TTL, without a per-request status database read.
- **FR-023**: The system MUST expose the endpoints: sign-in, refresh, sign-out, and API-key create / list / revoke, under the `/v1` base path; only these auth and API-key endpoints are in scope for this feature.
- **FR-024**: Access credentials MUST be short-lived and carry the identity and workspace/role claims needed to resolve context and authorize scoped access without a database read on the hot path (beyond the cached status check).

### Key Entities *(include if data involved)*

- **Workspace**: The tenant root and isolation boundary (unique slug, status). All workspace-owned data belongs to exactly one workspace.
- **User**: A human principal with an email, hashed password, role, and status; optionally bound to a workspace (SUPER_ADMIN may be unbound).
- **Refresh token**: A hashed, expiring, rotatable, revocable credential owned by a user, used to obtain new access credentials.
- **API key**: A workspace-owned machine credential: a one-time-shown high-entropy secret stored as a fast hash + prefix, with scopes, status, and a throttled last-used indicator.
- **Scope**: A string-backed capability (e.g. read/write per resource family, jobs:run) constraining what an API-key request may do.
- **Workspace context**: The single workspace resolved per request and applied to the data layer (row-level security setting) for that request's transaction.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Valid sign-in returns an access + refresh credential 100% of the time; invalid sign-in returns a uniform error with no factor disclosure 100% of the time.
- **SC-002**: A refresh credential works exactly once — after rotation, reuse is rejected 100% of the time; under two concurrent exchanges, at most one succeeds.
- **SC-003**: After sign-out or revocation, the affected credential authenticates 0 subsequent requests.
- **SC-004**: A revoked or non-active API key authenticates 0 requests; a valid key is confined to its scopes (0 out-of-scope operations succeed).
- **SC-005**: In a two-workspace test, 0 rows of workspace B are ever read or written by a workspace-A request — including when the application filter is deliberately omitted (row-level security blocks) and when no workspace context is set (fail closed, 0 rows).
- **SC-006**: The CI guard fails the build on 100% of introduced unscoped fetch-by-id / unscoped selects on workspace-owned models.
- **SC-007**: Suspending a workspace or user causes its credentials to be rejected within the status-cache TTL 100% of the time; steady-state authenticated requests perform 0 per-request status database reads (status served from cache).
- **SC-008**: `last_used_at` is written at most once per key per minute regardless of request volume (0 per-request writes).
- **SC-009**: Sign-in rate limiting engages after the configured threshold per account and per source address 100% of the time, with no factor disclosure in the throttled response.

## Assumptions

- Access credentials are short-lived signed tokens (JWT) carrying identity + workspace/role claims; refresh credentials are opaque, hashed server-side. Exact lifetimes/algorithms are implementation details chosen at planning time within the security constraints above.
- The correctness-critical cache (rate-limit counters, status cache, last-used buffer) is the non-evicting cache instance per PROJECT_SPEC §4; it is assumed available. When briefly unavailable, security-sensitive checks fail safe (deny/challenge).
- This feature creates only the identity/auth tables and applies row-level security to the first workspace-owned tables (`users`, `api_keys`). Products/variants/competitors/matches and their endpoints are out of scope (SPEC-04+). The `workspaces.default_scrape_profile_id` / `default_access_policy_id` columns exist as plain nullable identifiers with no foreign key yet (their targets are later specs).
- Bootstrapping the first workspace/SUPER_ADMIN (seed/admin path) is assumed to occur via the migration/seed or an administrative path; a public self-service signup flow is out of scope.
- Row-level security uses the SPEC-02 `emit_rls_policy` helper (ENABLE + FORCE + fail-closed `NULLIF(...)` predicate) and the transaction-scoped `SET LOCAL app.workspace_id` mechanism from SPEC-02.
- Build/CI environment has no live PostgreSQL/Redis (no container engine here): DB/Redis-independent logic (password + token hashing and rotation logic, API-key generation/hash/scope checks, JWT encode/decode, RLS DDL render, workspace-scoped helper construction, the CI guard script itself) is fully unit-tested here; acceptance items requiring a live database (RLS row denial, cross-workspace blocking, migration run) or live cache (rate-limit/status-TTL/last-used behavior) are authored and validated on a PostgreSQL/Redis-capable host.
