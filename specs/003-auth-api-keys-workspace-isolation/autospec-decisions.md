# Autospec Decisions — SPEC-03 Auth, API Keys & Workspace Isolation

Feature directory: `specs/003-auth-api-keys-workspace-isolation`
Master doc: `/srv/crawmatic/PROJECT_SPEC.md`

## specify

- [specify] Q: Any clarifications needed? → A: No NEEDS CLARIFICATION markers; requirements fully specified by the doc (source: §22 Core identity/auth tables + roles + scopes, §24 auth/api-key endpoints, §32 Workspace Isolation, §33 Security & Secrets, §35 subsection "03").
- [specify] Q: Feature short-name / directory? → A: `specs/003-auth-api-keys-workspace-isolation` (sequential; matches doc §5 dir name).
- [specify] Q: Scope — which tables/endpoints? → A: workspaces, users, refresh_tokens, api_keys + auth (login/refresh/logout) + api-keys (create/list/delete) endpoints ONLY. Products/competitors/matches etc. are SPEC-04+ (source: §35 "03" vs "04"/"05"; §24 endpoint list).
- [specify] Q: Password hashing? → A: argon2id (or bcrypt), per-user salt, uniform login failure, per-account+per-source rate limiting (source: §33).
- [specify] Q: API key storage? → A: high-entropy secret, SHA-256 hash + prefix, shown once, scopes, revocation, throttled last_used_at (≤1 write/key/min via Redis) (source: §33 — fast hash correct for high-entropy secrets).
- [specify] Q: Refresh token semantics? → A: hashed at rest, rotate on use, reject rotated reuse, atomic under concurrency, expire, logout/revoke (source: §33).
- [specify] Q: Isolation model? → A: users + api_keys are workspace-owned → RLS in the same migration (SPEC-02 emit_rls_policy NULLIF fail-closed + SET LOCAL app.workspace_id transaction-scoped); workspaces = tenant root (no workspace_id); refresh_tokens reached via owning user. Plus workspace-scoped repo helpers, workspace dependency per route, cross-workspace tests, CI unscoped-query guard (source: §32).
- [specify] Q: Status cache? → A: Redis-backed user/workspace status cache; suspension takes effect within TTL; no per-request status DB read (source: §35 + §4).
- [specify] Q: Live-Postgres/Redis acceptance given no daemon here? → A: DB/Redis-independent logic fully unit-tested here (hashing, token rotation logic, key gen/hash/scope, JWT, RLS DDL render, repo helper, CI guard); live-DB/Redis items (RLS row denial, cross-workspace blocking, rate-limit/status-TTL) authored + deferred to a PG/Redis host (source: no-docker-daemon constraint).

## clarify

No questions relayed to the user — doc + SPEC-01/02 foundation resolved all ambiguities; numeric lifetimes are config-tunable defaults. Doc-resolved clarifications in spec.md `## Clarifications` (Session 2026-07-02):

- [clarify] Q: Password hash? → A: argon2id + per-user salt (bcrypt fallback) (source: §33).
- [clarify] Q: Token lifetimes? → A: access ~15min, refresh ~30d, env-tunable (source: §33/§24; defaults).
- [clarify] Q: Status-cache TTL? → A: ~30-60s env-tunable; SC-007 asserts "within configured TTL" (source: §35; default).
- [clarify] Q: Rate-limit thresholds? → A: per-account+per-source, progressive backoff, config-tunable (source: §24/§33; defaults).
- [clarify] Q: First workspace/SUPER_ADMIN creation? → A: seed/bootstrap path, no public signup (source: §37/§38).
- [clarify] Q: Which tables workspace-owned (RLS now)? → A: users + api_keys; workspaces=tenant root; refresh_tokens via owning user (source: §22/§32).
- [clarify] Q: JWT claims? → A: identity + workspace_id + role/scope so no hot-path DB read beyond cached status (source: §32/§35).
- [clarify] Q: Live-DB/Redis items here? → A: unit-test DB-independent logic; defer live items to PG/Redis host (source: no-docker constraint).

## plan (opus subagent)

- [plan] Pinned libs → argon2-cffi (passwords), pyjwt HS256 (access JWT), stdlib secrets/hashlib/hmac (API-key + refresh SHA-256 + constant-time verify), redis-py. Crypto/models/helpers in app_shared (framework-agnostic); FastAPI dep+routers in apps/api.
- [plan] Tokens → stateless access JWT (sub/workspace_id/role/scopes/exp ~15m); opaque refresh stored SHA-256 only (~30d).
- [plan] Atomic refresh rotation → single `UPDATE refresh_tokens SET revoked_at=now() WHERE token_hash=:h AND revoked_at IS NULL AND expires_at>now() RETURNING` — exactly-once under concurrency, no session advisory locks (PgBouncer-safe); same statement rejects rotated-reuse/expiry/logout.
- [plan] RLS + nullable-workspace SUPER_ADMIN → emit_rls_policy unchanged on users+api_keys in creating migration; NULL-workspace SUPER_ADMIN rows never match fail-closed predicate (no bypass). **Pre-auth credential lookups (user-by-email, api-key-by-prefix) run via a dedicated `crawmatic_auth` BYPASSRLS role (AUTH_DATABASE_URL); request-serving `crawmatic_app` role NEVER bypasses RLS.** ⚠ analyze should scrutinize this BYPASSRLS role (Principle II).
- [plan] CI guard → scripts/check_workspace_scoping.py (stdlib ast scan of apps/+libs/); flags session.get(User/ApiKey) + unscoped select; imports workspace-owned set from app_shared.repository so sets can't drift; noqa + allowlist for false positives. Runnable here.
- [plan] Context → per-transaction `SELECT set_config('app.workspace_id', :wsid, true)` (bind-safe, pooler-safe).
- [plan] Redis (noeviction) → per-account+per-source login rate limit (progressive backoff, fail-safe deny); user/workspace status cache (short TTL, 0 per-request status DB read, fail-safe deny); last-used `SET NX EX` gate (≤1 write/key/min).
- [plan] Bootstrap → idempotent scripts/seed_bootstrap.py (env-driven, direct migration connection); no public signup.
- [plan] Constitution Check → PASS (pre+post). Principle II satisfied structurally; VIII via Redis throttling. Artifacts: plan.md, research.md (D1-D10), data-model.md, quickstart.md, contracts/{api-auth,api-keys,security-passwords,security-tokens,security-jwt,security-scopes,security-cache,workspace-context,repository-scoping,ci-scoping-guard,migration-identity}.md.

## checklist

- [checklist] Q: focus/depth/audience? → A: security & isolation (10 focus areas); Rigorous depth; Reviewer pre-implementation gate. No user clarifying questions (args fully specified).
- Generated checklists/security.md (32 requirements-quality items).
- 2 gaps found + fixed before checking:
  1. Login TIMING side-channel (spec had uniform error but not uniform timing) → added to FR-006 + edge case (dummy verify on unknown email).
  2. Pre-auth RLS-bypass (plan's crawmatic_auth BYPASSRLS role) had no governing requirement → added FR-020a + edge case confining elevated access to credential resolution only, unreachable by request-serving queries.
- Completion: security.md 32/32 pass; requirements.md 16/16 pass. Implement gate CLEAR.

## tasks (opus subagent)

- 53 tasks (T001-T053), 8 phases (Setup, Foundational, US1 sign-in, US2 api-keys, US3 isolation, US4 suspension, Seed/Bootstrap, Polish). FR-001..FR-024 + FR-020a + SC-001..SC-009 coverage tables; Scope Boundary forbids products/competitors/matches. 6 DEFERRED (live PG/Redis). CI guard (SC-006) validated here. Documented forward dep T031→T038 (build order).

## analyze (inline, forked)

0 CRITICAL/HIGH → no user pause. Remediated actionable findings:

- [analyze] C1 (MEDIUM, auth correctness): get_auth_session silently falling back to DATABASE_URL breaks auth — under FORCE RLS a non-BYPASSRLS role returns 0 rows for pre-auth lookups → login/api-key auth silently fail closed → A: T009 now MUST fail fast with a clear error when AUTH_DATABASE_URL is unset (+ unit test), not silently return an authenticate-nobody session.
- [analyze] I1 (MEDIUM): scope enforcement (FR-013/SC-004) has no scope-gated endpoint in SPEC-03 (api-key CRUD is role-gated admin) → A: annotated FR-013 + T036 that scope refusal is proven at dependency/unit level here (T034/T045) and end-to-end when resource endpoints land in SPEC-04.
- [analyze] P1 (LOW): GET /v1/api-keys pagination → A: noted api-keys are low-volume; §24 cursor pagination deferred to SPEC-04 high-volume lists.
- [analyze] O1 (LOW): forward dep T031→T038 already documented in tasks Dependencies (build order: US1 primitives → US2 primitives → status_cache → deps.py → api-keys router). Implement follows this.
- [analyze] V1/E1/R1 (LOW): acceptable no-action — FR-021 live-only (no-DB env), AUTH_FAILED within §34 "e.g." vocab, single REDIS_URL is the noeviction instance per assumption.
- Only MEDIUM/LOW fixed (no CRITICAL/HIGH); C1 change localized (T009 + test) + clearly correct → full re-run not required. 100% FR/SC coverage retained.

## implement (sonnet subagents, 5 dependency-coherent groups)

47/53 tasks [X]; 6 DEFERRED (live PG/Redis). Suite: 185 passed, 24 skipped.
- A Setup+Foundational (T001-T013): deps, config, identity enums+models, migration 55da7d6d939d w/ RLS on users+api_keys, repository scoped helpers, AST CI guard, get_auth_session fail-fast (C1).
- B US1 sign-in (T014-T025): argon2id passwords + timing-uniform dummy_verify (measured 1.03 ratio), tokens+atomic rotation SQL, JWT, rate limiter, auth router.
- C US2 + auth seam (T028-T035,T037,T038,T031,T032): api-key primitives (collision-safe), scopes, last-used throttle, status_cache (fail-safe deny), deps.py single auth seam, api-keys router.
- D US3 isolation tests + US4 (T039-T045,T047): identity/RLS/repository/guard/deps/status-cache unit tests; guard catches planted violations.
- E Seed + Polish (T049,T051-T053): seed_bootstrap, CI-guard doc, validation gate, deferred-run docs.

### DEFERRED — live Postgres/Redis (authored + skip cleanly; not gaps)
T026 auth flow, T027 login rate limit, T036 api-key flow, T046 cross-workspace RLS denial, T048 status-cache TTL/zero-read, T050 seed+online migration. Run on a PG/Redis host to close SC-001..SC-005/SC-007/SC-009 live proofs + FR-021.

## converge (opus subagent)

- Result: CONVERGED — no new tasks (tasks.md unchanged). Rigorous static sweep all PASS:
  guard exit 0 + catches planted violation; one alembic head; offline render = 4 identity tables + 6 RLS statements (ENABLE+FORCE+NULLIF) on users AND api_keys; all 4 sanctioned BYPASSRLS pre-auth lookups noqa-annotated, no unannotated unscoped access; app_shared fastapi/scrapy-free; SUPER_ADMIN requires explicit role-authorized workspace (not RLS bypass); get_auth_session fail-fast; login always pays argon2 cost; atomic UPDATE...RETURNING rotation; Base.metadata = _smoke_foundation + 4 identity tables only (no native enums, no SPEC-04 tables).
- FR-001..FR-024 + FR-020a built/verified here (FR-021 live-deferred); SC-006 executes green here; other SC live-proofs in the 6 deferred suites. Converged cycle 1, no implement re-loop.
