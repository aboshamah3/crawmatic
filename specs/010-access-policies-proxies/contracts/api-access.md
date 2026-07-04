# Contract: CRUD API (`apps/api/app/routers/{proxy_providers,access_policies,domain_access_rules}.py`)

Follows `routers/scrape_profiles.py` (dual-scope) and the tenant-CRUD routers: auth via
`get_current_principal` (session already has `set_workspace_context`), scope-gated via
`require_scopes(...)`, cursor pagination, `{"error":{"code":...,"message":...}}` envelope.
All under `/v1`.

## New scopes (`app_shared/security/scopes.py`)

`proxy_providers:read/write`, `access_policies:read/write`, `domain_rules:read/write`.

## Endpoints

### `/v1/proxy-providers` (dual-scope; scope `proxy_providers:*`)
- `POST` — create (own workspace). Body: `name, type, base_url, username?, password?,
  country_code?, status?, monthly_budget_limit?`. `base_url` → `validate_competitor_url`
  (422 `UNSAFE_URL`). `password` (plaintext) → `encrypt_secret` → `(password_encrypted,
  password_key_version)`; **response never includes it**. Duplicate name → 409.
- `GET` — list via `visible_providers_select` (own + global), cursor-paginated.
- `GET /{id}` — via `visible_providers_select` (own or global); else 404.
- `PATCH /{id}` — own-only (`owned_provider_get`; global/other-ws → 404). A new `password` is
  re-encrypted; `password: null` clears it (sets both columns null); omitted → unchanged.
- `DELETE /{id}` — own-only hard delete.
- **Response model** `ProxyProviderResponse`: all columns **except** `password_encrypted`/
  `password_key_version`; instead a boolean `has_password`. (SC-003 — no plaintext, no
  ciphertext leak.)

### `/v1/access-policies` (dual-scope; scope `access_policies:*`)
- `POST`/`GET`/`GET{id}`/`PATCH`/`DELETE` — same dual-scope pattern. Body/response carry the
  full FR-001 field set. `provider_id`, when set, → `assert_provider_assignable`
  (422 `WORKSPACE_MISMATCH` / 404). Validation: `max_retries≥0`, `timeout_ms>0`, ceilings>0.

### `/v1/domain-access-rules` (tenant-only; scope `domain_rules:*`)
- `POST`/`GET`/`GET{id}`/`PATCH`/`DELETE` — standard `scoped_select`/`scoped_get` CRUD.
  `competitor_id` must be in the caller's workspace (422 on cross-workspace, 404 dangling —
  reuse `app_shared.catalog.consistency`). `access_policy_id` → `assert_policy_assignable`.
  Duplicate `(competitor_id, domain, url_pattern)` → 409.

## Register

`apps/api/app/main.py`: `app.include_router(...)` for the three routers.

## Acceptance (skip-clean integration)

- Round-trip: a created policy returns every strategy/retry/rate field intact (Scenario US1-1).
- Proxy password: create with a password → response has `has_password=true` and **no**
  password field; DB stores ciphertext (not equal to plaintext); a second GET never exposes it
  (Scenario US1-2, SC-003).
- Cross-workspace: workspace B cannot read/patch/delete workspace A's tenant rows; B sees
  global providers/policies read-only and cannot mutate them (Scenario US1-3, SC-005).
- No-context query → zero tenant rows (globals still visible for the dual-scope tables)
  (Scenario US1-4).
- `base_url` to a private/loopback/metadata host or with `user:pass@` → 422 `UNSAFE_URL`
  (Scenario US1-5).
