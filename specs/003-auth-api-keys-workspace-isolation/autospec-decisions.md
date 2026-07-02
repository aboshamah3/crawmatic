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
