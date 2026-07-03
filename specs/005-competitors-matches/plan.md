# Implementation Plan: Competitors & Matches

**Branch**: `005-competitors-matches` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/005-competitors-matches/spec.md`

## Summary

Deliver the "manual match" half of the core monitoring loop on top of the SPEC-02 DB foundation, the SPEC-03 isolation/auth machinery, and the SPEC-04 catalog: the two workspace-owned tables **`competitors`** and **`competitor_product_matches`** (exact §22 shapes), their `/v1` CRUD + **set-based bulk-upsert** endpoints, a **save-time SSRF URL-safety validator**, a **versioned URL normalization + pattern-derivation** algorithm, and the second end-to-end scope-gated API family (`competitors:read/write`, `matches:read/write` via the SPEC-03 `require_scopes` seam). Matches link a client `product_variant` (and its `product`) to a competitor URL; every URL is validated safe and normalized into a canonical URL plus a versioned pattern at save time.

Concretely this feature adds:
- `app_shared`:
  - `models/competitors_matches.py` — two ORM models on `WorkspaceScopedBase`, with the §22 columns, the two unique keys, `unique(workspace_id, id)` on `competitors` (so matches can composite-FK it), and three **workspace-local composite FKs** on the match (product / variant / competitor). Long constraint names are given **explicit** short forms (mirroring the `product_group_items` precedent) because the `competitor_product_matches` names blow past Postgres's 63-byte identifier cap.
  - `url_safety.py` — a **pure, framework-agnostic** save-time SSRF validator (scheme allow-list, userinfo rejection, IP-literal deny-range checks, internal-hostname/suffix deny-list). No DNS resolution at save time (deferred to the SPEC-07 spider — see research D2).
  - `url_pattern.py` — a **pure** `URL_PATTERN_ALGORITHM_VERSION` constant + `normalize_url()` (canonical identity URL) + `derive_url_pattern()` (versioned grouping pattern), implementing the §15 steps with plan-level id-like thresholds behind the version bump.
  - `matches/upsert.py` — a **pure** set-based match upsert core (single `INSERT ... ON CONFLICT DO UPDATE` on the 4-column match key, in-batch last-wins dedup, variant→(variant_id, product_id) resolution, and a `prepare_match_urls` batch splitter that applies URL-safety + normalization and separates safe rows from rejected ones — the "reject-and-report" policy of FR-013). Reuses `app_shared.catalog.upsert.dedup_last_wins` and `app_shared.catalog.consistency` unchanged.
  - `enums.py` (extend): `LegalStatus`, `RobotsPolicy`, `MatchPriority`, `CompetitorStatus`, `MatchStatus`, `HealthStatus`.
  - `repository.py` (extend): add `Competitor` and `CompetitorProductMatch` to `WORKSPACE_OWNED_MODELS`; `models/__init__.py` re-exports them.
- `apps/api`:
  - `schemas/competitors.py` / `schemas/matches.py` — Pydantic request/response DTOs (FastAPI-coupled, so out of `app_shared`), including the bulk-upsert payload + result with a `rejected` list.
  - `routers/competitors.py` / `routers/matches.py` — scope-gated `/v1` CRUD + `POST /v1/matches/bulk-upsert`, on the already-context-set request session, using `scoped_select`/`scoped_get`, the consistency pre-check, and the URL-safety/normalization/upsert cores. Registered in `main.py`.
- repo root: one Alembic migration creating both tables (exact §22 shapes, the two unique keys, composite workspace-local FKs) and calling `emit_rls_policy` on **both** in the same migration; `down_revision = c2987b29555e` (current head).

Everything DB-independent is fully unit-tested **here** (model/constraint/index shapes + naming render + <63-byte explicit names, RLS DDL render for both tables, the full SSRF accept/deny corpus, URL normalization + pattern-derivation corpus + version constant, the match upsert statement construction + `on_conflict_do_update` on the 4-col key compiled to SQL without executing, in-batch last-wins dedup, `prepare_match_urls` safe/unsafe split, workspace-consistency checks for match refs, scope-gating wiring, and the CI scoping guard passing with the two new models). Live-Postgres items (actual create/upsert, RLS row denial, cross-workspace blocking, migration online run, end-to-end request flows) are **authored and marked** for a PG-capable host — no Docker daemon / live Postgres in this build env.

## Technical Context

**Language/Version**: Python 3.13 (`requires-python = ">=3.13,<3.14"`; uv workspace).

**Primary Dependencies**:
- Existing only — no new third-party deps. SQLAlchemy 2.0 (sync) incl. the **PostgreSQL dialect** `insert(...).on_conflict_do_update(...)` for the set-based match upsert; psycopg 3; Alembic; FastAPI + Pydantic v2 (`apps/api`); stdlib `urllib.parse` + `ipaddress` + `re` for the URL-safety validator and pattern derivation.
- `app_shared` MUST NOT import FastAPI (framework-agnostic) and MUST NOT import scrapy/twisted/playwright (unchanged import-boundary test). The models, enums, URL-safety validator, URL-pattern derivation, and the match-upsert **core** live in `app_shared`; the FastAPI schemas/routers/deps live in `apps/api`.

**Storage**: PostgreSQL 17. App requests connect through PgBouncer (transaction pooling); the migration connects directly (`MIGRATION_DATABASE_URL`). Workspace context is set per-transaction by the SPEC-03 auth seam (`set_config('app.workspace_id', :wsid, true)`) before any competitor/match query. RLS enabled+forced on both tables in the creating migration. Both unique keys backed by full (non-partial) unique constraints.

**Testing**: pytest. DB-independent logic unit-tested here (compile-to-SQL for the match upsert, no execution; pure functions for URL safety and pattern). Live-DB items authored and skipped when no reachable Postgres is present (same pattern as SPEC-03/04 live markers).

**Target Platform**: Linux server / containers. Only `apps/api` is publicly exposed.

**Project Type**: Backend monorepo (uv workspace). Spans `app_shared` (models, url_safety, url_pattern, matches core) and `apps/api` (schemas, routers), plus repo-root Alembic.

**Performance Goals**: Bulk match upsert executes in a **bounded** number of statements regardless of batch size — one `INSERT ... ON CONFLICT DO UPDATE` for all safe rows (the match has a single unique arbiter, so no identity-kind partitioning is even needed), plus the fixed lookups to resolve variant→(variant_id, product_id) and to consistency-check competitor ids. No per-row DB loop on any ingestion path (Principle VIII). List endpoints reuse the SPEC-04 keyset `(created_at, id)` cursor pagination, default 50 / max 500. URL-safety and pattern derivation are O(1) per URL, pure CPU, no I/O.

**Constraints**: Transaction-pooling-safe only (only `SET LOCAL`/`set_config(...,true)`; no session advisory locks; `prepare_threshold=None`). RLS fails closed (zero rows) when no workspace context is set. Save-time URL safety rejects any non-public/non-http(s)/credentialed URL and never stores it — on create, update, AND bulk-upsert. `url_pattern_version` is stored per row; patterns from different versions are never mixed in lookups (backfill on a version bump is out of scope). `current_price_id` is a **soft** reference (no FK); `scrape_profile_id`/`access_policy_id` are plain nullable references (no FK until SPEC-06/10). `app_shared` stays FastAPI-free and scrapy-free. No live Postgres in this build env.

**Scale/Scope**: Foundation for 10k–20k matches per workspace (§39). This spec adds **exactly two** tables and the competitors/matches `/v1` endpoints + save-time URL safety + versioned pattern + isolation. **No** scrape-profiles / access-policies / observations / prices / alerts / fetch-time URL re-validation / optimizer / url_pattern backfill (SPEC-06+).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Relevance | How this plan satisfies it |
|-----------|-----------|----------------------------|
| **I. API-First / Service boundaries** | New `app_shared` modules + FastAPI in `apps/api` | Competitor/match ORM models, the URL-safety validator, the URL-pattern derivation, and the match-upsert **core** live in `app_shared` and import only sqlalchemy/stdlib — never fastapi, never scrapy/twisted/playwright. The import-boundary test is extended to cover `app_shared.models.competitors_matches`, `app_shared.url_safety`, `app_shared.url_pattern`, and `app_shared.matches.*`. Pydantic schemas + the `/v1` routers + scope-gating live in `apps/api`, importing `app_shared` one-way. Only `apps/api` is publicly exposed. **PASS** |
| **II. Workspace Isolation (NON-NEGOTIABLE)** | Core of this spec | Both tables use `WorkspaceScopedBase` (`workspace_id NOT NULL`); `emit_rls_policy()` (ENABLE + FORCE + fail-closed `NULLIF(current_setting('app.workspace_id', true),'')::uuid`) is called on **both** in the **same** creating migration. Both are added to `WORKSPACE_OWNED_MODELS`, so the scoped repository helpers cover them and the AST CI guard (`scripts/check_workspace_scoping.py`, which imports that set) fails the build on any introduced unscoped fetch/select — verified still green with the two new models. Every endpoint runs on the SPEC-03 auth-seam session that has already called `set_workspace_context`; reads/writes go through `scoped_select`/`scoped_get`. All match FKs are **workspace-local** (composite `(workspace_id, <ref>_id)` → parent `(workspace_id, id)`; `competitors` gains `unique(workspace_id, id)`; catalog parents already have it), so a cross-workspace reference is structurally impossible, not just app-checked; a `workspace_consistency` pre-check turns a bad ref into a clean `422` before the DB would raise. Cross-workspace + no-context (fail-closed) tests authored (live-DB). **PASS** |
| **III. Variant-Level Pricing & Explicit Matching** | Directly exercised | A `CompetitorProductMatch` is exactly one competitor URL linked to exactly one `product_variant` (and its `product`); a variant may hold **unlimited** matches, constrained only by `unique(workspace_id, product_variant_id, competitor_id, normalized_competitor_url)`. Matching is manual/explicit (no automatic matching — §38). No pricing computed here; the health/price fields are defaults populated by SPEC-07+. **PASS** |
| **IV. Database-driven config** | Light | `default_scrape_profile_id`/`default_access_policy_id` on the competitor and `scrape_profile_id`/`access_policy_id` on the match are DB-configurable nullable references (targets in SPEC-06/10); no hardcoded scrape/threshold behavior is added. Pagination limits are §24 constants. Profile/policy *resolution chains* are out of scope (SPEC-06/09). **PASS (N/A-ish)** |
| **V. Disciplined Scraping Runtime (NON-NEGOTIABLE)** | Import boundary only | No scraping code; `app_shared` stays scrapy/twisted/playwright-free (see I). Fetch-time DNS re-resolution / redirect re-validation are explicitly deferred to the SPEC-07 spider. **PASS (N/A)** |
| **VI. Internal-only / legal (NON-NEGOTIABLE)** | Save-time URL safety + legal defaults | The save-time SSRF validator (§11 "at save time") is implemented in full and applied on create/update/bulk — no private/internal/loopback/link-local/unique-local/metadata target, no credentialed URL, no non-http(s) scheme is ever stored. Competitors default to `legal_status = REVIEW_REQUIRED` (Principle VI) and `robots_policy = RESPECT`. No login/CAPTCHA/paywall/external-unlocker surface exists in this spec. **PASS** |
| **VII. Monetary & Extraction Correctness (NON-NEGOTIABLE)** | No money column here | Matches carry **no** money column — `current_price_id` is a soft UUID reference to the (later) `match_current_prices` table; `success_rate_7d` is a bounded statistic (`NUMERIC(5,4)`), not a price. No `Decimal`/currency or confidence logic is introduced by this spec. **PASS (N/A)** |
| **VIII. Scale-Safe Data & Concurrency (NON-NEGOTIABLE)** | Ingestion & lists | Bulk upsert is **set-based** — a single `INSERT ... ON CONFLICT DO UPDATE` for all safe rows (no per-row loop, SC-006), constructed by a pure builder and compiled-to-SQL in tests; variant resolution and competitor consistency are one scoped `IN (...)` lookup each, never per-row. Keyset `(created_at, id)` cursor pagination — no OFFSET scans; capped at 500. UUIDv7 PKs keep inserts index-friendly at 10k–20k matches; `TIMESTAMPTZ` everywhere. All app traffic through PgBouncer; only `SET LOCAL`/`set_config(...,true)`. Single linear migration history (existing CI head guard). Partitioning/retention (§29) is N/A — competitors/matches are mutable-state, not append-heavy (the append tables `price_observations`/`request_attempts` are SPEC-07+). **PASS** |

**Technology & Security Constraints (§24/§33/§34)**: Stack lock-in honored (SQLAlchemy+Alembic, PostgreSQL pg-dialect `insert`, psycopg, FastAPI/Pydantic). Public API versioned under `/v1`; list endpoints cursor-paginated default 50 / max 500 (§24). UUIDv7 public ids (§21). Deletion follows §24 mutating rules: hard-delete only while no dependent history exists (true now — observations/attempts land in SPEC-07), structured for archive-by-status, response indicates which outcome (FR-016). New scope vocabulary used exactly as already defined in `app_shared.security.scopes` (`competitors:read/write`, `matches:read/write` — no new scopes minted). Structured error-code vocabulary reused/extended for save-time rejections: `UNSAFE_URL` (§34 family), `NOT_FOUND`, `FORBIDDEN`, validation `422`, `INVALID_CURSOR`, `WORKSPACE_MISMATCH` (cross-workspace/nonexistent ref).

**Gate result**: PASS — no violations. Complexity Tracking table intentionally empty. Re-checked post-Phase-1 (see end of plan): still PASS — no new tables, references, or scopes beyond §22/§24; the URL-safety validator is the mandated §11 save-time control, not new scope.

## Project Structure

### Documentation (this feature)

```text
specs/005-competitors-matches/
├── plan.md              # This file (/speckit-plan output)
├── research.md          # Phase 0 — enums + defaults, competitor unique(ws,id) + match composite FKs,
│                        #   SSRF save-time vs fetch-time split + deny-list, URL normalize vs pattern +
│                        #   id-like thresholds + version, single-arbiter match upsert + reject-and-report,
│                        #   variant/competitor ref resolution reuse, migration head, unit-vs-live split
├── data-model.md        # Phase 1 — 2 tables (exact §22 shapes), enums, the two unique keys, composite FKs,
│                        #   explicit <63-byte constraint names, RLS/isolation, health-defaults, URL identity
├── quickstart.md        # Phase 1 — how to validate (unit here; live create/upsert/RLS/migration on a PG host)
├── contracts/           # Phase 1 — interfaces this feature exposes
│   ├── api-competitors.md            # /v1/competitors CRUD
│   ├── api-matches.md                # /v1/matches CRUD + bulk-upsert
│   ├── models-competitors-matches.md # ORM model shapes, unique keys, composite FKs, explicit names, enums
│   ├── url-safety.md                 # save-time SSRF validator: allow/deny rules, deny-list, exception shape
│   ├── url-pattern.md                # normalize_url + derive_url_pattern + URL_PATTERN_ALGORITHM_VERSION
│   ├── matches-bulk-upsert.md        # set-based single-arbiter upsert + reject-and-report + ref resolution
│   ├── workspace-consistency.md      # reuse of the SPEC-04 consistency helper for match refs
│   └── migration-competitors-matches.md # the migration + RLS on both + single head
└── tasks.md             # Phase 2 (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
libs/shared/app_shared/
├── enums.py             # EXTEND: LegalStatus (REVIEW_REQUIRED/APPROVED/DISABLED),
│                        #   RobotsPolicy (RESPECT/REVIEW_REQUIRED/IGNORE_AFTER_APPROVAL),
│                        #   MatchPriority (LOW/NORMAL/HIGH/CRITICAL),
│                        #   CompetitorStatus (ACTIVE/ARCHIVED), MatchStatus (ACTIVE/PAUSED/FAILED/ARCHIVED),
│                        #   HealthStatus (HEALTHY/DEGRADED/FAILING/UNKNOWN). All StrEnum → VARCHAR(32).
├── url_safety.py        # NEW: pure save-time SSRF validator. validate_competitor_url(url) raising
│                        #   UnsafeUrlError(reason); scheme allow-list {http,https}; reject userinfo;
│                        #   IP-literal deny (ipaddress: not is_global / private / loopback / link-local /
│                        #   unique-local / reserved / metadata 169.254.169.254); internal hostname + suffix
│                        #   deny-list constants. NO DNS resolution (fetch-time is SPEC-07). Framework-agnostic.
├── url_pattern.py       # NEW: URL_PATTERN_ALGORITHM_VERSION=1; normalize_url(url) -> canonical identity URL
│                        #   (lowercase host, strip www./fragment/default-port/trailing-slash; KEEP query);
│                        #   derive_url_pattern(url) -> grouping pattern (drop scheme+query, :id id-like
│                        #   segments, * product-slug segments after known keys, preserve locale prefixes);
│                        #   derive_match_url_fields(url) -> (normalized_url, url_pattern, version). Pure.
├── repository.py        # EXTEND: add Competitor, CompetitorProductMatch to WORKSPACE_OWNED_MODELS
│                        #   (ModelT bound already `Base` from SPEC-04 — no change). Helper behavior unchanged.
├── models/
│   ├── __init__.py      # EXTEND: re-export Competitor, CompetitorProductMatch
│   └── competitors_matches.py # NEW: 2 ORM models (WorkspaceScopedBase + TimestampMixin), §22 columns,
│                        #   unique(ws,domain) + unique(ws,id) on competitors, the 4-col match unique,
│                        #   three composite workspace-local FKs on the match, EXPLICIT short constraint
│                        #   names (<63 bytes) for competitor_product_matches (product_group_items precedent),
│                        #   health defaults (health UNKNOWN, consecutive_failures=0, null rate/price/last-*).
└── matches/
    ├── __init__.py      # NEW
    └── upsert.py        # NEW: build_matches_upsert(rows) -> single pg Insert.on_conflict_do_update on the
                         #   4-col key; match_conflict_key(row); variant_lookup_keys(rows) +
                         #   resolve_match_variants(rows, by_external_id, by_sku, by_id) -> (resolved,
                         #   unresolved) (mirrors catalog variant resolution); prepare_match_urls(rows) ->
                         #   (safe_prepared, rejected) applying url_safety + url_pattern per record
                         #   (reject-and-report, FR-013). Reuses catalog.upsert.dedup_last_wins.

apps/api/app/
├── main.py              # EXTEND: include the competitors / matches routers
├── schemas/
│   ├── competitors.py   # NEW: CompetitorCreate/Update/Response/ListResponse (Pydantic v2)
│   └── matches.py       # NEW: MatchCreate/Update/Response/ListResponse; MatchBulkUpsertRequest;
│                        #   MatchBulkUpsertResult {upserted, matches, rejected:[{index,code,reason,url}]}.
│                        #   Reuses app.schemas.catalog.DeleteOutcome.
└── routers/
    ├── competitors.py   # NEW: POST/GET/GET{id}/PATCH/DELETE /v1/competitors
    │                    #   (require_scopes competitors:read|write); domain unique per workspace; delete
    │                    #   hard-deletes now, structured for archive-by-status, outcome reported.
    └── matches.py       # NEW: POST/GET/GET{id}/PATCH/DELETE /v1/matches + POST /v1/matches/bulk-upsert
                         #   (require_scopes matches:read|write); per-record URL safety + normalize + pattern;
                         #   variant/product/competitor refs consistency-checked in-workspace; single-arbiter
                         #   set-based upsert with reject-and-report.

alembic/versions/
└── <rev>_competitors_matches_tables.py  # NEW: create competitors + competitor_product_matches (exact §22
                         #   shapes, unique(ws,domain)+unique(ws,id), 4-col match unique, composite
                         #   workspace-local FKs, explicit <63-byte names); emit_rls_policy on both;
                         #   downgrade (matches then competitors); down_revision = c2987b29555e (current head)

tests/unit/
├── test_import_boundaries.py            # EXTEND: cover app_shared.models.competitors_matches,
│                                        #   app_shared.url_safety, app_shared.url_pattern, app_shared.matches.*
├── test_competitors_matches_models.py   # NEW: table/column shapes, the two unique keys, composite FKs,
│                                        #   explicit constraint names all <63 bytes, health defaults, enums
├── test_rls_competitors_matches.py      # NEW: emit_rls_policy render for both tables (fail-closed DDL)
├── test_url_safety.py                   # NEW: the accept/deny corpus — public http(s) accepted; localhost /
│                                        #   private / loopback / link-local / unique-local / metadata IP /
│                                        #   internal hostname+suffix / userinfo / non-http scheme rejected (v4+v6)
├── test_url_pattern.py                  # NEW: normalization + pattern corpus; id-like segments -> :id;
│                                        #   product-slug -> *; locale preserved; query kept in normalized,
│                                        #   dropped in pattern; version constant stamped
├── test_matches_upsert.py               # NEW: build_matches_upsert compiles to ON CONFLICT (4 cols) DO UPDATE;
│                                        #   last-wins dedup; ONE statement (no per-row loop); variant
│                                        #   resolution; prepare_match_urls safe/unsafe split
├── test_matches_scope_gating.py         # NEW: routers declare correct require_scopes (read vs write)
├── test_competitors_matches_scoping_guard.py # NEW: CI guard flags a planted unscoped select(Competitor)/
│                                        #   select(CompetitorProductMatch); clean tree passes
├── test_workspace_consistency.py        # EXTEND (or reuse): match refs accepted in-workspace, rejected cross/absent
└── test_migration_offline_competitors_matches.py # NEW: `alembic upgrade head --sql` renders both tables +
                                         #   unique keys + RLS (both tables); single head

tests/integration/  (authored, live-DB-marked — skipped without Postgres)
├── test_competitors_crud_live.py            # create; unique domain per workspace; read/update/list/delete outcome
├── test_matches_crud_live.py                # create w/ safe URL -> normalized+pattern+version; unsafe URL rejected;
│                                            #   unlimited matches per variant; exact-tuple duplicate rejected
├── test_matches_bulk_upsert_live.py         # set-based; re-push -> 0 dupes, in-place update; unsafe reject-report;
│                                            #   bounded statement count (assert via query log)
└── test_competitors_matches_isolation_live.py # cross-workspace blocked (app + RLS); no-context -> 0 rows
                                             #   (fail-closed); cross-workspace ref rejected; scope refusal
```

**Structure Decision**: Backend monorepo (uv workspace), matching SPEC-03/04. `app_shared` gains the two ORM models, a framework-agnostic URL-safety validator, a framework-agnostic versioned URL-pattern deriver, and a framework-agnostic `matches/` upsert core, so all DB-independent behavior (the security-critical SSRF validator and the URL-pattern algorithm especially) is unit-testable without FastAPI or a live DB. FastAPI schemas, routers, and scope-gating live in `apps/api` (keeping `app_shared` FastAPI-free). The two tables + RLS land in one repo-root Alembic migration chained onto the current head `c2987b29555e`.

## Complexity Tracking

> No Constitution Check violations — table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |
