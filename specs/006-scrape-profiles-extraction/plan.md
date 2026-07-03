# Implementation Plan: Scrape Profiles & Extraction Rules

**Branch**: `006-scrape-profiles-extraction` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/006-scrape-profiles-extraction/spec.md`

## Summary

Deliver the DB-driven extraction-configuration layer on top of the SPEC-02 foundation, SPEC-03 isolation/auth, SPEC-04 catalog, and SPEC-05 competitors/matches: the **`scrape_profiles`** table (exact §22 shape), the profile **validation layer** (enums, regex-compile + ReDoS screen, cookie session/auth deny, `validation_rules`/`confidence_rules`/money shape checks), a **config-resolution service** that returns the single scrape profile that applies to a match by walking match → domain-strategy(no-op) → competitor → workspace → global default (batch-resolved per `(competitor_id, url_pattern)`, Redis-cached), the DB-tunable **default-confidence** accessor, and the `/v1/scrape-profiles` CRUD + **set-based bulk-upsert** + assignment enforcement. No extraction is executed here — selectors/XPath/regex/JSON are stored and syntactically/shape-validated only (SPEC-07+ runs them).

The one structural novelty versus SPEC-03/04/05: `scrape_profiles.workspace_id` is **nullable** (NULL = a global/shared default), so this is the first **dual-scope** table — workspace rows are RLS-isolated exactly like prior entities, while global (`workspace_id IS NULL`) rows are readable by every workspace and writable by none through the tenant path. That drives three deliberate departures from the SPEC-05 mould, each justified below: it does **not** use `WorkspaceScopedBase` (which forces `NOT NULL`), it is **not** in `WORKSPACE_OWNED_MODELS` (whose `scoped_select` would hide global rows), and it gets a **custom global-readable RLS policy pair** instead of the standard single `emit_rls_policy`.

Concretely this feature adds:
- `app_shared`:
  - `models/scrape_profiles.py` — the `ScrapeProfile` ORM model on `Base + TimestampMixin` (not `WorkspaceScopedBase`), with the §22 columns, a **nullable** indexed `workspace_id` + nullable FK → `workspaces.id`, and **two partial unique indexes** ((workspace_id, name) WHERE workspace_id IS NOT NULL for tenant rows; (name) WHERE workspace_id IS NULL for global rows).
  - `models/rls.py` (extend) — `emit_global_readable_rls_policy(table)`: ENABLE + FORCE RLS, a `FOR SELECT` policy `USING (workspace_id IS NULL OR workspace_id = <ctx>)` and a `FOR ALL` write policy `USING (workspace_id = <ctx>) WITH CHECK (workspace_id = <ctx>)` — global rows readable by all, unwritable via the tenant path (FR-021).
  - `enums.py` (extend) — `ScrapeProfileMode`, `AdapterKey`, `VariantStrategy`.
  - `profiles/validation.py` — **pure** validators: enum coercion, `compile_regex_or_reject` (compile + catastrophic-backtracking heuristic), `reject_session_cookies` (auth/session cookie-name deny heuristic), `validate_validation_rules`, `validate_confidence_rules`, all raising a structured `ProfileValidationError(field, code, message)`. Reuses `app_shared.money.parse_money` (extracted pure money-boundary check) for finite/scale/non-negative money.
  - `profiles/confidence.py` — the §17 default confidences + `DEFAULT_MIN_ACCEPTED_CONFIDENCE=0.75` + `DEFAULT_PROMOTION_THRESHOLD=0.85` as constants, plus `resolve_confidence_rules(profile_rules)` merging a profile's overrides over the defaults (DB-tunable, FR-011).
  - `profiles/resolution.py` — the **pure** resolution core: `group_key`, `group_matches`, `resolve_group` (walk domain-strategy(None)→competitor→workspace→global with a `visible_ids` guard), `apply_match_override`, `NONE_RESOLVED` sentinel, `resolution_cache_key(workspace_id, competitor_id, url_pattern)`. The domain-strategy step is a tolerated no-op returning `None` (FR-015).
  - `profiles/repository.py` — dual-scope query helpers: `visible_profiles_select(ws)` (own OR global, read), `owned_profile_select(ws)`/`owned_profile_get(...)` (own only, write/manage — never global, FR-021), `assert_profile_assignable(session, ws, profile_id)` (visible-or-None → OK; cross-workspace/dangling → reject, FR-013).
  - `profiles/upsert.py` — **pure** set-based tenant bulk-upsert core: single `INSERT ... ON CONFLICT (workspace_id, name) WHERE workspace_id IS NOT NULL DO UPDATE`, `dedup_last_wins` on `(workspace_id, name)` (reused from catalog), reject-and-report split (FR-020). Never writes a global row.
  - `security/scopes.py` (extend) — `scrape_profiles:read` / `scrape_profiles:write`.
  - `config.py` (extend) — `PROFILE_RESOLUTION_CACHE_TTL_SECONDS: int = 30`.
  - `models/identity.py` + `models/competitors_matches.py` (extend) — add the three plain FKs `default_scrape_profile_id`/`scrape_profile_id → scrape_profiles(id) ON DELETE SET NULL`.
- `apps/api`:
  - `schemas/scrape_profiles.py` — Pydantic Create/Update/Response/ListResponse + Bulk request/result with a `rejected[]`, + the workspace-default assignment body.
  - `routers/scrape_profiles.py` — scope-gated `/v1/scrape-profiles` CRUD + `POST /v1/scrape-profiles/bulk-upsert` + `PUT /v1/scrape-profiles/workspace-default`; validation via the shared validator module; reads own+global, writes own-only.
  - `routers/competitors.py` / `routers/matches.py` (extend) — call `assert_profile_assignable` wherever `default_scrape_profile_id`/`scrape_profile_id` is set (create/update/bulk) so a cross-workspace assignment is a clean `422` (FR-013).
  - `services/profile_resolution.py` — a thin `apps/api` adapter that loads the bounded inputs (workspace default, competitor defaults, global default, visible-id set) and drives the pure core + Redis cache; exposed for internal batch callers (SPEC-07 refresh), not a new public endpoint in this spec.
  - `main.py` (extend) — include the scrape-profiles router.
- repo root: one Alembic migration `<rev>_scrape_profiles_table.py` creating `scrape_profiles` (+ partial uniques + workspace FK + custom global-readable RLS) and `ALTER`ing the three existing nullable columns into FKs `ON DELETE SET NULL`; `down_revision = f4c8a391d5c9` (current head — verified single head).

Everything DB/Redis-independent is fully unit-tested **here** (model/index/partial-unique shapes + names ≤63 bytes, global-readable RLS DDL render, the full enum/regex/cookie/validation-rules/confidence-rules validator corpus incl. money finite/scale/non-negative, resolution-chain ordering + visibility fall-through over in-memory inputs, batch grouping, cache-key derivation, none-resolved sentinel, confidence-default merge, bulk-upsert statement construction on the partial-index arbiter + last-wins dedup + reject-report, pagination reuse, scope-gating wiring, the dual-scope repository predicates, migration offline DDL render incl. the three `ON DELETE SET NULL` FKs, single head). Live-Postgres/Redis items (actual CRUD, RLS row denial incl. global read + global write-block, cross-workspace assignment, Redis TTL/invalidation, migration online run, end-to-end batch resolution) are **authored and marked** for a PG/Redis-capable host — no Docker daemon / live Postgres in this build env.

## Technical Context

**Language/Version**: Python 3.13 (`requires-python = ">=3.13,<3.14"`; uv workspace).

**Primary Dependencies**:
- Existing only — no new third-party deps. SQLAlchemy 2.0 (sync) incl. the PostgreSQL dialect `insert(...).on_conflict_do_update(...)` (partial-index arbiter) for the set-based profile upsert; psycopg 3; Alembic; FastAPI + Pydantic v2 (`apps/api`); `redis` (existing `app_shared.redis_client`) for the resolution cache; stdlib `re` (regex compile + ReDoS heuristic), `decimal` (money), `hashlib` (cache-key url_pattern hashing).
- `app_shared` MUST NOT import FastAPI (framework-agnostic) and MUST NOT import scrapy/twisted/playwright (unchanged import-boundary test). The model, enums, validators, confidence defaults, resolution **core**, dual-scope repository, and upsert **core** live in `app_shared`; the FastAPI schemas/routers/deps and the cache-driving orchestrator live in `apps/api`.

**Storage**: PostgreSQL 17. App requests connect through PgBouncer (transaction pooling); the migration connects directly (`MIGRATION_DATABASE_URL`). Workspace context is set per-transaction by the SPEC-03 auth seam (`set_config('app.workspace_id', :wsid, true)`) before any profile query. RLS enabled+forced on `scrape_profiles` in the creating migration, with the **custom global-readable policy pair** (read own+global; write own-only). Redis (existing per-process client) holds the short-TTL resolution cache.

**Testing**: pytest. DB/Redis-independent logic unit-tested here (validators, resolution ordering/grouping/cache-key over in-memory inputs, confidence-default merge, upsert compile-to-SQL with no execution, RLS/DDL render). Live-DB/Redis items authored and skipped when no reachable Postgres/Redis is present (same markers as SPEC-03/04/05).

**Target Platform**: Linux server / containers. Only `apps/api` is publicly exposed.

**Project Type**: Backend monorepo (uv workspace). Spans `app_shared` (model, enums, validators, confidence, resolution core, repository, upsert core) and `apps/api` (schemas, routers, cache orchestrator), plus repo-root Alembic.

**Performance Goals**: Batch resolution performs a **bounded** number of DB lookups per batch regardless of match count — one workspace-default read, one competitor-default `IN (...)` read over distinct competitor ids, one global-default read, one visible-id `IN (...)` read — and one grouped chain-walk per distinct `(competitor_id, url_pattern)`, not per match (SC-004, FR-018). Repeat group resolutions within the TTL are Redis hits (SC-005). Bulk profile upsert is a single `INSERT ... ON CONFLICT DO UPDATE` for all valid rows (no per-row loop, SC-008). List endpoints reuse the SPEC-04 keyset `(created_at, id)` cursor pagination, default 50 / max 500. Validators are O(pattern/bundle size), pure CPU, no I/O.

**Constraints**: Transaction-pooling-safe only (`SET LOCAL`/`set_config(...,true)`; no session advisory locks; `prepare_threshold=None`). RLS fails closed (zero rows) with no workspace context; global rows remain readable via the `workspace_id IS NULL` disjunct. The tenant write path can never create/edit/delete a global profile (RLS write policy `WITH CHECK (workspace_id = <ctx>)`, FR-021); global profiles are managed out-of-band via a privileged/BYPASSRLS platform path (no automatic global seed in this spec). Money in the rules bundles follows §19 exactly (Decimal, reject NaN/Infinity, reject over-scale, no float). Regex must compile and pass the ReDoS screen. Session/auth cookies rejected. `app_shared` stays FastAPI-free and scrapy-free. No live Postgres/Redis in this build env.

**Scale/Scope**: Config layer for 10k–20k matches per workspace per refresh (§39) — resolution is batch/grouped/cached to avoid N+1 (Principle IV/VIII). This spec adds **exactly one** table (`scrape_profiles`), promotes **three** existing nullable columns to FKs, and delivers the profile CRUD/bulk/assignment + resolution service + validators. **No** access_policies / proxy_providers / domain_access_rules (SPEC-10), **no** domain_strategy_profiles (SPEC-12), **no** spider execution / price_observations (SPEC-07), **no** execution of any stored selector/regex/JSON.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Relevance | How this plan satisfies it |
|-----------|-----------|----------------------------|
| **I. API-First / Service boundaries** | New `app_shared` modules + FastAPI in `apps/api` | The `ScrapeProfile` model, enums, validators, confidence defaults, resolution **core**, dual-scope repository, and upsert **core** live in `app_shared` and import only sqlalchemy/redis/stdlib — never fastapi, never scrapy/twisted/playwright. The import-boundary test is extended to cover `app_shared.models.scrape_profiles` and `app_shared.profiles.*`. Pydantic schemas, the `/v1` router, scope-gating, and the cache-driving orchestrator live in `apps/api`, importing `app_shared` one-way. Only `apps/api` is publicly exposed. **PASS** |
| **II. Workspace Isolation (NON-NEGOTIABLE)** | Core of this spec (dual-scope) | `scrape_profiles` is dual-scope: workspace rows (`workspace_id NOT NULL`) are RLS-isolated exactly like prior entities; global rows (`workspace_id IS NULL`) are shared-read, tenant-unwritable. Because a nullable `workspace_id` means the standard helpers don't fit, isolation is enforced by a **dedicated** repository module (`app_shared.profiles.repository`) whose read helper always constrains to `(workspace_id = ctx OR workspace_id IS NULL)` and whose manage helpers constrain to `workspace_id = ctx` only, plus the **custom global-readable RLS policy pair** (defense-in-depth: read own+global, write own-only). `ScrapeProfile` is intentionally **excluded** from `WORKSPACE_OWNED_MODELS` (its `scoped_select` would wrongly hide global rows); the dual-scope repository is the single sanctioned query path and every profile `select` outside it carries a `workspace_id`/global predicate (guard-coverage decision in research D2). Cross-workspace/dangling assignment references are rejected at write (`assert_profile_assignable`, FR-013) and treated as unset at resolution (FR-017). Cross-workspace + global-read + global-write-block tests authored (live-DB). **PASS** |
| **III. Variant-Level Pricing & Explicit Matching** | Indirect | No matching or pricing logic changes; a profile is assigned to a match/competitor/workspace but resolution never alters the variant-level match identity. **PASS (N/A)** |
| **IV. Database-Driven Configuration** | Direct core of this spec | This *is* the DB-driven config layer (§9): scrape profiles are DB rows; validation rules, confidence thresholds, and per-method confidences are DB-tunable (`validation_rules`/`confidence_rules`, defaults applied by `resolve_confidence_rules`, never hardcoded in the extractor, FR-011). The profile-resolution chain is **batch-resolved per `(competitor_id, url_pattern)` and Redis-cached** (short TTL keyed by `(workspace_id, competitor_id, url_pattern)`) — never walked per match (FR-018/FR-019, Principle IV verbatim). **PASS** |
| **V. Disciplined Scraping Runtime (NON-NEGOTIABLE)** | Import boundary only | No scraping code; no stored selector/XPath/regex/JSON is executed. `app_shared` stays scrapy/twisted/playwright-free (see I). Regex is only *compiled* (safety screen), never run against HTML here. **PASS (N/A)** |
| **VI. Internal-only / legal (NON-NEGOTIABLE)** | Cookie guardrail | The `cookies` validator rejects session/authentication cookies (§30 legal guardrail, §22 config guardrail), accepting only non-identifying technical cookies (currency/locale). No login/credentialed/external-unlocker surface is introduced. **PASS** |
| **VII. Monetary & Extraction Correctness (NON-NEGOTIABLE)** | `validation_rules` money + confidence | Money in `validation_rules` (`min_price`/`max_price`) is validated with §19 semantics via the shared `app_shared.money` boundary: Decimal only, reject `NaN`/`Infinity`, reject over-scale (>4 dp) instead of rounding, non-negative, `min ≤ max` (FR-008/FR-022). Confidence values are constrained to `[0,1]`; the documented default minimum (0.75) and promotion threshold (0.85) plus the §17 per-method defaults are DB-tunable, not baked-in literals (FR-009/FR-011). No cross-currency comparison is introduced. **PASS** |
| **VIII. Scale-Safe Data & Concurrency (NON-NEGOTIABLE)** | Resolution + bulk + FKs | Resolution avoids per-match N+1 by grouping on `(competitor_id, url_pattern)` and bounding DB access to a fixed set of `IN (...)` lookups per batch, cached in Redis (SC-004/SC-005). Bulk upsert is set-based (one `ON CONFLICT DO UPDATE` for all valid rows, SC-008), constructed by a pure builder and compiled-to-SQL in tests. Keyset `(created_at, id)` cursor pagination — no OFFSET scans; capped at 500. UUIDv7 PK; `TIMESTAMPTZ` everywhere. All app traffic through PgBouncer; only `SET LOCAL`. Single linear migration head (CI head guard). The three assignment FKs use `ON DELETE SET NULL` so a profile delete leaves **no** dangling reference (FR-023) without a hot-path scan. `scrape_profiles` is mutable-config, not append-heavy — partitioning (§29) is N/A. **PASS** |

**Technology & Security Constraints (§24/§33/§34)**: Stack lock-in honored (SQLAlchemy+Alembic, PostgreSQL pg-dialect `insert`, psycopg, FastAPI/Pydantic, Redis). Public API versioned under `/v1`; list endpoints cursor-paginated default 50 / max 500 (§24). UUIDv7 public ids (§21). Deletion (FR-023): a profile hard-delete is permitted; `ON DELETE SET NULL` nulls every referencing assignment so resolution stays safe (no block-on-referenced needed). New scope vocabulary `scrape_profiles:read`/`scrape_profiles:write` minted in `app_shared.security.scopes` following the per-entity precedent (§24 lists the `/v1/scrape-profiles` endpoints; §22's scope list predates this spec — the two-scope extension is the minimal faithful addition). Structured error-code vocabulary reused/extended: `VALIDATION_ERROR` (field-specific profile-validation rejections carrying `field`/`code`), `NOT_FOUND`, `FORBIDDEN`, `WORKSPACE_MISMATCH` (cross-workspace assignment), `DUPLICATE_PROFILE` (name conflict), `INVALID_CURSOR`.

**Gate result**: PASS — no violations. The dual-scope departures (no `WorkspaceScopedBase`, excluded from `WORKSPACE_OWNED_MODELS`, custom RLS pair) are **required by** Principle II/the §9 global-default semantics, not a relaxation of them — they are documented in Complexity Tracking as justified deviations from the SPEC-05 mould, with the isolation guarantees preserved by the dedicated repository + custom RLS. Re-checked post-Phase-1 (see end of plan): still PASS.

## Project Structure

### Documentation (this feature)

```text
specs/006-scrape-profiles-extraction/
├── plan.md              # This file (/speckit-plan output)
├── research.md          # Phase 0 — enums; dual-scope model (nullable ws_id, not WorkspaceScopedBase,
│                        #   not in WORKSPACE_OWNED_MODELS) + guard coverage; partial-unique NULL-ws names;
│                        #   custom global-readable RLS pair; FK-promotion (plain FK ON DELETE SET NULL vs
│                        #   composite) decision; resolution chain/grouping/cache/none-resolved + global-default
│                        #   identification; validators (regex ReDoS, cookie deny, validation/confidence/money);
│                        #   confidence defaults; bulk-upsert arbiter; scopes; assignment endpoints; migration head;
│                        #   global-seed out-of-band; unit-vs-live split
├── data-model.md        # Phase 1 — scrape_profiles (exact §22 shape), enums, partial uniques, workspace FK,
│                        #   the 3 promoted assignment FKs, dual-scope RLS, ResolvedProfile transient
├── quickstart.md        # Phase 1 — how to validate (unit here; live CRUD/RLS/global-read/write-block/
│                        #   resolution/cache/migration on a PG+Redis host)
├── contracts/           # Phase 1 — interfaces this feature exposes
│   ├── models-scrape-profiles.md      # ORM shape, partial uniques, workspace FK, enums, dual-scope note
│   ├── rls-global-readable.md         # emit_global_readable_rls_policy — read own+global, write own-only
│   ├── profile-validation.md          # enum/regex-ReDoS/cookie/validation_rules/confidence_rules/money validators
│   ├── confidence-defaults.md         # §17 defaults + resolve_confidence_rules merge accessor
│   ├── config-resolution.md           # resolution core: chain, grouping, visibility guard, cache key, none-resolved
│   ├── profiles-repository.md         # dual-scope query helpers + assert_profile_assignable
│   ├── profiles-bulk-upsert.md        # set-based tenant upsert on the partial-index arbiter + reject-and-report
│   ├── api-scrape-profiles.md         # /v1/scrape-profiles CRUD + bulk-upsert + workspace-default assignment
│   ├── assignment-enforcement.md      # visibility checks added to competitor/match assignment paths
│   └── migration-scrape-profiles.md   # the migration + custom RLS + 3 FKs ON DELETE SET NULL + single head
└── tasks.md             # Phase 2 (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
libs/shared/app_shared/
├── enums.py             # EXTEND: ScrapeProfileMode (HTTP/BROWSER/CUSTOM),
│                        #   AdapterKey (default_http/jsonld_first/selector_only/regex_only/
│                        #     shopify_product_json/woocommerce_store_api/playwright_rendered/custom_adapter),
│                        #   VariantStrategy (PAGE_SINGLE_PRICE/URL_HAS_VARIANT_SELECTED/HTML_VARIANT_TABLE/
│                        #     EMBEDDED_JSON_VARIANTS/SELECT_VARIANT_WITH_PLAYWRIGHT/CUSTOM_VARIANT_ADAPTER)
├── config.py            # EXTEND: PROFILE_RESOLUTION_CACHE_TTL_SECONDS: int = 30
├── money.py             # EXTEND: extract pure `parse_money(value) -> Decimal` (finite/scale/non-negative/
│                        #   no-float) reused by Money.process_bind_param AND profiles.validation
├── repository.py        # UNCHANGED set of WORKSPACE_OWNED_MODELS (ScrapeProfile deliberately NOT added — dual-scope)
├── models/
│   ├── __init__.py      # EXTEND: re-export ScrapeProfile
│   ├── rls.py           # EXTEND: emit_global_readable_rls_policy(table) -> (ENABLE, FORCE, SELECT policy,
│   │                    #   FOR ALL write policy USING+WITH CHECK) — read own+global, write own-only
│   ├── identity.py      # EXTEND: workspaces.default_scrape_profile_id -> FK scrape_profiles(id) ON DELETE SET NULL
│   ├── competitors_matches.py # EXTEND: competitors.default_scrape_profile_id +
│   │                    #   competitor_product_matches.scrape_profile_id -> FK scrape_profiles(id) ON DELETE SET NULL
│   └── scrape_profiles.py # NEW: ScrapeProfile(Base, TimestampMixin) — nullable indexed workspace_id + nullable
│                        #   FK workspaces.id; §22 columns; enum_column mode/adapter_key/variant_strategy; JSONB
│                        #   variant_selector_config/price_transform_rules/validation_rules/confidence_rules/
│                        #   headers/cookies; two PARTIAL unique indexes (tenant WHERE ws IS NOT NULL; global
│                        #   WHERE ws IS NULL); documented defaults (the three *_enabled, variant_strategy,
│                        #   request_timeout_ms)
├── security/
│   └── scopes.py        # EXTEND: SCRAPE_PROFILES_READ = "scrape_profiles:read",
│                        #   SCRAPE_PROFILES_WRITE = "scrape_profiles:write"
└── profiles/
    ├── __init__.py      # NEW
    ├── validation.py    # NEW: ProfileValidationError(field, code, message); coerce enums;
    │                    #   compile_regex_or_reject(pattern) (compile + ReDoS heuristic);
    │                    #   reject_session_cookies(cookies) (auth/session name deny-list + heuristic);
    │                    #   validate_validation_rules(bundle) (required_currency 3-letter; min/max via
    │                    #   parse_money finite/scale/non-neg + min<=max; text fields = list[str]);
    │                    #   validate_confidence_rules(bundle) (values in [0,1]); validate_profile(payload) facade
    ├── confidence.py    # NEW: DEFAULT_CONFIDENCE_RULES (§17 per-method), DEFAULT_MIN_ACCEPTED_CONFIDENCE=0.75,
    │                    #   DEFAULT_PROMOTION_THRESHOLD=0.85; resolve_confidence_rules(profile_rules) merge
    ├── repository.py    # NEW: visible_profiles_select(ws) (own OR global, read); owned_profile_select(ws)/
    │                    #   owned_profile_get(session,id,ws) (own only, manage); profile_visibility_map(...)
    │                    #   + assert_profile_assignable(session, ws, profile_id) (None ok; visible ok;
    │                    #   cross-ws/dangling -> reject, FR-013); GLOBAL_DEFAULT_PROFILE_NAME = "global_default"
    ├── resolution.py    # NEW: ResolvedProfile / NONE_RESOLVED sentinel; group_key(match)=(competitor_id,
    │                    #   url_pattern); group_matches(rows); resolve_group(competitor_default, workspace_default,
    │                    #   global_default, visible_ids, domain_strategy=None) walk (domain-strategy no-op ->
    │                    #   competitor -> workspace -> global, skipping non-visible ids, FR-014/15/16/17);
    │                    #   apply_match_override(group_result, override_id, visible_ids);
    │                    #   resolution_cache_key(ws, competitor_id, url_pattern)
    └── upsert.py        # NEW: build_profiles_upsert(rows) -> single pg Insert.on_conflict_do_update targeting
                         #   the tenant partial unique (index_elements=[workspace_id,name],
                         #   index_where=text("workspace_id IS NOT NULL")); dedup_last_wins on (workspace_id,name)
                         #   (reused from catalog.upsert); prepare_profiles(rows) -> (valid, rejected) applying
                         #   the validators per row (reject-and-report, FR-020). Tenant-only (never global).

apps/api/app/
├── main.py              # EXTEND: include the scrape-profiles router
├── deps.py              # UNCHANGED auth seam; router uses require_scopes("scrape_profiles:...")
├── schemas/
│   └── scrape_profiles.py # NEW: ScrapeProfileCreate/Update/Response/ListResponse; ScrapeProfileBulkUpsertRequest;
│                        #   ScrapeProfileBulkUpsertResult{upserted, profiles, rejected:[{index,name,field,code,reason}]};
│                        #   WorkspaceDefaultProfileAssignment{profile_id: uuid|null}. Reuses app.schemas.catalog.DeleteOutcome.
├── services/
│   └── profile_resolution.py # NEW: cache-driving orchestrator — loads bounded inputs (workspace default,
│                        #   competitor defaults IN(...), global default by reserved name, visible-id set),
│                        #   drives resolution.resolve_group per group with Redis get/set (TTL) +
│                        #   invalidate_resolution_cache(redis, ws, competitor_id) best-effort prefix delete on writes
└── routers/
    ├── scrape_profiles.py # NEW: POST/GET/GET{id}/PATCH/DELETE /v1/scrape-profiles +
    │                    #   POST /v1/scrape-profiles/bulk-upsert + PUT /v1/scrape-profiles/workspace-default
    │                    #   (require_scopes scrape_profiles:read|write); create/update run validate_profile;
    │                    #   list/get read own+global (visible_profiles_select); create/patch/delete manage own-only
    │                    #   (owned_profile_* -> 404 on a global/other-ws id via tenant path, FR-021)
    ├── competitors.py   # EXTEND: assert_profile_assignable on default_scrape_profile_id (create/update, FR-013)
    └── matches.py       # EXTEND: assert_profile_assignable on scrape_profile_id (create/update/bulk, FR-013)

alembic/versions/
└── <rev>_scrape_profiles_table.py  # NEW: create scrape_profiles (§22 shape, 2 partial uniques, nullable
                         #   workspace FK); emit_global_readable_rls_policy("scrape_profiles"); ALTER the three
                         #   existing columns -> FK scrape_profiles(id) ON DELETE SET NULL
                         #   (competitors/competitor_product_matches/workspaces); downgrade drops FKs then table;
                         #   down_revision = f4c8a391d5c9 (current head)

tests/unit/
├── test_import_boundaries.py          # EXTEND: cover app_shared.models.scrape_profiles, app_shared.profiles.*
├── test_scrape_profiles_models.py     # NEW: table/column shapes, 2 partial-unique indexes + predicates,
│                                       #   nullable workspace_id + FK, names <=63 bytes, documented defaults, enums
├── test_rls_scrape_profiles.py        # NEW: emit_global_readable_rls_policy render (ENABLE/FORCE + SELECT own|global
│                                       #   + FOR ALL write own-only WITH CHECK)
├── test_profile_validation.py         # NEW: enum accept/reject; regex compile-ok + un-compilable reject +
│                                       #   catastrophic-pattern reject corpus; cookie technical-accept /
│                                       #   session-auth-reject corpus; validation_rules currency/min<=max/money
│                                       #   finite+scale+non-neg / text-list corpus; confidence_rules [0,1] corpus
├── test_confidence_defaults.py        # NEW: DEFAULT_* values match §17; resolve_confidence_rules merge/override
├── test_profile_resolution.py         # NEW: chain ordering across all precedence combos; visibility fall-through
│                                       #   (dangling/cross-ws -> unset); domain-strategy no-op skip; NONE_RESOLVED;
│                                       #   group_matches one-result-per-group; match override precedence
├── test_profile_resolution_cache_key.py # NEW: resolution_cache_key deterministic + collision-free per tuple
├── test_profiles_repository.py        # NEW: visible_profiles_select emits (ws OR NULL); owned_* emits ws-only;
│                                       #   assert_profile_assignable None/visible ok, cross-ws/dangling reject
├── test_profiles_upsert.py            # NEW: build_profiles_upsert compiles to ON CONFLICT (workspace_id, name)
│                                       #   WHERE workspace_id IS NOT NULL DO UPDATE (one statement, no per-row loop);
│                                       #   last-wins dedup; prepare_profiles valid/rejected split
├── test_scrape_profiles_scope_gating.py # NEW: router declares correct require_scopes (read vs write)
├── test_scrape_profiles_routes_registered.py # NEW: the router is mounted in main.py
├── test_scopes.py                     # EXTEND: scrape_profiles:read/write in the Scope vocabulary
├── test_money.py                      # EXTEND: parse_money extracted pure function (finite/scale/non-neg/no-float)
└── test_migration_offline_scrape_profiles.py # NEW: `alembic upgrade head --sql` renders scrape_profiles +
                                        #   partial uniques + custom RLS + the 3 ON DELETE SET NULL FKs; single head

tests/integration/  (authored, live-DB/Redis-marked — skipped without Postgres/Redis)
├── test_scrape_profiles_crud_live.py       # create w/ validated bundles round-trip; unique name per ws; read/update/
│                                            #   list/delete; invalid payloads (enum/regex/cookie/rules) 422
├── test_scrape_profiles_isolation_live.py  # cross-ws profile invisible to writes; global (ws NULL) readable by all;
│                                            #   tenant path cannot create/edit/delete a global row (RLS write block);
│                                            #   no-context -> zero own rows (global still visible per policy)
├── test_scrape_profiles_bulk_upsert_live.py # set-based; re-push -> update in place; invalid rows reject-reported;
│                                            #   bounded statement count
├── test_profile_assignment_live.py         # assign to competitor/match/workspace-default accepts own+global,
│                                            #   rejects cross-ws; clearing (null) ok; ON DELETE SET NULL nulls refs
└── test_profile_resolution_live.py         # batch resolution precedence end-to-end; grouped (no per-match N+1);
                                             #   Redis cache hit within TTL; write invalidates/expires
```

**Structure Decision**: Backend monorepo (uv workspace), matching SPEC-03/04/05. `app_shared` gains the `ScrapeProfile` model, the extraction enums, the pure validators (regex/cookie/rules/money — the correctness-critical surface), the confidence defaults, the pure resolution core, the dual-scope repository, and the pure upsert core — so every DB/Redis-independent behavior is unit-testable without FastAPI, a live DB, or Redis. FastAPI schemas, the router, scope-gating, and the cache-driving resolution orchestrator live in `apps/api` (keeping `app_shared` FastAPI-free). The one table + custom RLS + three FK promotions land in one repo-root Alembic migration chained onto the current head `f4c8a391d5c9`.

## Complexity Tracking

> The three dual-scope departures below are the only deviations from the SPEC-05 workspace-owned mould. Each is required by the §9 global-default semantics (a `scrape_profiles` row with `workspace_id IS NULL` must be readable by every workspace yet writable by none through the tenant path) and preserves — not relaxes — Principle II. Documented per the constitution's "deviation MUST be justified in writing" rule.

| Deviation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| `ScrapeProfile` uses `Base + TimestampMixin`, **not** `WorkspaceScopedBase` | `WorkspaceScopedBase` forces `workspace_id NOT NULL`; global defaults require `workspace_id IS NULL` (§9, FR-001) | Keeping `NOT NULL` and modelling "global" as a magic sentinel workspace id was rejected — it would fake a nonexistent workspace row, break the `workspaces` FK, and leak the sentinel into every tenant query |
| `ScrapeProfile` **excluded** from `WORKSPACE_OWNED_MODELS` | Its `scoped_select`/`scoped_get` constrain to `workspace_id = ctx`, which would hide the global (`NULL`) rows that resolution and reads must see (FR-013/FR-014) | Adding it and special-casing every read to also union global rows was rejected as more error-prone than one dedicated dual-scope repository module that encodes "own OR global" once; the repository is the single sanctioned query path (mirrors how `app_shared.repository` is the sanctioned path for strictly-owned models) |
| Custom `emit_global_readable_rls_policy` (SELECT own+global; FOR ALL write own-only) instead of the standard single `emit_rls_policy` | The standard policy `USING (workspace_id = ctx)` makes `NULL`-workspace rows invisible to everyone (`NULL = ctx` is NULL) — the opposite of the required "global readable by all, tenant-unwritable" (FR-013/FR-021) | Reusing `emit_rls_policy` and adding global visibility in the app layer only was rejected — it would leave the DB defense-in-depth layer unable to express "global rows are read-shared but write-protected", violating the two-layer isolation model |
