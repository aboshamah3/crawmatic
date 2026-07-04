---
description: "Dependency-ordered task list for SPEC-10 Access Policies, Proxies & Request Attempts"
---

# Tasks: Access Policies, Proxies & Request Attempts

**Input**: Design documents from `/specs/010-access-policies-proxies/`

**Prerequisites**: plan.md (required), spec.md (required), research.md (D1–D10), data-model.md, quickstart.md, contracts/ (9 files: models-access, migration-access, encryption, access-repository, policy-resolution, access-engine, budget-ceilings, spider-integration, api-access)

**Tests**: Included — spec.md, plan.md (Project Structure `tests/unit` + `tests/integration`) and quickstart.md enumerate the unit + live suites, matching the SPEC-05→09 convention. Every DB/Redis/Scrapy-**independent** behavior is unit-tested **here**: the pure `next_attempt` strategy × attempt-number × flag × budget matrix, the `assign_proxy` rotate/sticky/degrade cases, the effective-policy precedence chain (domain-rule > workspace > global; disabled fall-through; URL-pattern-beats-domain-only), the cache-codec round-trip, the Fernet keyring encrypt/decrypt/rotate + unknown-version raise, the Redis budget/ceiling/cooldown counter math against a fake redis, the ORM/RLS DDL render via offline `alembic upgrade head --sql`, the dual-scope vs tenant scoping guard, and the "no `request_attempts` scan" AST/grep guard. Live-stack tests (migration+RLS apply, dual-scope + tenant CRUD with password redaction, cross-workspace + no-context isolation, real-Redis budget/cooldown, spider proxy-assignment + attempt-row emission) are **authored and skip cleanly** where no Postgres/Redis/Scrapyd is reachable — no container engine in this build env (SPEC-05→09 deferred-verification pattern).

**Organization**: Tasks are grouped by user story (US1..US3) to enable independent implementation and testing. Shared blocking work is in Setup + Foundational.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: `[US1]`/`[US2]`/`[US3]` maps a task to a spec.md user story (Setup / Foundational / Polish carry no story label)
- Every task lists an exact repo-relative file path

## Path Conventions

Backend monorepo (uv workspace). Enums/config/scopes/the encryption helper/the pure engines/ORM models/repository registration live in `libs/shared/app_shared/`; the error classifier + spider extension in `libs/scrape-core/scrape_core/` and `apps/scrapers/price_monitor/`; the routers + schemas + resolution orchestrator in `apps/api/app/`; the migration at repo-root `alembic/versions/`; tests in `tests/unit/` and `tests/integration/`.

---

## Scope Boundary (read first)

**IN SCOPE — controlled access behavior over the existing SPEC-07 scrape path:**

- **3 new tables** (`libs/shared/app_shared/models/access.py`) in exactly two isolation shapes (research D2):
  - `proxy_providers` + `access_policies` — **dual-scope** (`workspace_id` nullable; `NULL` = global system default readable by all, writable by none through the tenant path). `Base + TimestampMixin` (NOT `WorkspaceScopedBase`), **excluded** from `WORKSPACE_OWNED_MODELS`, RLS via `emit_global_readable_rls_policy`, queried through the dedicated `app_shared.access.repository` (the SPEC-06 `scrape_profiles` pattern).
  - `domain_access_rules` — **tenant-only** (`workspace_id NOT NULL`): `WorkspaceScopedBase`, `emit_rls_policy`, **in** `WORKSPACE_OWNED_MODELS`, queried via `scoped_select`/`scoped_get`.
  - One new Alembic migration chaining onto the single head `e4a75b48360c`; soft references (`provider_id`, `access_policy_id`, `competitor_id`) are plain UUID **no-FK** (§22).
- **3 new enums** (append to `app_shared.enums`, `StrEnum` → `VARCHAR` via `enum_column`): `AccessStrategy`, `ProxyType`, `ProxyProviderStatus` (D9). `AccessMethod` + `ScrapeErrorCode` are **reused unchanged** (already carry every member — no widening migration).
- **Fernet credential encryption** (`app_shared/security/encryption.py`, D3): a versioned keyring (`encrypt_secret`/`decrypt_secret`/`reencrypt_secret`, `SecretDecryptionError`) built from `Settings.ENCRYPTION_KEYS` + `ENCRYPTION_PRIMARY_KEY_VERSION`; never falls back to plaintext; the plaintext password is never a response field (SC-003).
- **Two pure, framework-free engines** in `app_shared/access/` (stdlib only, exhaustively unit-testable): `engine.py` (`next_attempt` next-`AccessMethod` + `assign_proxy` provider/rotation/sticky, D6) and `resolution.py` (`select_domain_rule` + `resolve_effective_policy` precedence chain + Redis cache-key/codec, D5). Plus `budget.py` — Redis monthly proxy budget (`INCR`/`EXPIRE`) + per-min/hour/day ceilings + per-domain cooldown (`SET NX EX`), **never** a `request_attempts` scan (D7, FR-010).
- **Dual-scope query helpers** (`app_shared/access/repository.py`, D2): `visible_*`/`owned_*` selects + `assert_*_assignable` cross-workspace guards.
- **CRUD API** (D10): 3 routers (`proxy_providers`, `access_policies`, `domain_access_rules`) + schemas + orchestrator (`services/access_resolution.py`) + 6 new `Scope` members; dual-scope reads (`visible_*`), own-only writes (`owned_*`), password redaction (`has_password`).
- **Spider extension (US2 + US3 wiring finish, NOT a rebuild)**: extend `generic_price_spider.py` at its `load_targets`/`_request_for`/`errback`/`_build_result` seams to consume the resolved policy, set `request.meta['proxy']`, retry per policy, decrypt the proxy password off-reactor, and stamp each `ScrapeResult`'s `access_method`/`proxy_provider_id`/`proxy_country`/`attempt_number` (previously hardcoded `DIRECT_HTTP`). Enable Scrapy's built-in `HttpProxyMiddleware` in `settings.py`.

**OUT OF SCOPE (do NOT build — later specs / already done):**

- **`request_attempts` table/model/migration** — **ALREADY EXISTS** (SPEC-07: `RequestAttempt` in `libs/shared/app_shared/models/observations.py`, migration `2db33dea5e14`, monthly-partitioned from birth, composite `PRIMARY KEY (id, created_at)`, already in `WORKSPACE_OWNED_MODELS`, columns `access_method`/`proxy_provider_id`/`proxy_country`/`status_code`/`response_time_ms`/`success`/`error_code`/`error_message` present). US3 only **populates** those fields (today hardcoded `DIRECT_HTTP`) and asserts one row per attempt incl. retries — **no new table, no `op.add_column`, no `WORKSPACE_OWNED_MODELS` edit** (research D1).
- **`BatchedPersistencePipeline`** — **UNCHANGED**; it already writes `RequestAttempt` off-reactor and batched. No persistence-pipeline change.
- **SSRF validator** — reuse the existing `app_shared.url_safety.validate_competitor_url` (save-time) + `scrape_core.safety.SafeResolver`/`SsrfGuardMiddleware` (fetch-time); write **no** new validator (D4, FR-005).
- Actual Playwright browser rendering (SPEC-14 — here only the `allow_browser_fallback` flag / `PLAYWRIGHT_PROXY` `AccessMethod` **intent**, terminated as STOP).
- Cluster-wide distributed domain rate limiter + fencing in-flight locks / `max_concurrent_requests` enforcement (SPEC-11 — here only each policy's own per-min/hour/day ceilings + per-domain cooldown; concurrency is intent only).
- Learned `preferred_method` / strategy optimizer (SPEC-12 — the engine accepts a `preferred_method` input; learning is later).
- `request_attempts` retention/partition maintenance (later); encryption key-management provisioning (operational).

Reuse unchanged: `app_shared.url_safety`, `scrape_core.safety`, `app_shared.redis_client`, the `INCR`+`EXPIRE`-on-first-hit / `SET NX EX` primitives from `security/rate_limit.py` + `security/last_used.py`, the SPEC-06 dual-scope pattern (`emit_global_readable_rls_policy` + `profiles/repository.py` + `profiles/resolution.py` + `routers/scrape_profiles.py` + `services/profile_resolution.py`), `app_shared.catalog.consistency` (`assert_*`/`MissingReference`/`CrossWorkspaceReference`), `enum_column`, `emit_rls_policy`, `WorkspaceScopedBase`/`TimestampMixin`, `scoped_select`/`scoped_get`, `deps.require_scopes`, cursor pagination, the AST scoping guard, `scrape_core.items.ScrapeResult` + `scrape_core.errors.classify_exception`, `run_in_thread`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Enums, config, scopes, and the empty pure-engine package. All DB/infra-independent; every later file imports these.

- [X] T001 [P] Extend `libs/shared/app_shared/enums.py` with three `StrEnum` → `VARCHAR` enums (per data-model.md / contracts/models-access.md / D9, rendered via `enum_column`): `AccessStrategy` (`DIRECT_ONLY`/`DIRECT_THEN_PROXY`/`PROXY_FIRST`/`RESIDENTIAL_ONLY`/`BROWSER_FALLBACK`), `ProxyType` (`DATACENTER`/`RESIDENTIAL`/`MOBILE`), `ProxyProviderStatus` (`ACTIVE`/`DISABLED`). Reuse the existing `AccessMethod` (`DIRECT_HTTP`/`DIRECT_HTTP_RETRY`/`PROXY_HTTP`/`PLAYWRIGHT_PROXY`, line 184) and `ScrapeErrorCode` (line 225 — already carries `PROXY_FAILED`/`RATE_LIMITED`/`HTTP_429`/`HTTP_403`/`TIMEOUT`/`DNS_ERROR`/`BLOCKED`/`LIMIT_REACHED`/`UNKNOWN_ERROR`) — do NOT add or widen either. (FR-001, FR-002)
- [X] T002 [P] Extend `libs/shared/app_shared/config.py` (`Settings`, per contracts/encryption.md + contracts/budget-ceilings.md): add `ENCRYPTION_KEYS: str` (required — comma-separated `version:urlsafe_b64_fernet_key` pairs), `ENCRYPTION_PRIMARY_KEY_VERSION: int = 1`, and `ACCESS_RESOLUTION_CACHE_TTL_SECONDS: int = 30`. Add a `field_validator`/parser that splits `ENCRYPTION_KEYS` into `{version: key}` and asserts the primary version is present (fail-fast on misconfig). Ceiling/cooldown values are per-policy/per-domain DB columns, NOT global settings. (FR-003, FR-010, FR-011, D3, D7)
- [X] T003 [P] Extend `libs/shared/app_shared/security/scopes.py` with six `Scope` members: `PROXY_PROVIDERS_READ`/`PROXY_PROVIDERS_WRITE` (`proxy_providers:read`/`:write`), `ACCESS_POLICIES_READ`/`ACCESS_POLICIES_WRITE` (`access_policies:read`/`:write`), `DOMAIN_RULES_READ`/`DOMAIN_RULES_WRITE` (`domain_rules:read`/`:write`), following the existing scope-member convention. (FR-001, FR-002, FR-004, D10)
- [X] T004 [P] Create `libs/shared/app_shared/access/__init__.py` — empty package init for the framework-agnostic access package (the `engine`/`resolution`/`repository`/`budget` modules + re-exports land in Foundational/US2). (D5, D6, D7)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The Fernet keyring, the three ORM models + registration, the dual-scope repository, the single-head migration, and the DB/infra-independent shape/RLS/encryption/offline-migration/scoping tests. **No user story can be implemented until this phase is complete.**

**⚠️ CRITICAL**: Blocks all of Phase 3–5.

- [ ] T005 Create `libs/shared/app_shared/security/encryption.py` per contracts/encryption.md — pure helper depending only on `cryptography.fernet` + `get_settings()`: `@dataclass(frozen=True) EncryptedSecret(ciphertext: str, key_version: int)`, `class SecretDecryptionError(RuntimeError)`, `encrypt_secret(plaintext) -> EncryptedSecret` (encrypt with the PRIMARY key), `decrypt_secret(ciphertext, key_version) -> str` (look up the version in the keyring; raise `SecretDecryptionError` if the version is absent or the token invalid — NEVER return ciphertext or blank), `reencrypt_secret(ciphertext, key_version) -> EncryptedSecret` (decrypt-old / encrypt-primary rotation primitive). Build the keyring once per process (module-level `lru_cache`) from `Settings.ENCRYPTION_KEYS`/`ENCRYPTION_PRIMARY_KEY_VERSION`. `app_shared`-pure (no FastAPI/Scrapy). (FR-003, §33, D3, depends on T002)
- [ ] T006 Create `libs/shared/app_shared/models/access.py` — three ORM models per data-model.md / contracts/models-access.md. `ProxyProvider(Base, TimestampMixin)` **dual-scope**: nullable indexed `workspace_id` (workspace FK) — NOT `WorkspaceScopedBase`; `name` (Text), `type`/`status` via `enum_column`, `base_url` (Text), `username` (Text, null), `password_encrypted` (Text, null), `password_key_version` (Integer, null), `country_code` (Text, null), `monthly_budget_limit` (Integer, null); `__table_args__` = the two partial-unique `Index`es (`uq_proxy_providers_workspace_id_name` WHERE `workspace_id IS NOT NULL`, `uq_proxy_providers_name_global` WHERE `workspace_id IS NULL`) + `ix_proxy_providers_workspace_id` + workspace FK. `AccessPolicy(Base, TimestampMixin)` **dual-scope**: same nullable-workspace pattern; `strategy` via `enum_column`; `provider_id` plain nullable `Uuid` (**no FK**); `country_code` (null); booleans `use_proxy_on_first_attempt`(default False)/`use_proxy_on_retry`(True)/`allow_browser_fallback`(False)/`rotate_per_request`(False)/`sticky_session`(False); `max_retries`(Integer, default 2)/`session_ttl_minutes`(null)/`max_requests_per_minute`/`_per_hour`/`_per_day`(null)/`timeout_ms`(Integer, default 30000); the same partial-unique index pair (`uq_access_policies_*`) + `ix_access_policies_workspace_id` + workspace FK. `DomainAccessRule(Base, WorkspaceScopedBase, TimestampMixin)` **tenant-only**: `competitor_id` indexed `Uuid` (**no FK**), `domain` (Text), `url_pattern`/`url_pattern_override` (Text, null), `access_policy_id` `Uuid` (**no FK**), `max_concurrent_requests`/`max_requests_per_minute`/`cooldown_seconds` (Integer), `block_detection_rules` (`postgresql.JSONB`, null), `enabled` (Boolean, default True); `__table_args__` = workspace FK + `ix_domain_access_rules_workspace_id` + composite lookup `ix_domain_access_rules_workspace_id_competitor_id_domain` + the domain/pattern uniqueness via a `COALESCE(url_pattern,'')` expression unique index. All enum-like columns render `VARCHAR` (no DB ENUM); all timestamps `TZDateTime`; all constraint/index names ≤63 bytes. (FR-001, FR-002, FR-004, FR-006, depends on T001)
- [ ] T007 Extend `libs/shared/app_shared/models/__init__.py` to import + re-export `ProxyProvider`, `AccessPolicy`, `DomainAccessRule` (add to `__all__`; `Base.metadata` visibility for the Alembic offline render). (depends on T006)
- [ ] T008 Extend `libs/shared/app_shared/repository.py` to add **only** `DomainAccessRule` to `WORKSPACE_OWNED_MODELS` (so `scoped_select`/`scoped_get` + the AST scoping guard cover the tenant-only table). `ProxyProvider`/`AccessPolicy` are **deliberately excluded** — their `scoped_select` would hide global rows; they use `app_shared.access.repository` (T009) instead. (FR-006, D2, depends on T006)
- [ ] T009 Create `libs/shared/app_shared/access/repository.py` per contracts/access-repository.md — the sanctioned query path for the two dual-scope tables, SQLAlchemy-only, mirroring `app_shared/profiles/repository.py`: `visible_providers_select(ws)` (own OR global `IS NULL`), `owned_provider_select(ws)` (own-only), `owned_provider_get(session, id_, ws)`, `assert_provider_assignable(session, ws, provider_id|None)` (None→ok; own+global→ok; cross-workspace→`CrossWorkspaceReference`; dangling→`MissingReference`, reusing `app_shared.catalog.consistency`); and the identical `visible_policies_select`/`owned_policy_select`/`owned_policy_get`/`assert_policy_assignable` set for `AccessPolicy`. `DomainAccessRule` needs no dedicated repo (tenant-only via `scoped_select`). (FR-006, D2, depends on T006)
- [ ] T010 Create the Alembic migration `alembic/versions/<rev>_access_policies_proxies_tables.py` per contracts/migration-access.md — `down_revision = "e4a75b48360c"` (current single head, verified). `upgrade()`: `op.create_table("proxy_providers", ...)` (all columns; enums → `sa.String(...)`, ids → `sa.Uuid`, timestamps → `sa.DateTime(timezone=True)`; `PrimaryKeyConstraint("id")`, workspace FK) then `ix_proxy_providers_workspace_id` + the two partial-unique indexes (`uq_proxy_providers_workspace_id_name` `postgresql_where=sa.text("workspace_id IS NOT NULL")`, `uq_proxy_providers_name_global` `postgresql_where=sa.text("workspace_id IS NULL")`); `op.create_table("access_policies", ...)` (same dual-scope index pair + `ix_access_policies_workspace_id` + workspace FK; `provider_id` plain UUID no-FK); `op.create_table("domain_access_rules", ...)` (workspace FK + `ix_domain_access_rules_workspace_id` + composite `ix_domain_access_rules_workspace_id_competitor_id_domain` + uniqueness on `(workspace_id, competitor_id, domain, COALESCE(url_pattern,''))` via an expression unique index); then RLS in the **same** migration — `for stmt in emit_global_readable_rls_policy("proxy_providers"): op.execute(stmt)`, same for `access_policies`, and `for stmt in emit_rls_policy("domain_access_rules"): op.execute(stmt)`. `downgrade()`: `op.drop_table` in reverse order (`domain_access_rules`, `access_policies`, `proxy_providers`). **`request_attempts` is NOT touched** (already exists — no `op.add_column`). Preserve a single head. (FR-006, FR-014, D1, depends on T006)

### Foundational tests (DB/infra-independent)

- [ ] T011 [P] Unit test `tests/unit/test_access_models.py` — table/column names + nullability for all three tables; every enum column `enum_column`-renders `VARCHAR` (not DB enum); `ProxyProvider`/`AccessPolicy` carry a **nullable** `workspace_id` + both partial-unique namespaces + `created_at`/`updated_at`; `DomainAccessRule` carries a **non-null** `workspace_id` + the composite lookup index + the `COALESCE(url_pattern,'')` uniqueness; `provider_id`/`access_policy_id`/`competitor_id` are plain `Uuid` with **no** FK; `ProxyProvider`/`AccessPolicy` are **absent** from `WORKSPACE_OWNED_MODELS` and `DomainAccessRule` **present**; all three re-exported from `app_shared.models`; every constraint/index name ≤63 bytes. (FR-001, FR-002, FR-004, FR-006, depends on T006, T007, T008)
- [ ] T012 [P] Unit test `tests/unit/test_encryption.py` per contracts/encryption.md Acceptance — `decrypt_secret(*encrypt_secret(p)) == p` round-trip for arbitrary strings incl. unicode; `encrypt_secret` twice on the same plaintext yields different ciphertext (Fernet IV) but both decrypt back; `decrypt_secret` with an unknown `key_version` raises `SecretDecryptionError` (never returns a value); `reencrypt_secret` on a v1 token returns a `key_version == primary` token that decrypts to the original, and a two-key ring proves decrypt-old-while-writing-new; no code path returns/logs the plaintext. (FR-003, SC-003, depends on T005)
- [ ] T013 [P] Unit test `tests/unit/test_migration_offline_access.py` — `alembic upgrade head --sql` (offline, no DB) renders `CREATE TABLE` for `proxy_providers`/`access_policies`/`domain_access_rules`, both partial-unique namespaces on each dual-scope table, the `COALESCE(url_pattern,'')` expression unique index, the dual read/write `emit_global_readable_rls_policy` statements (`ENABLE`/`FORCE ROW LEVEL SECURITY` + own-or-global read + own-only write) for the two dual-scope tables and the single fail-closed `emit_rls_policy` for `domain_access_rules`; `request_attempts` is **absent** from the diff (already exists); `alembic heads` yields a single head; `down_revision == "e4a75b48360c"`. (FR-006, FR-014, depends on T010)
- [ ] T014 [P] Unit test `tests/unit/test_access_scoping_guard.py` — the workspace-scoping AST CI guard flags a planted unscoped `select` on `DomainAccessRule` (in the guarded `WORKSPACE_OWNED_MODELS` set) and confirms `ProxyProvider`/`AccessPolicy` are intentionally **not** guarded there (they use `access.repository`); `scripts/check_workspace_scoping.py` passes on the real tree. (FR-006, SC-005, depends on T008)

**Checkpoint**: Enums/config/scopes + encryption keyring + models + dual-scope repository + single-head migration + RLS wired; DB-independent shape/encryption/offline-migration/scoping tests green. User stories can begin.

---

## Phase 3: User Story 1 - Configure how competitor sites are accessed (Priority: P1) 🎯 MVP

**Goal**: An operator can create/read/update/delete **access policies** and **proxy providers** (dual-scope: own + read-only global defaults) and **domain access rules** (tenant-only) through `/v1` CRUD. Proxy passwords are encrypted at rest and never returned in plaintext (only `has_password`). All four entities are workspace-isolated; a `base_url` failing SSRF is rejected.

**Independent Test**: Create an access policy + proxy provider via the API; the policy round-trips with every strategy/retry/rate field; the proxy `password` is accepted, stored as ciphertext, and the response exposes only `has_password` (no password field, no ciphertext); a second workspace sees only its own entries plus read-only globals and can neither read nor mutate workspace A's tenant rows; a no-context query returns zero tenant rows (globals still visible); a `base_url` to a private/loopback/metadata host or with `user:pass@` → 422 `UNSAFE_URL`.

### Implementation for User Story 1

- [ ] T015 [P] [US1] Create `apps/api/app/schemas/access.py` per contracts/api-access.md — request/response envelopes for the three tables. `ProxyProviderCreate`/`Update` accept a plaintext `password` (never stored on the ORM); `ProxyProviderResponse` carries **every column EXCEPT** `password_encrypted`/`password_key_version`, exposing instead a boolean `has_password` (SC-003). `AccessPolicyCreate`/`Update`/`Response` carry the full FR-001 field set with Pydantic validation (`max_retries` `ge=0`, `timeout_ms` `gt=0`, ceilings `gt=0` when set). `DomainAccessRuleCreate`/`Update`/`Response` carry the FR-004 set (`max_concurrent_requests` `ge=1`, `max_requests_per_minute`/`cooldown_seconds` `ge=0`). (FR-001, FR-002, FR-004, SC-003, D10)
- [ ] T016 [US1] Create `apps/api/app/routers/proxy_providers.py` (`/v1/proxy-providers`, dual-scope) per contracts/api-access.md, following `routers/scrape_profiles.py`: `require_scopes(PROXY_PROVIDERS_READ/WRITE)`; `POST` create (own workspace) — `base_url` → `validate_competitor_url` (422 `UNSAFE_URL`), plaintext `password` → `encrypt_secret` → `(password_encrypted, password_key_version)`, duplicate name → 409; `GET` list via `visible_providers_select` (own+global), cursor-paginated; `GET /{id}` via `visible_providers_select` else 404; `PATCH /{id}` own-only (`owned_provider_get`; global/other-ws → 404), a new `password` re-encrypts, `password: null` clears both columns, omitted → unchanged; `DELETE /{id}` own-only hard delete. Response via `ProxyProviderResponse` (never the password). (FR-002, FR-003, FR-005, FR-006, SC-003, depends on T005, T009, T015)
- [ ] T017 [US1] Create `apps/api/app/routers/access_policies.py` (`/v1/access-policies`, dual-scope) per contracts/api-access.md — same dual-scope pattern (`visible_policies_select` reads, `owned_policy_*` writes, `require_scopes(ACCESS_POLICIES_*)`, duplicate name → 409); `provider_id` when set → `assert_provider_assignable` (422 `WORKSPACE_MISMATCH` / 404 dangling); full FR-001 body/response. (FR-001, FR-006, depends on T009, T015)
- [ ] T018 [US1] Create `apps/api/app/routers/domain_access_rules.py` (`/v1/domain-access-rules`, tenant-only) per contracts/api-access.md — standard `scoped_select`/`scoped_get` CRUD (`require_scopes(DOMAIN_RULES_*)`); `competitor_id` must be in the caller's workspace (422 cross-workspace / 404 dangling via `app_shared.catalog.consistency`); `access_policy_id` → `assert_policy_assignable`; duplicate `(competitor_id, domain, url_pattern)` → 409. (FR-004, FR-006, depends on T008, T009, T015)
- [ ] T019 [US1] Extend `apps/api/app/main.py` to `app.include_router(...)` the three new routers (proxy_providers, access_policies, domain_access_rules). (depends on T016, T017, T018)
- [ ] T020 [P] [US1] Integration test `tests/integration/test_api_access.py` (skip-clean) per quickstart.md §3 / contracts/api-access.md Acceptance — a created policy round-trips every strategy/retry/rate field (US1-1); create a proxy provider with a `password` → response has `has_password=true` and **no** password field, DB stores ciphertext ≠ plaintext, a second GET never exposes it (US1-2, SC-003); cross-workspace read/patch/delete of workspace A's tenant rows denied and globals read-only/immutable from the tenant path (US1-3, SC-005); no-context query → zero tenant rows, globals still visible for the dual-scope tables (US1-4); `base_url` to a private/loopback/metadata host or with `user:pass@` → 422 `UNSAFE_URL` (US1-5); `provider_id`/`access_policy_id`/`competitor_id` cross-workspace assignment → 422. (depends on T019)
- [ ] T021 [P] [US1] Integration test `tests/integration/test_access_isolation_live.py` (skip-clean) per contracts/access-repository.md + migration-access.md Acceptance — applies the migration to a live Postgres, then asserts dual-scope RLS + repository semantics directly: a workspace sees own rows + all global (`workspace_id IS NULL`) rows via `visible_*`; `owned_*` never returns a global row (write path cannot mutate a system default); a no-context session → `visible_*` returns only globals, zero tenant rows; `domain_access_rules` fail-closed isolation (own-only, no-context → zero). (FR-006, SC-005, depends on T010, T009)

**Checkpoint**: US1 fully functional — access config CRUD is workspace-isolated, credentials encrypted & redacted, SSRF-guarded. Deliverable MVP.

---

## Phase 4: User Story 2 - Drive direct-vs-proxy behavior during a scrape (Priority: P2)

**Goal**: The pure engines turn a resolved policy + attempt history into the next transport decision and proxy assignment; the resolution chain makes a matching enabled domain rule override the workspace/global default (URL-pattern beats domain-only), batch-resolved once per group and Redis-cached; the budget/ceiling/cooldown counters gate proxied attempts via cheap Redis ops; and the spider consumes all of this at its request seam (set `request.meta['proxy']`, retry per policy, budget-check off-reactor) — SSRF still applies to the target.

**Independent Test**: Drive `next_attempt` over the full strategy × attempt-number × flag × budget matrix and assert `DIRECT_ONLY` never proxies, `DIRECT_THEN_PROXY` retries via `PROXY_HTTP`, `max_retries=0` → one plan then STOP, `proxy_budget_exhausted` reroutes/stops per strategy; `resolve_effective_policy` puts an enabled matching domain rule over the workspace default and the workspace over the global, with disabled-rule fall-through and URL-pattern-beats-domain-only; a fake-redis proves the monthly budget flips `allowed=False` past the limit and resets on a new `%Y_%m`, and the per-min/hour/day ceilings + cooldown gate; a spider run with `DIRECT_THEN_PROXY` + a failed direct first attempt issues a second attempt via `PROXY_HTTP`, and `DIRECT_ONLY` never proxies.

### Implementation for User Story 2

- [ ] T022 [P] [US2] Create `libs/shared/app_shared/access/engine.py` (pure, stdlib only) per contracts/access-engine.md — `@dataclass(frozen=True) AttemptPlan(access_method: AccessMethod, use_proxy: bool)`, `STOP` falsy sentinel, `next_attempt(strategy, *, attempt_number, max_retries, use_proxy_on_first_attempt, use_proxy_on_retry, allow_browser_fallback, preferred_method=None, proxy_budget_exhausted=False) -> AttemptPlan | _Stop` encoding the full behavior matrix (`DIRECT_ONLY` never proxies; `DIRECT_THEN_PROXY` direct-then-`PROXY_HTTP`-on-retry; `PROXY_FIRST`/`RESIDENTIAL_ONLY` proxied first; `BROWSER_FALLBACK`/`allow_browser_fallback` → terminal `PLAYWRIGHT_PROXY` **intent only**; unknown-domain default chain `DIRECT_HTTP → DIRECT_HTTP_RETRY → PROXY_HTTP → PLAYWRIGHT_PROXY → STOP`; `preferred_method` starts a learned domain; `max_retries==0` → one plan then STOP; `proxy_budget_exhausted` skips proxy steps → fall through or STOP); and `@dataclass(frozen=True) ProxyAssignment(provider_id, country, sticky_key)` + `assign_proxy(*, strategy, policy_provider_id, policy_country, domain_rule_country, visible_providers, attempt_number, rotate_per_request, sticky_session, session_seed) -> ProxyAssignment | None` (pick provider+country per policy/domain-rule, rotate vs stable sticky_key; for `RESIDENTIAL_ONLY` restrict the candidate set to `ProxyType.RESIDENTIAL` providers; `None` when no eligible provider is visible/DISABLED/absent → caller degrades). (FR-008, FR-009, D6, depends on T001, T004)
- [ ] T023 [P] [US2] Create `libs/shared/app_shared/access/resolution.py` (pure, no SQLAlchemy/Redis/FastAPI) per contracts/policy-resolution.md, mirroring `app_shared/profiles/resolution.py` — `@dataclass(frozen=True) ResolvedPolicy(policy_id, level: Literal["domain_rule","workspace","global"])`, `NONE_RESOLVED` sentinel; `select_domain_rule(rules, *, domain, url) -> object | None` (most-specific enabled match: URL-pattern rule beats domain-only; disabled ignored; deterministic tie-break by pattern length then id); `resolve_effective_policy(*, domain_rule_policy_id, workspace_default_policy_id, global_default_policy_id, visible_ids) -> ResolvedPolicy | _NoneResolved` (precedence domain_rule → workspace → global; a candidate counts only if in `visible_ids`, else falls through — dangling/cross-workspace tolerated); `access_resolution_cache_key(workspace_id, competitor_id, domain, url_pattern)` + `encode_result`/`decode_result` codec. (FR-007, D5, depends on T004)
- [ ] T024 [P] [US2] Create `libs/shared/app_shared/access/budget.py` (framework-agnostic; takes a `redis.Redis`-shaped client, like `security/rate_limit.py`) per contracts/budget-ceilings.md — `@dataclass BudgetResult(allowed, used, limit)` + `incr_and_check_monthly_budget(redis, *, provider_id, limit, now)` (key `proxybudget:{provider_id}:{now:%Y_%m}`, `INCR` then `EXPIRE` to end-of-month on first hit; `limit is None` → always allowed; incremented once per PROXIED request; Redis error → fail-open `allowed=True`); `@dataclass RateDecision(allowed, retry_after_seconds)` + `check_rate_ceilings(redis, *, policy_id, domain, per_minute, per_hour, per_day)` (up to three windowed `INCR`+`EXPIRE` counters 60/3600/86400 s keyed `ratelimit:{policy_id}:{domain}:{window}`; any None skipped; over-ceiling → `allowed=False` with `retry_after`; fail-open on Redis error — documented divergence from the fail-closed login limiter); `check_domain_cooldown(redis, *, domain, cooldown_seconds)` (`SET NX EX` gate `cooldown:{domain}`; `<=0` → always True). Per-domain `max_concurrent_requests` is intent only (SPEC-11). **No `request_attempts` query anywhere in the module** (FR-010, §22). (FR-010, FR-011, D7, depends on T002, T004)
- [ ] T025 [US2] Create `apps/api/app/services/access_resolution.py` orchestrator per contracts/policy-resolution.md, mirroring `services/profile_resolution.py` — given a batch of matches, group by `(competitor_id, domain, url_pattern)`, check the Redis cache per group (`access_resolution_cache_key`), on miss do the **bounded** loads (the workspace's reserved-name `default` access policy + the global `global_default` policy via `visible_policies_select`, the competitor's enabled domain rules via `scoped_select`, the visible-policy id set; workspace `default` overrides `global_default`, and when neither resolves the group yields `NONE_RESOLVED` → target skipped), run the pure `select_domain_rule` + `resolve_effective_policy` **once per group**, cache with TTL `Settings.ACCESS_RESOLUTION_CACHE_TTL_SECONDS`, and return one `ResolvedPolicy | NONE_RESOLVED` per match — never an N+1 per-match walk (Principle IV). (FR-007, D5, depends on T023, T009, T008)
- [ ] T026 [US2] Extend `apps/scrapers/price_monitor/spiders/generic_price_spider.py` at its request-side seams per contracts/spider-integration.md (§1–§3, do NOT rebuild) — in `load_targets` (off-reactor `run_in_thread`) resolve the effective policy per `(competitor, domain, url_pattern)` group using the **same** `access_resolution_cache_key` cache the API orchestrator populates (duplicated bounded-load shape, `apps → libs` only), attaching the resolved policy to each `SpiderTarget`; in `_request_for` compute `next_attempt(..., attempt_number=1)`, for a proxied plan call `assign_proxy(...)`, set `request.meta["proxy"] = "http://{host}:{port}"` + a `Proxy-Authorization` header from `username` + `decrypt_secret(password_encrypted, password_key_version)` decrypted **inside `run_in_thread`** (never on the reactor, never logged), stash `access_method`/`proxy_provider_id`/`proxy_country`/`attempt_number` in `request.meta`, and before **every** dispatch (direct or proxied) call `check_rate_ceilings(per-min/hour/day)` — using the matching domain rule's `max_requests_per_minute` in place of the policy's per-minute ceiling when the rule sets one — + `check_domain_cooldown(domain rule cooldown_seconds)` (off-reactor) — on `allowed=False` defer/skip the attempt and stamp `RATE_LIMITED` (do not dispatch); and before a **proxied** dispatch additionally call `incr_and_check_monthly_budget` (off-reactor) — on `allowed=False` recompute `next_attempt(proxy_budget_exhausted=True)`; in `errback`/retry consult `next_attempt(..., attempt_number=n+1)` and yield a follow-up `scrapy.Request` with the new method/proxy up to `max_retries`, or terminate on `STOP` (`PLAYWRIGHT_PROXY` recorded as intent, terminates here). Per-domain `max_concurrent_requests` is not enforced here (SPEC-11). (FR-008, FR-009, FR-010, FR-011, D6, D7, D8, depends on T022, T023, T024, T025, T005)
- [ ] T027 [P] [US2] Extend `apps/scrapers/price_monitor/settings.py` to enable Scrapy's built-in `HttpProxyMiddleware` (reads `request.meta["proxy"]`) in `DOWNLOADER_MIDDLEWARES`, keeping the existing SSRF `DNS_RESOLVER` (`SafeResolver`) + `SsrfGuardMiddleware` in place so the *target* URL is still DNS-re-resolved and every redirect hop re-validated (FR-005 fetch-time). (FR-005, D8)

### Tests for User Story 2

- [ ] T028 [P] [US2] Unit test `tests/unit/test_access_engine.py` (exhaustive, no infra) per contracts/access-engine.md Acceptance — every (strategy × `attempt_number ∈ 1..max_retries+2` × flag combination) yields the documented matrix; `DIRECT_ONLY` never emits `use_proxy=True` for any input (SC-001); `max_retries=0` → one plan then STOP; `proxy_budget_exhausted` reroutes/stops per strategy; `preferred_method` starts a learned domain; `assign_proxy` returns `None` for a DISABLED/missing provider and honors sticky (stable key across attempts) vs rotate (differs); `RESIDENTIAL_ONLY` picks only a `ProxyType.RESIDENTIAL` provider and returns `None` (→ degrade) when only non-residential providers are visible. (FR-008, FR-009, SC-001, depends on T022)
- [ ] T029 [P] [US2] Unit test `tests/unit/test_policy_resolution.py` (no infra) per contracts/policy-resolution.md Acceptance — precedence table: enabled domain rule > workspace default > global (SC-004, 100%); disabled domain rule falls through to default; URL-pattern rule beats domain-only for a matching URL, domain-only applies when no pattern matches; dangling/cross-workspace candidate id skipped (not an error); a batch of N matches in one group walks the chain **once** (one cache write per distinct group — Principle IV); `decode_result(encode_result(r)) == r` incl. `NONE_RESOLVED`. (FR-007, SC-004, depends on T023)
- [ ] T030 [P] [US2] Unit test `tests/unit/test_access_budget.py` (fake redis, no infra) per contracts/budget-ceilings.md Acceptance — the (limit+1)-th proxied `incr_and_check_monthly_budget` returns `allowed=False`; a new `%Y_%m` (simulated `now`) resets; TTL set only on first hit; `check_rate_ceilings` flips `allowed=False` with a positive `retry_after` on the exceeded window, independent windows tracked separately; `check_domain_cooldown` second call within the window → False, after expiry → True; **assert (import/AST/grep guard) that the module issues no `request_attempts` query** (FR-010, §22). (FR-010, FR-011, depends on T024)
- [ ] T031 [P] [US2] Integration test `tests/integration/test_access_budget_redis.py` (skip-clean, real Redis) — the same budget/ceiling/cooldown behaviors against a live `noeviction` Redis: monthly rollover, TTL-to-end-of-month, per-window counters, `SET NX EX` cooldown; fail-open on a simulated Redis error. (FR-010, FR-011, SC — depends on T024)
- [ ] T032 [US2] Integration test `tests/integration/test_spider_access.py` (skip-clean) per contracts/spider-integration.md Acceptance (request-side) — `DIRECT_THEN_PROXY` with `max_retries≥1` and a failed direct first attempt issues a **second** attempt via `PROXY_HTTP` with the proxy provider/country set (US2-1); `DIRECT_ONLY` never proxies any attempt (US2-2, SC-001); a disabled/missing referenced provider degrades (falls back per strategy or records `PROXY_FAILED`) and never crashes the run (Edge Case); a policy whose per-minute ceiling (or an active domain cooldown) is exceeded **defers/skips** the attempt and records `RATE_LIMITED` — no dispatch (FR-011, US2-4); an exhausted proxy budget reroutes per strategy or records `LIMIT_REACHED` (US2-5); the decrypted proxy password never appears in captured logs. (FR-008, FR-009, FR-011, SC-001, depends on T026, T027)

**Checkpoint**: US2 functional — resolution + engines + budget/ceilings drive real direct-vs-proxy behavior in the spider; domain rules override defaults; proxied attempts are budget/ceiling-gated.

---

## Phase 5: User Story 3 - Record every request attempt for audit and tuning (Priority: P3)

**Goal**: Every fetch attempt (direct or proxied, success or failure, including retries) is logged as exactly one `RequestAttempt` row with the **real** `access_method`/`proxy_provider_id`/`proxy_country`/`attempt_number`/`status_code`/`response_time_ms`/`success`/structured `error_code` — populated by finishing the spider's result-side wiring (previously hardcoded `DIRECT_HTTP`) — with writes staying batched and off the reactor via the **unchanged** `BatchedPersistencePipeline`, landing in the correct monthly partition.

**Independent Test**: Run a scrape that makes N attempts (mix of direct/proxy, success/failure) and assert N `RequestAttempt` rows are persisted with correct `access_method`, proxy fields, status, timing, success, and `error_code`; a direct attempt + a proxied retry produce two rows with `attempt_number` 1 & 2 (the retry carrying provider/country); a blocked/timeout/proxy failure carries the corresponding structured code (`BLOCKED`/`TIMEOUT`/`PROXY_FAILED`); rows land in the correct monthly partition and writes are batched off the reactor thread.

### Implementation for User Story 3

- [ ] T033 [US3] Extend `libs/scrape-core/scrape_core/errors.py` `classify_exception` (and any HTTP-status mapping) to cover the proxy/access failure vocabulary per contracts/spider-integration.md §4 — map proxy connect/auth failures → `PROXY_FAILED`, budget exhaustion → `LIMIT_REACHED`, rate defer → `RATE_LIMITED`, plus the already-present `HTTP_429`/`HTTP_403`/`TIMEOUT`/`DNS_ERROR`/`BLOCKED`/`UNKNOWN_ERROR` — reusing the existing `ScrapeErrorCode` members (no enum change). (FR-013, depends on T001)
- [ ] T034 [US3] Extend `apps/scrapers/price_monitor/spiders/generic_price_spider.py` `_build_result` per contracts/spider-integration.md §4 (result-side wiring finish) — stamp each emitted `ScrapeResult`'s `access_method` from the **actual** attempt's method (not the hardcoded `DIRECT_HTTP`), plus `proxy_provider_id`/`proxy_country` (None for direct), the real `attempt_number`, `status_code`, `response_time_ms`, `success`, and the `error_code` from `classify_exception`; ensure **one** `ScrapeResult` is emitted per attempt **including retries** (so the unchanged `BatchedPersistencePipeline` writes one `RequestAttempt` per attempt). No pipeline/model/migration change. (FR-012, FR-013, FR-015, D1, depends on T026, T033)
- [ ] T035 [US3] Extend `tests/integration/test_spider_access.py` (skip-clean) with the attempt-logging assertions per contracts/spider-integration.md Acceptance + quickstart.md §5 — a direct attempt + a proxied retry produce **two** `RequestAttempt` rows with `attempt_number` 1 & 2, the retry carrying the proxy provider/country (US3-1, SC-002); a blocked/timeout/proxy failure carries the matching `error_code` (US3-2); exactly one row per attempt (SC-002); each row lands in the correct monthly partition with `access_method`/proxy fields set (US3-3); writes stay batched & off the reactor thread (unchanged pipeline; SC-006); a workspace-context query sees only its rows, no-context → zero (US3-4, SC-005). (FR-012, FR-013, FR-014, FR-015, depends on T034, T032)

**Checkpoint**: US3 functional — every attempt is faithfully logged with real access/proxy fields into the existing partitioned, off-reactor-batched `request_attempts`.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Whole-feature guards and validation across all three stories.

- [ ] T036 [P] Run the full guard sweep — `uv run python scripts/check_workspace_scoping.py` (confirms `DomainAccessRule` is scoped and the two dual-scope tables use `access.repository`, not `scoped_select`) and `uv run alembic heads` (confirms exactly one head after the new revision). (SC-005, FR-006)
- [ ] T037 [P] Add/verify the security grep-guards — a test/CI assertion that no `ProxyProviderResponse` (or any router response) can carry `password`/`password_encrypted`/`password_key_version` (SC-003), that the spider never logs a decrypted password, and that `app_shared/access/budget.py` contains no `request_attempts` reference (FR-010) — extend `tests/unit/test_encryption.py` / add a small `tests/unit/test_access_guards.py`. (SC-003, FR-010)
- [ ] T038 [P] Confirm the import-boundary constitution rule — `app_shared` (enums, config, encryption, models, `access/*`) imports **no** Scrapy/Twisted/FastAPI and no `apps/*`; add/extend the existing import-boundary unit test to cover the new `app_shared.access` package + `app_shared.security.encryption`. (Principle I, plan Constraints)
- [ ] T039 Run the quickstart.md validation walkthrough end-to-end (`uv sync --all-packages`; pure-engine unit suites §2/§4 must pass everywhere; the integration suites §1/§3/§5 skip cleanly without infra); confirm the Success-mapping table (SC-001..SC-006) is exercised. (all SC)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately.
- **Foundational (Phase 2)**: Depends on Setup — **BLOCKS all user stories**.
- **User Stories (Phase 3–5)**: All depend on Foundational. US1 is the MVP. US2 depends on Foundational (engines/budget are independent of US1's API, but the resolution orchestrator reuses the dual-scope repository from Foundational). US3 depends on US2's spider request-side wiring (T026) since it finishes the same spider's result side.
- **Polish (Phase 6)**: Depends on all desired user stories being complete.

### Key task dependencies

- T005 (encryption) ← T002 (config).
- T006 (models) ← T001 (enums); T007/T008/T009/T010 ← T006.
- T009 (dual-scope repo) ← T006; reused by US1 routers (T016/T017) and US2 orchestrator (T025).
- US1: T015 (schemas) → T016/T017/T018 (routers) → T019 (register) → T020/T021 (tests).
- US2: T022/T023/T024 (pure engines, all [P]) → T025 (orchestrator ← T023/T009/T008) → T026 (spider ← T022/T023/T024/T025/T005) ; T027 [P]; tests T028/T029/T030 [P] follow their module, T031 ← T024, T032 ← T026/T027.
- US3: T033 (error map ← T001) + T034 (spider result-side ← T026/T033) → T035 (← T034/T032).

### Within Each User Story

- Tests are written against the module/endpoint they cover; pure-engine unit tests can precede or follow the engine (both are authored in the same phase).
- Models/repository/migration before services and routers.
- Story complete before moving to the next priority.

### Parallel Opportunities

- **Setup**: T001, T002, T003, T004 all [P] (different files).
- **Foundational**: T005 (encryption) is independent of T006–T010 (models/migration) — parallelizable; the four foundational tests T011/T012/T013/T014 are all [P] once their targets exist.
- **US1**: T015 [P] first; T020 and T021 [P] once the routers/migration exist.
- **US2**: the three pure engines T022/T023/T024 are fully [P]; T027 [P]; the four unit/integration tests T028/T029/T030/T031 are [P].
- **US3**: T033 and the T034 spider edit touch different files but T034 imports T033 — sequential.
- **Polish**: T036/T037/T038 [P].
- Once Foundational completes, **US1 and the US2 pure engines can be built in parallel** by different developers (US1 = API/config surface; US2 engines = pure logic); US3 joins after US2's spider seam lands.

---

## Parallel Example: Foundational + User Story 2 engines

```bash
# After Setup, launch the independent Foundational building blocks together:
Task: "Create the Fernet keyring in libs/shared/app_shared/security/encryption.py"   # T005
Task: "Create the 3 ORM models in libs/shared/app_shared/models/access.py"           # T006

# After Foundational, launch the three pure engines together (US2):
Task: "Create the pure attempt engine in libs/shared/app_shared/access/engine.py"        # T022
Task: "Create the pure resolution chain in libs/shared/app_shared/access/resolution.py"  # T023
Task: "Create the Redis budget/ceilings in libs/shared/app_shared/access/budget.py"      # T024

# ...and their exhaustive unit tests together:
Task: "tests/unit/test_access_engine.py"        # T028
Task: "tests/unit/test_policy_resolution.py"    # T029
Task: "tests/unit/test_access_budget.py"        # T030
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (enums, config, scopes, package init).
2. Complete Phase 2: Foundational (encryption, models, repository, migration, RLS, guards) — **blocks all stories**.
3. Complete Phase 3: User Story 1 (dual-scope + tenant CRUD, credential encryption/redaction, SSRF).
4. **STOP and VALIDATE**: an operator can safely store access policies, proxy providers (encrypted), and domain rules, workspace-isolated. Deliverable.

### Incremental Delivery

1. Setup + Foundational → foundation ready.
2. US1 (P1) → configuration layer → validate → MVP.
3. US2 (P2) → engines + resolution + budget + spider request-side → validate direct-vs-proxy behavior.
4. US3 (P3) → finish the spider result-side wiring → validate faithful attempt logging.
5. Polish → guards + quickstart sweep.

### Notes

- `request_attempts` is **already built** (SPEC-07) — US3 is a wiring finish, not a new table/migration. Do NOT recreate it or touch `BatchedPersistencePipeline`.
- The two pure engines (`engine.py`, `resolution.py`) + `budget.py` are the acceptance core (SC-001/SC-004/FR-010/FR-011) and must be exhaustively unit-testable with **no infra**.
- Never blocking Redis/DB on the Scrapy reactor thread — all budget/ceiling checks and password decryption run off-reactor (`run_in_thread`).
- `PLAYWRIGHT_PROXY` is signalled only (SPEC-14); cluster-wide limiter + in-flight locks are SPEC-11; both out of scope here.
- [P] = different files, no dependency on an incomplete task; commit after each task or logical group.
