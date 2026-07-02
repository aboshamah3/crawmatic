# Quickstart & Validation: Auth, API Keys & Workspace Isolation

How to validate this feature. Split into **(A) runs here** (no live services — pure logic, DDL render, CI guard) and **(B) deferred to a PostgreSQL + Redis host** (live RLS/rate-limit/status/migration/integration). Mirrors the SPEC-01/02 split; the no-Docker build env validates everything DB/Redis-independent.

## Prerequisites

- `uv sync` (installs new deps: `argon2-cffi`, `pyjwt`, `redis`). Run `uv lock` after editing `libs/shared/app_shared/pyproject.toml`.
- Config: set `JWT_SECRET` (required). Optional tunables default sensibly: `ACCESS_TOKEN_TTL_SECONDS=900`, `REFRESH_TOKEN_TTL_SECONDS=2592000`, `STATUS_CACHE_TTL_SECONDS=30`, `LOGIN_RATE_LIMIT_MAX_ATTEMPTS=5`, `LOGIN_RATE_LIMIT_WINDOW_SECONDS=60`, `API_KEY_LAST_USED_THROTTLE_SECONDS=60`. For live isolation testing set `AUTH_DATABASE_URL` to the `crawmatic_auth` (BYPASSRLS) role.

---

## A. Validates in THIS environment (no DB/Redis)

Run the unit suite:

```bash
uv run pytest tests/unit -q
```

Expected coverage:

| Check | Test | FR/SC |
|-------|------|-------|
| Password hash ≠ plaintext; verify round-trip; wrong pw fails; needs_rehash | `test_passwords.py` | FR-005 |
| API-key gen entropy, prefix parse, sha256 hash, constant-time verify, **prefix-collision safety** | `test_api_key_security.py` | FR-012/FR-016 |
| Refresh gen/hash; rotation predicate (rotated/expired/revoked → rejected) | `test_refresh_tokens.py` | FR-008/FR-009/FR-011 |
| JWT encode/decode round-trip; expired → reject; bad signature → reject | `test_jwt.py` | FR-024 |
| Scope vocabulary; `has_scopes`; out-of-scope refused; invalid scope rejected | `test_scopes.py` | FR-013 |
| Identity model shapes; `users.workspace_id` nullable; enums render as strings | `test_identity_models.py` | FR-002/FR-003 |
| `emit_rls_policy` render for `users`+`api_keys` (fail-closed predicate present) | `test_rls_identity.py` | FR-004/FR-019 |
| Scoped helpers require `workspace_id`; unscoped access on workspace-owned model raises | `test_repository_scoping.py` | FR-018 |
| CI guard flags planted `session.get(User)` / unscoped `select(ApiKey)`; passes clean code | `test_workspace_scoping_guard.py` | FR-020/SC-006 |
| Uniform login error: unknown-email vs wrong-password identical | `test_uniform_login_error.py` | FR-006/SC-001 |
| Offline migration renders 4 tables + RLS; single head | `test_migration_offline_auth.py` | FR-004/FR-023 |
| `app_shared` imports no fastapi/scrapy/twisted/playwright (new submodules) | `test_import_boundaries.py` | Principle I |

Guard + single-head, DB-independent:

```bash
uv run python scripts/check_workspace_scoping.py     # exit 0 on a clean tree
bash scripts/check_single_head.sh                    # exactly 1 head
uv run alembic upgrade head --sql | head -60         # renders the 4 CREATE TABLEs + RLS offline
```

---

## B. Deferred — requires PostgreSQL + Redis (authored, run on a capable host)

Setup (once): create two DB roles — `crawmatic_app` (normal, no BYPASSRLS → `DATABASE_URL`) and `crawmatic_auth` (`BYPASSRLS` → `AUTH_DATABASE_URL`); run the migration; seed the first admin:

```bash
uv run alembic upgrade head                          # online, direct-to-Postgres (MIGRATION_DATABASE_URL)
BOOTSTRAP_ADMIN_EMAIL=admin@example.com BOOTSTRAP_ADMIN_PASSWORD=... \
  uv run python scripts/seed_bootstrap.py            # first workspace + SUPER_ADMIN
uv run pytest tests/integration -q                   # live suite (skips if no PG/Redis)
```

| Scenario | Test | FR/SC |
|----------|------|-------|
| ws-A request reads/writes **0** of ws-B's rows; unscoped query still 0 (RLS); no-context → 0 (fail closed) | `test_rls_cross_workspace.py` | FR-019/FR-021/SC-005 |
| login → refresh (rotate) → reuse rejected → 2 concurrent rotations, exactly 1 wins → logout revokes | `test_auth_flow.py` | FR-006/FR-009/FR-010/FR-011/SC-002/SC-003 |
| api-key create (secret shown once) → authenticate → scope-limited (out-of-scope refused) → list (no secret) → revoke → denied | `test_api_key_flow.py` | FR-012/FR-013/FR-014/SC-004 |
| login rate limit engages per-account + per-source with backoff; fail-safe deny on cache down | `test_rate_limit.py` | FR-007/SC-009 |
| suspend workspace/user → rejected within status-cache TTL; steady state **0** per-request status DB reads | `test_status_cache.py` | FR-022/SC-007 |
| `last_used_at` ≤1 write/key/min under burst | `test_last_used_throttle.py` | FR-015/SC-008 |
| online migration `upgrade head` creates 4 tables + RLS enabled; seed creates workspace + SUPER_ADMIN | live migration test | FR-004/FR-023 |

---

## Manual smoke (live host)

```bash
# 1. Log in
curl -sX POST localhost:8000/v1/auth/login -d '{"email":"admin@example.com","password":"..."}' -H 'content-type: application/json'
#    -> {access_token, refresh_token, token_type, expires_in}

# 2. Create a scoped API key (as WORKSPACE_ADMIN, with X-Workspace-Id if SUPER_ADMIN)
curl -sX POST localhost:8000/v1/api-keys -H "authorization: bearer <access>" \
  -d '{"name":"n8n","scopes":["products:read","jobs:read"]}' -H 'content-type: application/json'
#    -> includes "api_key" ONCE

# 3. Use the API key
curl -s localhost:8000/v1/api-keys -H "authorization: bearer <ck_...>"   # limited to its scopes

# 4. Refresh, then logout
curl -sX POST localhost:8000/v1/auth/refresh -d '{"refresh_token":"<r>"}' -H 'content-type: application/json'
curl -sX POST localhost:8000/v1/auth/logout  -d '{"refresh_token":"<r2>"}' -H 'content-type: application/json'
```

See `contracts/` for exact request/response shapes and guarantees.
