# Phase 0 Research: Access Policies, Proxies & Request Attempts

All Technical-Context unknowns resolved doc-first against `PROJECT_SPEC.md` (§2, §11, §22,
§32, §33, §34), the constitution, and the SPEC-06/07/08/09 code already in the repo. No open
NEEDS CLARIFICATION remains. The spec's Clarifications session already bound the enums and
the Fernet decision; this phase records the reuse/structure decisions.

---

## D1 — `request_attempts` already exists; US3 is a wiring finish, not a new table

**Decision**: Do **not** create a `request_attempts` table, model, or migration. SPEC-07
already ships `RequestAttempt` (`libs/shared/app_shared/models/observations.py`) — monthly-
partitioned by `created_at`, composite `PRIMARY KEY (id, created_at)`, in
`WORKSPACE_OWNED_MODELS`, with columns `access_method`, `proxy_provider_id`, `proxy_country`,
`status_code`, `response_time_ms`, `success`, `error_code`, `error_message`. Its migration
is `2db33dea5e14_observations_current_prices_tables.py`. The `ScrapeResult` transport item
(`scrape_core/items.py`) already carries all attempt fields, and
`BatchedPersistencePipeline._flush_batch` already inserts one `RequestAttempt` per item
**off-reactor and batched** (`run_in_thread` + `workspace_txn`). US3's remaining work is only
to make the spider **populate** `access_method`/`proxy_provider_id`/`proxy_country` correctly
(today it hardcodes `DIRECT_HTTP`) and to emit one `ScrapeResult` per attempt including
retries.

**Rationale**: FR-014/FR-015 (monthly partition from birth, PK includes partition key, soft
refs, off-reactor batched writes) are already satisfied — re-creating the table would
duplicate and conflict. Constitution VIII partitioning requirement is met by the existing
table.

**Alternatives considered**: A separate SPEC-10 `request_attempts` table (rejected — it
already exists; would collide on the migration and split the write path). Adding columns to
it (rejected — the existing column set already matches FR-012 exactly; no `op.add_column`
needed).

---

## D2 — Scope shape per table: two dual-scope + one tenant-only (reconcile FR-006 vs §22)

**Decision**:
- `proxy_providers` and `access_policies` → **dual-scope** (`workspace_id` nullable; `NULL` =
  global system default, readable by every workspace, writable by none through the tenant
  path). Exactly the SPEC-06 `scrape_profiles` pattern: `Base + TimestampMixin` (not
  `WorkspaceScopedBase`), `emit_global_readable_rls_policy`, a dedicated
  `app_shared.access.repository` with `visible_*`/`owned_*` selects, and **not** in
  `WORKSPACE_OWNED_MODELS`.
- `domain_access_rules` → **tenant-only** (`workspace_id NOT NULL`): `WorkspaceScopedBase`,
  `emit_rls_policy`, added to `WORKSPACE_OWNED_MODELS`, queried via `scoped_select`.

**Rationale**: §22 (source of truth) lists `proxy_providers.workspace_id nullable` and
`access_policies.workspace_id nullable`, but `domain_access_rules.workspace_id` **without**
`nullable`. A domain rule binds a `competitor_id` (a workspace-owned entity, SPEC-05) and a
domain — a *global* domain rule referencing a tenant's competitor is nonsensical, so tenant-
only is the correct and stricter reading. FR-006's "all three configuration tables … null =
global" is satisfied for the two config catalogs that legitimately have shared defaults;
domain rules are per-tenant overrides. This is the stricter interpretation, so Governance's
"stricter rule applies" holds. Documented so the discrepancy is a decision, not a drift.

**Alternatives considered**: Making all three dual-scope (rejected — contradicts §22's non-
nullable `domain_access_rules.workspace_id` and yields meaningless global domain rules).
Making all three tenant-only (rejected — loses the global system-default providers/policies
that FR-006 and US1 require: "global (system-provided) defaults … a workspace only ever sees
the global defaults plus its own entries").

---

## D3 — Fernet credential encryption: a versioned keyring in `app_shared.security.encryption`

**Decision**: New pure helper `app_shared/security/encryption.py` (alongside `jwt.py`,
`rate_limit.py`), depending only on `cryptography.fernet` (already in `uv.lock`) + `Settings`.
Shape:

- `encrypt_secret(plaintext: str) -> EncryptedSecret` → returns `(ciphertext: str,
  key_version: int)`, encrypting with the **primary** key.
- `decrypt_secret(ciphertext: str, key_version: int) -> str` → looks up the Fernet for that
  `key_version` in the keyring and decrypts; raises `SecretDecryptionError` (operational
  error, never a plaintext fallback) if the version is missing/unreadable.
- `reencrypt_secret(ciphertext, key_version) -> EncryptedSecret` → decrypt-with-old,
  encrypt-with-primary (the rotation primitive).

The keyring is built from `Settings.ENCRYPTION_KEYS` — a comma-separated list of
`version:urlsafe_b64_key` pairs — plus `Settings.ENCRYPTION_PRIMARY_KEY_VERSION` selecting
which one new writes use. Old versions stay in the map so existing ciphertexts remain
decryptable; retiring a key = removing its entry after all rows are re-encrypted (§33
decrypt-old / re-encrypt / retire).

`proxy_providers` stores `password_encrypted` (Text, the ciphertext string) + a
`password_key_version` (Integer) so each field records the version that encrypted it
(FR-003). The API layer encrypts on write and **never** decrypts into a response — the
plaintext password is never a response field (SC-003); decryption happens only in the
scrape-side proxy-auth assembly.

**Rationale**: Matches §33 exactly (Fernet, env-var key, `key_version` column, rotation
story) and the constitution's "Secrets & auth" clause. Placing it pure in `app_shared`
(no FastAPI/Scrapy) keeps it reusable by both the API (encrypt on write) and the scraper
(decrypt to build `Proxy-Authorization`), and unit-testable without infra.

**Alternatives considered**: A single `ENCRYPTION_KEY` with no version map (rejected — cannot
satisfy the rotation story: you cannot decrypt old-key rows while writing new-key rows).
`MultiFernet` alone (rejected — it tries keys in order but does not record *which* key was
used per field; the explicit `key_version` column is required by §33 and makes retirement
deterministic — you know exactly which rows still need re-encryption).

---

## D4 — SSRF validation: reuse the existing two-layer validator, add nothing

**Decision**: Reuse `app_shared.url_safety.validate_competitor_url` (save-time, pure, no DNS)
for `proxy_providers.base_url` and any policy-supplied URL on the API write path — it already
enforces http/https-only, public-host-only, rejects loopback/private/link-local/reserved/
multicast/unspecified IPs, internal hostnames/suffixes, and embedded `user:pass@` userinfo
(FR-005 save-time). Fetch-time re-resolution + per-redirect re-validation is already provided
by `scrape_core.safety.resolver.SafeResolver` (connect-time DNS re-resolution, DNS-rebinding
defense) and `scrape_core.safety.middleware.SsrfGuardMiddleware` (re-validates every request
including each redirect hop). No new validator is written.

**Rationale**: FR-005 is verbatim the control SPEC-07 already built and wired into the
`price_monitor` Scrapy project (`settings.py`: `DNS_RESOLVER` + `DOWNLOADER_MIDDLEWARES`).
The task brief explicitly says reuse it. Proxy `base_url` is user input → route it through
`validate_competitor_url` at save time; the proxied fetch still goes through the same fetch-
time guards for the *target* URL.

**Alternatives considered**: A proxy-specific validator (rejected — the same deny rules
apply; duplication risks drift). Validating the proxy endpoint's resolved IP at fetch time
(noted as a hardening option but out of scope — the proxy endpoint is operator-configured and
save-time validated; the SSRF threat model targets the *fetched* URL, which the existing
`SafeResolver` covers).

---

## D5 — Effective-policy resolution: a pure chain + cached orchestrator (mirror profiles)

**Decision**: A pure `app_shared/access/resolution.py` (SQLAlchemy/Redis/FastAPI-free)
mirroring `app_shared/profiles/resolution.py`:

- `resolve_effective_policy(*, domain_rule_policy_id, workspace_default_policy_id,
  global_default_policy_id, visible_ids) -> ResolvedPolicy | NONE_RESOLVED` — precedence:
  matching **enabled** domain rule → workspace default → global default; a candidate counts
  only if present in `visible_ids` (own+global), else falls through (dangling/cross-workspace
  tolerated).
- `select_domain_rule(rules, *, domain, url) -> DomainAccessRule | None` — pure "most
  specific match wins": among enabled rules matching the domain, a URL-pattern match beats a
  domain-only rule (Edge Case: URL-pattern rule wins).
- Redis cache-key + value codec (`access_resolution_cache_key`, `encode`/`decode`) so the
  chain is walked **once per `(workspace, competitor, domain, url_pattern)` group** and shared
  across every match in the group.

The orchestrator `apps/api/app/services/access_resolution.py` (and a duplicated bounded-load
shape in the spider, `apps -> libs` only) drives the pure core with the DB loads + Redis
cache, exactly like `services/profile_resolution.py`.

**Rationale**: Principle IV forbids per-match resolution queries (N+1 at 10k–20k matches);
§9 mandates batch-resolve + cache. FR-007's precedence and the "most specific match" edge
case are pure decisions → unit-testable with no infra, like the SPEC-06 resolution core.

**Alternatives considered**: Resolving inside the spider per match with direct queries
(rejected — N+1 amplification, violates Principle IV). A SQL-side `ORDER BY specificity`
(rejected — untestable precedence, couples to DB, cannot cache).

---

## D6 — Access-attempt engine: pure `next_method` + proxy assignment

**Decision**: A pure `app_shared/access/engine.py` (stdlib only) that turns a resolved policy
+ attempt history into the next transport decision:

- `next_attempt(strategy, *, attempt_number, max_retries, use_proxy_on_first_attempt,
  use_proxy_on_retry, allow_browser_fallback, preferred_method=None, proxy_budget_exhausted=
  False) -> AttemptPlan | STOP` where `AttemptPlan` carries the chosen `AccessMethod` and a
  `use_proxy: bool`. Encodes:
  - `DIRECT_ONLY` → only `DIRECT_HTTP`/`DIRECT_HTTP_RETRY`, never a proxy (FR-008 scenario 2).
  - `DIRECT_THEN_PROXY` → first attempt direct, retry via `PROXY_HTTP` when
    `use_proxy_on_retry` (FR-008 scenario 1).
  - `PROXY_FIRST` / `RESIDENTIAL_ONLY` → first attempt already proxied.
  - `BROWSER_FALLBACK` / `allow_browser_fallback` → final fallback is `PLAYWRIGHT_PROXY`
    (**signalled only** — SPEC-14 executes it).
  - Unknown domain default chain: `DIRECT_HTTP → DIRECT_HTTP_RETRY → PROXY_HTTP →
    PLAYWRIGHT_PROXY → STOP`; a learned domain starts from `preferred_method`.
  - `max_retries = 0` → exactly one attempt then STOP (Edge Case).
  - `proxy_budget_exhausted` → skip proxy steps, fall through per strategy, else STOP with the
    caller mapping it to `LIMIT_REACHED` (FR-010).
- `assign_proxy(policy, providers, *, attempt_number, sticky_session, rotate_per_request,
  visible_providers) -> ProxyAssignment | None` — picks provider + country per policy/domain
  rule, honoring rotation vs sticky-session; returns `None` (with a fall-back/`PROXY_FAILED`
  signal) when the referenced provider is disabled/deleted (Edge Case: degrade gracefully).

**Rationale**: This is the behavioral core of US2 ("access policy controls direct/proxy
behavior"). Keeping it pure makes the full strategy × attempt-number × budget matrix
exhaustively unit-testable (SC-001 "100% of runs follow the configured sequence"), mirroring
the SPEC-09 pure alert engine. The spider is then a thin consumer that executes the plan.

**Alternatives considered**: Embedding the decision logic in the spider `errback`/retry loop
(rejected — not unit-testable without Scrapy/reactor; couples determinism to infra). A Scrapy
`RetryMiddleware` subclass holding the state (rejected as the *authority* — the middleware may
*execute* the plan, but the decision must stay pure and testable).

---

## D7 — Proxy monthly budget + policy rate ceilings: Redis counters, never row scans

**Decision**: `app_shared/access/budget.py` (takes a `redis.Redis`-shaped client, like
`security/rate_limit.py`):

- `incr_and_check_monthly_budget(redis, *, provider_id, limit, now) -> BudgetResult` — key
  `proxybudget:{provider_id}:{YYYY_MM}`, `INCR` then set a TTL to the end of the month on
  first hit; returns whether the increment exceeded `limit`. **Incremented per proxied
  request**, reset by the monthly key rollover — never counts `request_attempts` (FR-010,
  §22). Approximate under contention is acceptable (soft ceiling with defined fallback,
  Assumptions).
- `check_rate_ceilings(redis, *, policy_id, domain, per_minute, per_hour, per_day) ->
  RateDecision` — three windowed `INCR`+`EXPIRE` counters (60/3600/86400 s) keyed by
  `ratelimit:{scope}:{window}`; over-ceiling → defer/block, mapped to `RATE_LIMITED` (FR-011).
- `check_domain_cooldown(redis, *, domain, cooldown_seconds)` — a `SET NX EX` gate per domain
  (the SPEC-03 `last_used` pattern) enforcing the per-domain cooldown; per-domain
  `max_concurrent_requests` is expressed as intent (a bounded semaphore key) — the
  **cluster-wide** distributed limiter + fencing in-flight locks are SPEC-11 (out of scope).

**Rationale**: §2 principle "no read-modify-write for hot counters" and §22's explicit "never
by counting `request_attempts` rows" → Redis `INCR`. Reuses the exact
`INCR`+`EXPIRE`-on-first-hit and `SET NX EX` primitives already proven in
`security/rate_limit.py` / `security/last_used.py`. Budget is a soft ceiling → approximate
counting fine.

**Alternatives considered**: Counting `request_attempts` rows for the budget (rejected —
explicitly forbidden, and a hot-path scan at scale). A DB counter column with
read-modify-write (rejected — hot-row contention, Principle VIII). Building the full
distributed limiter here (rejected — that is SPEC-11's scope; this spec enforces only the
policy's own ceilings + per-domain cooldown/concurrency intent).

---

## D8 — Spider integration: extend the existing seams, add HttpProxyMiddleware

**Decision**: Extend `apps/scrapers/price_monitor/spiders/generic_price_spider.py` at its
existing seams — `_request_for` (choose first-attempt `access_method` + proxy from the
resolved policy; set `request.meta['proxy']` + a `Proxy-Authorization` header for
`PROXY_HTTP`), `errback`/retry (re-yield a follow-up request per the pure engine's
`next_attempt`, switching to proxy on retry, up to `max_retries`), and `_build_result` (stamp
each `ScrapeResult`'s `access_method`/`proxy_provider_id`/`proxy_country` and
`attempt_number` instead of the hardcoded `DIRECT_HTTP`). Scrapy's built-in
`HttpProxyMiddleware` (reads `request.meta['proxy']`) is enabled in `settings.py`; the proxy
password is decrypted via `app_shared.security.encryption.decrypt_secret` off-reactor inside
`run_in_thread` (never on the reactor thread, never logged). The budget `INCR` fires for each
proxied request; the persistence pipeline is **unchanged** (it already writes the attempt
rows).

**Rationale**: Task brief: "do not rebuild the spider; extend/hook it." The transport item +
pipeline already flow the proxy fields end-to-end, so only the decision + `request.meta`
wiring is new. Keeping decryption + Redis off-reactor honors Principle V.

**Alternatives considered**: A brand-new proxy spider (rejected — duplicates the spider; the
brief forbids it). Decrypting on the reactor thread (rejected — Principle V reactor-safety;
crypto + Redis are blocking).

---

## D9 — New enums live in `app_shared.enums`; `AccessMethod`/`ScrapeErrorCode` reused

**Decision**: Add three `StrEnum`s to `app_shared/enums.py`:
- `AccessStrategy`: `DIRECT_ONLY, DIRECT_THEN_PROXY, PROXY_FIRST, RESIDENTIAL_ONLY,
  BROWSER_FALLBACK` (§22 access-policy strategies).
- `ProxyType`: `DATACENTER, RESIDENTIAL, MOBILE` (§22 proxy provider types).
- `ProxyProviderStatus`: `ACTIVE, DISABLED` (Clarifications Q4 — doc-silent, defaulted).

Reuse `AccessMethod` (already all four members) and `ScrapeErrorCode` (already includes
`PROXY_FAILED`, `RATE_LIMITED`, `HTTP_429`, `HTTP_403`, `TIMEOUT`, `DNS_ERROR`, `BLOCKED`,
`LIMIT_REACHED`, `UNKNOWN_ERROR` — the full FR-013 set). No widening migration needed (enums
render as app-validated `VARCHAR`).

**Rationale**: Consistency with the established `enum_column`/`StrEnum` convention; the error
and access-method vocabularies were deliberately declared forward-compat in SPEC-07 exactly
so this spec adds no migration on those columns.

**Alternatives considered**: A Postgres-native enum (rejected — the repo forbids it). Reusing
`RecordStatus`/`CompetitorStatus` for the provider status (rejected — different value casing
and semantics; a dedicated enum is clearer and matches §22).

---

## D10 — New API scopes + CRUD endpoints follow the dual-scope router precedent

**Decision**: Add six `Scope` members to `app_shared/security/scopes.py`:
`PROXY_PROVIDERS_READ/WRITE`, `ACCESS_POLICIES_READ/WRITE`, `DOMAIN_RULES_READ/WRITE`. Three
routers under `apps/api/app/routers/` follow `routers/scrape_profiles.py`: dual-scope reads
via `visible_*` selects (own+global) and own-only writes via `owned_*` (a global/other-
workspace id 404s through the tenant path) for providers/policies; standard `scoped_select`
CRUD for domain rules. Cursor pagination + the `{"error": {"code": ...}}` envelope +
`require_scopes(...)` gating are reused unchanged. Proxy-provider responses **omit** the
password entirely (only a boolean `has_password`); create/update accept a plaintext
`password` and encrypt it via D3.

**Rationale**: Matches the ratified SPEC-06 dual-scope endpoint pattern and the §24 API
conventions (`/v1`, cursor pagination). SC-003 (no plaintext password in any response) is
enforced structurally by the response schema never carrying the field.

**Alternatives considered**: Returning a masked password string (rejected — still leaks
length/format; a boolean presence flag is safer). Write-through of globals from the tenant
path (rejected — globals are system-managed, read-only to tenants, per FR-006).
