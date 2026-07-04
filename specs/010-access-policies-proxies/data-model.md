# Phase 1 Data Model: Access Policies, Proxies & Request Attempts

Three new tables in `libs/shared/app_shared/models/access.py`, exact PROJECT_SPEC §22 shapes.
Enum-like columns use `enum_column` (app-validated `VARCHAR`, never a DB-native enum).
Timestamps are `TZDateTime` (`TIMESTAMPTZ`, naive rejected) via `TimestampMixin`. Soft
references (`provider_id`, `access_policy_id`, `competitor_id`) are plain UUIDs with **no
FK** — §22's soft-reference philosophy (tolerate a disabled/deleted referent; readers must
degrade gracefully). Only `workspace_id` gets a real FK (the RLS anchor).

`request_attempts` is **not** redefined here — it already exists (SPEC-07,
`models/observations.py`, `RequestAttempt`). See research D1. Its shape is reproduced at the
end for reference only.

New enums added to `app_shared.enums` (all `StrEnum` → app-validated `VARCHAR`, research D9):

- `AccessStrategy`: `DIRECT_ONLY, DIRECT_THEN_PROXY, PROXY_FIRST, RESIDENTIAL_ONLY,
  BROWSER_FALLBACK`
- `ProxyType`: `DATACENTER, RESIDENTIAL, MOBILE`
- `ProxyProviderStatus`: `ACTIVE, DISABLED`

Reused enums (already declared, no change): `AccessMethod`
(`DIRECT_HTTP/DIRECT_HTTP_RETRY/PROXY_HTTP/PLAYWRIGHT_PROXY`), `ScrapeErrorCode` (full §34
vocabulary incl. `PROXY_FAILED/RATE_LIMITED/HTTP_429/HTTP_403/TIMEOUT/DNS_ERROR/BLOCKED/
LIMIT_REACHED/UNKNOWN_ERROR`).

---

## Entity: ProxyProvider (`proxy_providers`) — DUAL-SCOPE

A proxy endpoint with encrypted credentials. **Dual-scope** (`workspace_id IS NULL` = global
system default readable by all, writable by none via tenant path — SPEC-06 pattern, research
D2). `Base + TimestampMixin` (**not** `WorkspaceScopedBase`); **not** in
`WORKSPACE_OWNED_MODELS`; RLS via `emit_global_readable_rls_policy`.

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | UUID (uuidv7) | no | PK |
| `workspace_id` | UUID | yes | indexed; FK → `workspaces.id`; `NULL` = global default; RLS column |
| `name` | Text | no | unique per scope (partial unique indexes, below) |
| `type` | `ProxyType` VARCHAR | no | DATACENTER / RESIDENTIAL / MOBILE |
| `base_url` | Text | no | SSRF-validated at save time (`validate_competitor_url`, FR-005) |
| `username` | Text | yes | proxy auth username (not secret) |
| `password_encrypted` | Text | yes | Fernet ciphertext; **never** returned in an API response (FR-003, SC-003) |
| `password_key_version` | Integer | yes | which keyring version encrypted `password_encrypted` (FR-003 rotation); non-null iff `password_encrypted` is |
| `country_code` | Text | yes | preferred exit country (e.g. `US`) |
| `status` | `ProxyProviderStatus` VARCHAR | no | ACTIVE / DISABLED; default ACTIVE |
| `monthly_budget_limit` | Integer | yes | max proxied requests/month; enforced via Redis counter (FR-010), not row counts |
| `created_at` | TIMESTAMPTZ | no | `TimestampMixin` |
| `updated_at` | TIMESTAMPTZ | no | `TimestampMixin` |

**Constraints / indexes**
- FK: `fk_proxy_providers_workspace_id_workspaces` on `workspace_id`.
- Partial unique `uq_proxy_providers_workspace_id_name` on `(workspace_id, name)`
  `WHERE workspace_id IS NOT NULL` (per-tenant namespace).
- Partial unique `uq_proxy_providers_name_global` on `(name)` `WHERE workspace_id IS NULL`
  (single global namespace).
- Index `ix_proxy_providers_workspace_id`.

**Validation rules**
- `base_url` → `validate_competitor_url` (http/https, public host, no userinfo) on create &
  update (FR-005). `UnsafeUrlError` → `422 {"error":{"code":"UNSAFE_URL"}}`.
- `type`/`status` validated by `enum_column`.
- `password` (plaintext, request-only) → encrypted to `(password_encrypted,
  password_key_version)` on write; the ORM column never receives plaintext.

---

## Entity: AccessPolicy (`access_policies`) — DUAL-SCOPE

A named access strategy consulted by every scrape. **Dual-scope** (same as `proxy_providers`).

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | UUID (uuidv7) | no | PK |
| `workspace_id` | UUID | yes | indexed; FK; `NULL` = global default; RLS column |
| `name` | Text | no | unique per scope (partial unique indexes) |
| `strategy` | `AccessStrategy` VARCHAR | no | DIRECT_ONLY / DIRECT_THEN_PROXY / PROXY_FIRST / RESIDENTIAL_ONLY / BROWSER_FALLBACK |
| `provider_id` | UUID | yes | **soft ref** → `proxy_providers.id` (no FK); disabled/deleted tolerated (degrade per strategy) |
| `country_code` | Text | yes | preferred proxy country override |
| `use_proxy_on_first_attempt` | Boolean | no | default `false` |
| `use_proxy_on_retry` | Boolean | no | default `true` |
| `allow_browser_fallback` | Boolean | no | default `false`; signals `PLAYWRIGHT_PROXY` intent (SPEC-14 executes) |
| `max_retries` | Integer | no | default `2`; `0` → exactly one attempt (Edge Case) |
| `rotate_per_request` | Boolean | no | default `false`; proxy rotation vs sticky |
| `sticky_session` | Boolean | no | default `false` |
| `session_ttl_minutes` | Integer | yes | sticky-session lifetime |
| `max_requests_per_minute` | Integer | yes | policy's own ceiling (Redis, FR-011) |
| `max_requests_per_hour` | Integer | yes | policy's own ceiling |
| `max_requests_per_day` | Integer | yes | policy's own ceiling |
| `timeout_ms` | Integer | no | default `30000` |
| `created_at` | TIMESTAMPTZ | no | `TimestampMixin` |
| `updated_at` | TIMESTAMPTZ | no | `TimestampMixin` |

**Constraints / indexes**
- FK `fk_access_policies_workspace_id_workspaces`.
- Partial unique `uq_access_policies_workspace_id_name` `WHERE workspace_id IS NOT NULL`;
  partial unique `uq_access_policies_name_global` `WHERE workspace_id IS NULL`.
- Index `ix_access_policies_workspace_id`.

**Validation rules**
- `strategy` via `enum_column`. `max_retries ≥ 0`, `timeout_ms > 0`, ceilings (if set) `> 0` —
  enforced in the Pydantic schema (`ge=`/`gt=`), mirroring `scrape_profiles` validation.
- `provider_id`, when set, must be assignable (own+global visible) — `assert_*_assignable`
  cross-workspace guard (SPEC-06 pattern); a dangling id is tolerated at resolution time
  (soft ref) but rejected at assignment time via the API for a clean UX.

---

## Entity: DomainAccessRule (`domain_access_rules`) — TENANT-ONLY

A per-domain override binding a competitor + domain (optionally a URL pattern) to an access
policy. **Tenant-only** (`workspace_id NOT NULL`, research D2): `WorkspaceScopedBase` +
`emit_rls_policy` + in `WORKSPACE_OWNED_MODELS`.

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | UUID (uuidv7) | no | PK |
| `workspace_id` | UUID | no | `WorkspaceScopedBase`; indexed; FK; RLS column |
| `competitor_id` | UUID | no | **soft ref** → `competitors.id` (no FK); indexed |
| `domain` | Text | no | the matched host (e.g. `shop.example.com`) |
| `url_pattern` | Text | yes | optional pattern; a URL-pattern rule beats a domain-only rule (most specific wins) |
| `url_pattern_override` | Text | yes | optional per-rule URL rewrite/override |
| `access_policy_id` | UUID | no | **soft ref** → `access_policies.id` (no FK) |
| `max_concurrent_requests` | Integer | no | per-domain concurrency intent (cluster-wide enforcement is SPEC-11) |
| `max_requests_per_minute` | Integer | no | per-domain ceiling (Redis) |
| `cooldown_seconds` | Integer | no | per-domain cooldown gate (Redis `SET NX EX`) |
| `block_detection_rules` | JSONB | yes | optional block-detection config |
| `enabled` | Boolean | no | default `true`; a disabled rule is ignored → default policy applies (Edge Case) |
| `created_at` | TIMESTAMPTZ | no | `TimestampMixin` |
| `updated_at` | TIMESTAMPTZ | no | `TimestampMixin` |

**Constraints / indexes**
- FK `fk_domain_access_rules_workspace_id_workspaces`.
- Index `ix_domain_access_rules_workspace_id`.
- Index `ix_domain_access_rules_workspace_id_competitor_id_domain` on
  `(workspace_id, competitor_id, domain)` — the resolution lookup key (batch-load a
  competitor's enabled rules by domain).
- Unique `uq_domain_access_rules_workspace_id_competitor_id_domain_url_pattern` on
  `(workspace_id, competitor_id, domain, url_pattern)` so a domain+pattern pair is defined
  once (a NULL `url_pattern` is the domain-only rule; Postgres treats NULLs as distinct, so a
  partial-unique or `COALESCE` expression index is used to forbid two domain-only rules for
  the same domain — see migration contract).

**Validation rules**
- `access_policy_id` must be assignable (own+global visible) at write time.
- `competitor_id` must belong to the caller's workspace (cross-workspace → `422`), reusing the
  `app_shared.catalog.consistency` `assert_*`/`MissingReference`/`CrossWorkspaceReference`
  helpers already used by matches.
- `max_concurrent_requests ≥ 1`, `max_requests_per_minute ≥ 0`, `cooldown_seconds ≥ 0`.

---

## Reference only: RequestAttempt (`request_attempts`) — ALREADY EXISTS (SPEC-07)

Not created or altered by this spec (research D1). Monthly-partitioned by `created_at`,
composite `PRIMARY KEY (id, created_at)`, `WorkspaceScopedBase`, in `WORKSPACE_OWNED_MODELS`.
Columns: `id`, `workspace_id`, `created_at` (partition key), `scrape_job_id` (soft ref, null),
`match_id` (soft ref, indexed), `attempt_number`, `url`, `access_method` (`AccessMethod`),
`proxy_provider_id` (soft ref, null), `proxy_country`, `status_code`, `response_time_ms`,
`success`, `error_code` (`ScrapeErrorCode`), `error_message`. This spec only **populates**
`access_method`/`proxy_provider_id`/`proxy_country` correctly (previously hardcoded
`DIRECT_HTTP`) and emits one row per attempt including retries (FR-012/013/015).

---

## Registration checklist (mirrors SPEC-06/09)

- `app_shared/enums.py`: add `AccessStrategy`, `ProxyType`, `ProxyProviderStatus`.
- `app_shared/models/access.py`: declare `ProxyProvider`, `AccessPolicy` (dual-scope),
  `DomainAccessRule` (tenant).
- `app_shared/models/__init__.py`: import/re-export the three so Alembic `target_metadata`
  sees them.
- `app_shared/repository.py`: add **only** `DomainAccessRule` to `WORKSPACE_OWNED_MODELS`
  (the two dual-scope tables are deliberately excluded — they use `app_shared.access.repository`).
- `app_shared/security/scopes.py`: add the six new `Scope` members.
- Migration `<newrev>_access_policies_proxies_tables.py` (down_revision `e4a75b48360c`):
  create all three tables + indexes; `emit_global_readable_rls_policy` for
  `proxy_providers`/`access_policies`, `emit_rls_policy` for `domain_access_rules`.
