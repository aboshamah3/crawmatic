# Implementation Plan: Access Policies, Proxies & Request Attempts

**Branch**: `010-access-policies-proxies` (not on a git branch; feature dir is the anchor) | **Date**: 2026-07-04 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/010-access-policies-proxies/spec.md`

## Summary

Give the scraping engine **controlled access behavior**: store how competitor sites are
reached (direct vs proxy) and log every fetch attempt. Three new configuration tables —
`proxy_providers` and `access_policies` (both **dual-scope**: `workspace_id IS NULL` =
global read-only default, mirroring SPEC-06 `scrape_profiles`) and `domain_access_rules`
(**tenant-only**, `workspace_id NOT NULL`, standard RLS — it binds a workspace-owned
`competitor_id`, so a global row is nonsensical; matches §22's non-nullable column). A new
**Fernet encryption helper** with a versioned keyring (`key_version` per encrypted field,
decrypt-old / re-encrypt / retire rotation) protects proxy passwords, which are never
returned in plaintext. Two **pure, framework-free engines** in `app_shared/access/`: a
`resolve_effective_policy` chain (enabled matching domain rule — URL-pattern over
domain-only — overrides the competitor/workspace default) and an `access_attempt` engine
that, given a policy + attempt history + proxy-budget signal, returns the next
`AccessMethod` (`DIRECT_HTTP → DIRECT_HTTP_RETRY → PROXY_HTTP → PLAYWRIGHT_PROXY → STOP`)
and proxy assignment (rotation vs sticky) — exhaustively unit-testable. Redis holds the
**monthly proxy-budget counter** (per-proxied-request `INCR` keyed by `YYYY_MM`, never a
`request_attempts` scan) and the **policy rate ceilings** (per-min/hour/day) + per-domain
cooldown. The existing SPEC-07 spider is **extended** (not rebuilt) to consume the resolved
policy, set `request.meta['proxy']` for `PROXY_HTTP`, retry per policy, and stamp each
`ScrapeResult`'s `access_method`/`proxy_provider_id`/`proxy_country` — the **already-built**
`BatchedPersistencePipeline` then writes the `RequestAttempt` rows off-reactor and batched
(US3 is largely a wiring finish, not a new table).

The two engines are the acceptance core and are DB/Redis/Scrapy-free (pure, exhaustively
unit-testable, incl. every strategy × attempt-number branch); DB/Redis/Celery/spider paths
use skip-clean integration tests (SPEC-05..09 convention — no live infra in this build
environment).

## Technical Context

**Language/Version**: Python 3.13 (repo-wide, `uv` workspace; `requires-python >=3.13,<3.14`)

**Primary Dependencies**: SQLAlchemy 2.x + Alembic (models/migration), `cryptography`
(Fernet — already resolved in `uv.lock`), Redis (`redis` sync client — budget/ceiling
counters), FastAPI (CRUD endpoints), Scrapy/Twisted (spider integration — extend only) —
all already locked (Constitution: Locked stack). The two pure engines depend on **stdlib
only** (`enum`, `dataclasses`).

**Storage**: PostgreSQL via PgBouncer (transaction pooling); uuidv7 PKs, RLS
(`SET LOCAL app.workspace_id`), dual-scope global-readable RLS for `proxy_providers`/
`access_policies`. Redis (`noeviction` instance) for monthly proxy budget + rate ceilings +
per-domain cooldown. `request_attempts` (partitioned) already exists (SPEC-07).

**Testing**: pytest. Pure engines → exhaustive unit tests (every strategy/attempt/budget
branch; resolution precedence table; keyring encrypt/decrypt/rotate round-trips);
DB/Redis/Celery/spider/API → integration tests that **skip cleanly** when infra is absent
(SPEC-01..09 precedent).

**Target Platform**: Linux multi-service deployment (api-service, worker-service,
scrapyd-http-service, …).

**Project Type**: Backend monorepo (`uv` workspace) — `libs/shared` (`app_shared`),
`libs/scrape-core` (`scrape_core`), `apps/api`, `apps/scrapers`, `apps/workers`.

**Performance Goals**: 2,000 products & 10k–20k matches per workspace. Effective-policy
resolution is **batch-resolved once per `(competitor_id, domain, url_pattern)` group and
Redis-cached** (Principle IV — never walked per match). Attempt logging stays batched and
off the reactor thread (reuses the SPEC-07/08 pipeline; no new hot path). Budget/ceiling are
cheap `INCR`/`EXPIRE`, never a row scan (Principle VIII, §22).

**Constraints**: No blocking Redis/DB on the Scrapy reactor thread (all Redis budget/ceiling
checks off-reactor via `run_in_thread`, or on the API/worker side); proxy passwords never
leave the process in plaintext and never appear in an API response; SSRF validation reuses
the existing `validate_competitor_url` (save-time) + `validate_resolved_target`/`SafeResolver`
(fetch-time) — no new validator; single-head Alembic migration chaining onto `e4a75b48360c`;
`app_shared` MUST NOT import Scrapy/Twisted/FastAPI or `apps/*`.

**Scale/Scope**: 3 new tables + 3 new enums + 1 encryption keyring helper + 2 pure engines +
1 resolution orchestrator + 1 budget/ceiling Redis helper + spider integration (extend) + 3
CRUD routers/schema sets + 4 API scopes. **Out of scope (deferred):** actual Playwright
rendering (SPEC-14 — here only the `allow_browser_fallback` flag / `PLAYWRIGHT_PROXY`
`AccessMethod` intent); the cluster-wide distributed domain rate limiter + in-flight match
locks (SPEC-11 — here only each policy's own per-min/hour/day ceilings + per-domain
cooldown/concurrency intent); learned preferred-method / strategy optimizer (SPEC-12 — the
engine accepts a `preferred_method` input but learning is later); `request_attempts`
retention/partition maintenance (later); key-management provisioning (operational).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Status | How this plan complies |
|-----------|--------|------------------------|
| **I. API-First, Service-Oriented** | PASS | Engines + models + enums + encryption helper live in `app_shared` (scraping-free, stdlib/crypto only). The spider (`apps/scrapers`) consumes the pure engine + orchestrator by importing `libs/*` only — never another `apps/*`. Proxy budget increment happens where the proxied request is decided (off-reactor via `run_in_thread`). No new Celery task. |
| **II. Workspace Isolation (NON-NEGOTIABLE)** | PASS | `domain_access_rules` → `WorkspaceScopedBase` + `emit_rls_policy` + `WORKSPACE_OWNED_MODELS` (own-only). `proxy_providers`/`access_policies` are dual-scope → nullable `workspace_id`, `emit_global_readable_rls_policy` (own OR global read; own-only write), dedicated `app_shared.access.repository` visible/owned selects (the SPEC-06 pattern), **not** in `WORKSPACE_OWNED_MODELS`. No-context read → zero tenant rows (globals still readable). Cross-workspace tests required. |
| **III. Variant-Level Pricing & Explicit Matching** | PASS (n/a) | No pricing/matching logic introduced. Domain rules key on `competitor_id` + domain, not variants. |
| **IV. Database-Driven Configuration** | PASS | Access behavior is DB-configured (policies/providers/domain rules), not hardcoded (§9). Effective-policy resolution is **batch-resolved once per group and Redis-cached** (short TTL keyed by workspace/competitor/url_pattern) — mirrors `profiles/resolution.py`; never an N+1 per-match walk. Budget/ceiling knobs are env-tunable `Settings`. |
| **V. Disciplined Scraping Runtime (NON-NEGOTIABLE)** | PASS | Attempt logging reuses the existing off-reactor, batched `BatchedPersistencePipeline` (`run_in_thread` + `workspace_txn`) — no new blocking path. All Redis budget/ceiling round-trips run off the reactor thread (spider side via `run_in_thread`; API/worker side natively). No `time.sleep`, no sync commit on the reactor. Spider persists only; still no analysis in-spider. |
| **VI. Internal-Only & Legally Compliant Access (NON-NEGOTIABLE)** | PASS | Access methods restricted to the four internal `AccessMethod` members; **no external unlocker/scraping API**. `proxy_providers.base_url` (and every fetch URL/redirect hop) validated by the existing SSRF layer at save time (`validate_competitor_url`) and fetch time (`SafeResolver`/`validate_resolved_target`) — DNS re-resolution + per-hop re-validation preserved. `PLAYWRIGHT_PROXY` is only signalled, not executed (SPEC-14). |
| **VII. Monetary & Extraction Correctness** | PASS (n/a) | No money/extraction logic. `monthly_budget_limit` is an integer request count, not a currency amount. |
| **VIII. Scale-Safe Data & Concurrency** | PASS | `request_attempts` stays monthly-partitioned from birth (SPEC-07, PK includes `created_at`); this spec adds no un-partitioned append table. Proxy budget enforced by a Redis monthly `INCR`, **never** by counting `request_attempts` (FR-010, §22). Resolution cached (no per-match query storm). All Postgres traffic via PgBouncer; `SET LOCAL` only. Cluster-wide limiter deferred to SPEC-11 (documented). |

**Gate result: PASS** — no violations; Complexity Tracking table left empty. (The dual-scope
departure for `proxy_providers`/`access_policies` is the already-ratified SPEC-06 pattern,
not a new deviation.)

## Project Structure

### Documentation (this feature)

```text
specs/010-access-policies-proxies/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── models-access.md            # 3 ORM shapes + enums
│   ├── migration-access.md         # single-head migration + RLS (dual-scope + tenant)
│   ├── encryption.md               # Fernet keyring helper + Settings + rotation
│   ├── access-repository.md        # dual-scope visible/owned selects (SPEC-06 pattern)
│   ├── policy-resolution.md        # pure effective-policy chain + orchestrator + cache
│   ├── access-engine.md            # pure next-method / proxy-assignment engine
│   ├── budget-ceilings.md          # Redis monthly budget + per-min/hour/day + cooldown
│   ├── spider-integration.md       # extend generic_price_spider (meta proxy, retry, attempts)
│   └── api-access.md               # CRUD endpoints + scopes for the 3 tables
├── spec.md
└── tasks.md             # /speckit-tasks output (NOT created here)
```

### Source Code (repository root)

```text
libs/shared/app_shared/
├── enums.py                         # +AccessStrategy, +ProxyType, +ProxyProviderStatus
│                                    #  (AccessMethod, ScrapeErrorCode already exist — reuse)
├── models/
│   ├── access.py                    # NEW — ProxyProvider, AccessPolicy, DomainAccessRule
│   └── __init__.py                  # register the 3 models (metadata + re-export)
├── repository.py                    # add ONLY DomainAccessRule to WORKSPACE_OWNED_MODELS
├── security/
│   └── encryption.py                # NEW — versioned Fernet keyring (encrypt/decrypt/rotate)
├── access/                          # NEW package — PURE engines + repository + orchestration
│   ├── __init__.py
│   ├── engine.py                    #   pure: next AccessMethod + proxy assignment (stdlib only)
│   ├── resolution.py                #   pure: effective-policy precedence chain + cache codec
│   ├── repository.py                #   dual-scope visible/owned selects (SQLAlchemy only)
│   └── budget.py                    #   Redis monthly budget + rate ceilings + cooldown
└── config.py                        # +ENCRYPTION_KEYS/ENCRYPTION_PRIMARY_KEY_VERSION,
                                     #  +PROXY_BUDGET/ACCESS_* tuning knobs

libs/shared/app_shared/security/scopes.py
                                     # +PROXY_PROVIDERS_*, +ACCESS_POLICIES_*,
                                     #  +DOMAIN_RULES_* (read/write) Scope members

alembic/versions/
└── <newrev>_access_policies_proxies_tables.py   # NEW — chains onto e4a75b48360c (head)

apps/api/app/
├── routers/
│   ├── proxy_providers.py           # NEW — CRUD (password never in response)
│   ├── access_policies.py           # NEW — CRUD (dual-scope reads, own-only writes)
│   └── domain_access_rules.py       # NEW — CRUD (tenant-only)
├── schemas/access.py                # NEW — request/response envelopes (no plaintext pw out)
├── services/access_resolution.py    # NEW — orchestrator (bounded loads + Redis cache),
│                                    #  mirrors services/profile_resolution.py
└── main.py                          # include the 3 new routers

libs/scrape-core/scrape_core/
└── (no new module) pipelines.py already writes RequestAttempt with proxy fields — unchanged

apps/scrapers/price_monitor/
├── spiders/generic_price_spider.py  # EXTEND — resolve effective policy per group; choose
│                                    #  first-attempt access_method + proxy; retry per policy
│                                    #  (errback re-yields via PROXY on retry); stamp each
│                                    #  ScrapeResult's access_method/proxy_provider_id/country
└── settings.py                      # ensure HttpProxyMiddleware enabled (meta['proxy'])

tests/  (per-package, mirroring SPEC-09 layout)
├── unit/    — exhaustive engine tests (strategy×attempt matrix, resolution precedence,
│              keyring encrypt/decrypt/rotate, budget/ceiling counter math)
└── integration/ — skip-clean: migration+RLS (dual-scope + tenant), CRUD endpoints
                   (password redaction, cross-workspace, global read-only), budget/cooldown
                   Redis, spider proxy-assignment + attempt-row emission
```

**Structure Decision**: Reuse the established monorepo layout exactly. The **pure engines**
go in a new `app_shared/access/` package (mirrors `app_shared/alerts/` and
`app_shared/profiles/` — pure core + SQLAlchemy-only repository + orchestrator seam), the
**ORM** in `app_shared/models/access.py` (mirrors `models/scrape_profiles.py` for the
dual-scope tables and `models/observations.py` for the tenant-scoped one), the **encryption
helper** in `app_shared/security/encryption.py` (alongside `jwt.py`/`rate_limit.py`), the
**migration** chains onto the single head `e4a75b48360c`, the **orchestrator** in
`apps/api/app/services/access_resolution.py` (mirrors `services/profile_resolution.py`), the
**endpoints** follow the `routers/scrape_profiles.py` dual-scope, scope-gated,
`scoped`/`visible` select, cursor-paginated conventions, and the **spider** is extended at
its existing `_request_for`/`errback`/`_build_result` seams (the transport `ScrapeResult`
already carries `access_method`/`proxy_provider_id`/`proxy_country`; the pipeline already
persists them — so US3 needs no persistence change).

## Complexity Tracking

> No Constitution Check violations — table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |
