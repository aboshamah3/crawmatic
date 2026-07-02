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
