---
description: "Dependency-ordered task list for SPEC-05 Competitors & Matches"
---

# Tasks: Competitors & Matches

**Input**: Design documents from `/specs/005-competitors-matches/`

**Prerequisites**: plan.md, spec.md, research.md (D1–D9), data-model.md, contracts/ (8 files), quickstart.md, checklists/ (requirements.md + security.md)

**Tests**: Unit tests are DB-independent and run **here**; live-Postgres acceptance tests are **authored + DEFERRED** (no Docker daemon / live Postgres in this build env — see research D9). Deferred tasks stay unchecked `- [ ]` and are marked ⏸ DEFERRED (needs live Postgres).

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: `[US1]`/`[US2]`/`[US3]`/`[US4]` maps a task to a spec.md user story (Setup/Foundational/Polish carry no story label)
- Every task lists an exact repo-relative file path

---

## Scope Boundary (read first)

**IN SCOPE — exactly two tables and their surface:**

- Tables: `competitors`, `competitor_product_matches` (workspace-owned, RLS on **both** in the creating migration).
- Endpoints under `/v1`: competitors (create/list/get/update/delete); matches (create/list/get/update/delete/bulk-upsert).
- Behaviors: save-time SSRF URL-safety validation (create/update/bulk), versioned URL normalization + pattern derivation, set-based single-arbiter bulk match upsert with reject-and-report, cursor pagination, workspace isolation + reference integrity + scope-gating (`competitors:read/write`, `matches:read/write`), hard-delete-vs-archive outcome.

**OUT OF SCOPE (do NOT create anything for these — SPEC-06+):** scrape-profiles, access-policies, observations, prices/price-history, alerts, the domain strategy optimizer, fetch-time DNS re-resolution / redirect re-validation, url_pattern version-bump backfill, automatic matching. `current_price_id` is a **soft** reference (no FK); `scrape_profile_id`/`access_policy_id` are plain nullable references (no FK until SPEC-06/10). **No new API-key scopes** — `competitors:*`/`matches:*` already exist in `app_shared.security.scopes.Scope`. Reuse SPEC-04 helpers unchanged: `pagination.py` keyset helpers, `catalog.upsert.dedup_last_wins`, `catalog.consistency.assert_refs_in_workspace`, `app.schemas.catalog.DeleteOutcome`, `deps.require_scopes`, `scoped_select`/`scoped_get`, `WorkspaceScopedBase`/`emit_rls_policy`/`enum_column`, the AST scoping guard.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Enums and empty package scaffolding that later files import.

- [X] T001 [P] Extend `libs/shared/app_shared/enums.py` with six `StrEnum`s (research D1), each string-backed via `enum_column` → `VARCHAR(32)`, exact uppercase §22 tokens: `LegalStatus` (`REVIEW_REQUIRED`/`APPROVED`/`DISABLED`), `RobotsPolicy` (`RESPECT`/`REVIEW_REQUIRED`/`IGNORE_AFTER_APPROVAL`), `CompetitorStatus` (`ACTIVE`/`ARCHIVED`), `MatchPriority` (`LOW`/`NORMAL`/`HIGH`/`CRITICAL`), `MatchStatus` (`ACTIVE`/`PAUSED`/`FAILED`/`ARCHIVED`), `HealthStatus` (`HEALTHY`/`DEGRADED`/`FAILING`/`UNKNOWN`). (FR-003, FR-004, FR-017)
- [X] T002 [P] Create `libs/shared/app_shared/matches/__init__.py` (empty package init for the framework-agnostic match-upsert core).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The two ORM models, their registration, and the single migration that creates both tables + RLS. **No user story can be implemented until this phase is complete.**

**⚠️ CRITICAL**: Blocks all of Phase 3–6.

- [X] T003 Create both ORM models in `libs/shared/app_shared/models/competitors_matches.py` on `WorkspaceScopedBase` + `TimestampMixin` (per data-model.md): `Competitor` (`name`, `domain`, `status`/`legal_status`/`robots_policy` via `enum_column`, nullable `default_scrape_profile_id`/`default_access_policy_id`/`max_concurrent_requests`/`max_requests_per_minute`; `unique(workspace_id, domain)`, `unique(workspace_id, id)`, `workspace_id → workspaces.id` FK) and `CompetitorProductMatch` (all §22 columns incl. `competitor_url`/`normalized_competitor_url`/`url_pattern`/`url_pattern_version`, `competitor_variant_*`/`external_title`, nullable `scrape_profile_id`/`access_policy_id`, `priority`/`status`/`health_status` enums, health fields with defaults `health_status=UNKNOWN`/`consecutive_failures=0`/nullable `success_rate_7d` `NUMERIC(5,4)`/`current_price_id`/`last_error_code`/`last_*_at`, `competitor_variant_options` JSONB). Match constraints use **explicit <63-byte names** (research D5): `unique(workspace_id, product_variant_id, competitor_id, normalized_competitor_url)` = `uq_cpm_ws_variant_competitor_norm_url`; composite workspace-local FKs `fk_cpm_workspace_product_products` `(workspace_id, product_id)→products(workspace_id, id)`, `fk_cpm_workspace_variant_variants` `(workspace_id, product_variant_id)→product_variants(workspace_id, id)`, `fk_cpm_workspace_competitor_competitors` `(workspace_id, competitor_id)→competitors(workspace_id, id)`, `fk_cpm_workspace_workspaces` `(workspace_id)→workspaces(id)`. `current_price_id`/`scrape_profile_id`/`access_policy_id` carry **no** FK. (FR-001, FR-003, FR-004, FR-005, FR-006, FR-017)
- [X] T004 Re-export `Competitor`, `CompetitorProductMatch` from `libs/shared/app_shared/models/__init__.py` so `Base.metadata` sees them for Alembic offline render (depends on T003).
- [X] T005 In `libs/shared/app_shared/repository.py` add `Competitor` and `CompetitorProductMatch` to `WORKSPACE_OWNED_MODELS` (`ModelT` already `bound=Base` from SPEC-04 — no change); helper behavior unchanged (depends on T003). (FR-001, FR-002)
- [X] T006 Author the Alembic migration `alembic/versions/<rev>_competitors_matches_tables.py` with `down_revision = "c2987b29555e"` (current head): `create_table` both (exact §22 shapes — `String(32)` enums, `Numeric(5,4)` `success_rate_7d`, `JSONB` `competitor_variant_options`, `Integer` `url_pattern_version`), the `unique(workspace_id, domain)`+`unique(workspace_id, id)` on competitors, the 4-col match unique + three composite workspace-local FKs (explicit <63-byte names per D5), then `emit_rls_policy(...)` on **both** tables in the same migration; FK-safe `downgrade()` drops `competitor_product_matches` then `competitors`; single head (depends on T003, T004). (FR-001, FR-002, FR-005, FR-006)
- [X] T007 [P] Unit test `tests/unit/test_competitors_matches_models.py`: table/column shapes for both, `unique(workspace_id, domain)` + `unique(workspace_id, id)` on competitors, the 4-col match unique, the three composite-FK shapes + explicit names, **assert every emitted constraint name ≤63 bytes** (D5), enum columns render `VARCHAR(32)`, health-field defaults (`health_status=UNKNOWN`, `consecutive_failures=0`, nullable rate/price/timestamps), `current_price_id`/`scrape_profile_id`/`access_policy_id` have no FK. (FR-004, FR-005, FR-006, FR-017, SC-002)
- [X] T008 [P] Unit test `tests/unit/test_rls_competitors_matches.py`: `emit_rls_policy` renders ENABLE+FORCE + fail-closed `USING (workspace_id = NULLIF(current_setting('app.workspace_id', true),'')::uuid)` DDL for **both** `competitors` and `competitor_product_matches`. (FR-001, FR-002, SC-007)
- [X] T009 [P] Unit test `tests/unit/test_migration_offline_competitors_matches.py`: `alembic upgrade head --sql` renders both tables + both unique keys + the 4-col match unique + composite FKs + RLS statements for both; assert single head (`down_revision = c2987b29555e`). (FR-001, FR-002)

**Checkpoint**: Both models + migration + RLS in place; DB-independent shape/name/RLS/offline-migration tests green. User stories can begin.

---

## Phase 3: User Story 1 - Register competitors (Priority: P1) 🎯 MVP

**Goal**: Competitor CRUD — create with name+domain and legal/robots/status/priority defaults, `domain` unique per workspace, read/update/list (cursor-paginated), delete reporting hard-delete-vs-archive outcome.

**Independent Test**: Create a competitor with a domain → stored workspace-scoped with `legal_status=REVIEW_REQUIRED`/`robots_policy=RESPECT`/`status=ACTIVE`; re-registering the same domain in the same workspace → rejected (`409`); read/update/list/delete work and delete returns `{id, outcome}`.

### Schemas + endpoints

- [X] T010 [US1] Create `apps/api/app/schemas/competitors.py` (Pydantic v2, FastAPI-coupled): `CompetitorCreate` (name+domain required; optional status/legal_status/robots_policy/default_scrape_profile_id/default_access_policy_id/max_concurrent_requests/max_requests_per_minute), `CompetitorUpdate` (PATCH — all optional), `CompetitorResponse`, `{items, next_cursor}` list envelope; reuse `app.schemas.catalog.DeleteOutcome`. Enum fields validate against the T001 enums; defaults applied server-side when omitted. (FR-003, FR-012, FR-016, FR-017)
- [X] T011 [US1] Create `apps/api/app/routers/competitors.py` (per contracts/api-competitors.md): `POST /v1/competitors` (require `competitors:write`; apply defaults; `unique(workspace_id, domain)` dup → `409`), `GET /v1/competitors` (require `competitors:read`; keyset pagination via `pagination.py`, default 50 / max 500), `GET /v1/competitors/{id}` (`scoped_get` → `404` cross-ws), `PATCH /v1/competitors/{id}` (require `competitors:write`), `DELETE /v1/competitors/{id}` (require `competitors:write`; hard-delete now — no dependent history until SPEC-07 — structured for archive-by-status, returns `{id, outcome}`). Uses `scoped_select`/`scoped_get` on the request-context session. (FR-003, FR-012, FR-014, FR-015, FR-016)
- [X] T012 [US1] Register the competitors router in `apps/api/app/main.py` under `/v1` (depends on T011). (FR-012)
- [ ] T013 [P] [US1] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_competitors_crud_live.py`: create competitor → stored with defaults, workspace-scoped; same-domain re-create → `409`; read/update/list persistence; delete returns `hard_deleted` outcome. (SC-001)

**Checkpoint**: Competitor CRUD + domain uniqueness functional; MVP demoable (unit/schema-verified here; live create deferred).

---

## Phase 4: User Story 2 - Link a variant to a competitor URL, safely (Priority: P1)

**Goal**: Create a match linking a variant (and its parent product) to a competitor URL that is safety-validated at save time and normalized into a canonical URL + versioned pattern; unsafe/credentialed/non-http(s) URLs rejected and never stored; a variant may hold unlimited matches bounded only by the 4-col unique.

**Independent Test**: Create a match with a valid public http(s) URL → stored with `normalized_competitor_url` + `url_pattern` + `url_pattern_version`; submit a localhost / private / loopback / link-local / unique-local / metadata-IP / internal-hostname / userinfo / non-http URL → rejected at save time (`422 UNSAFE_URL`), not stored; multiple matches for one variant across competitors/URLs all stored, exact-tuple duplicate rejected.

### Core logic (DB-independent, security-critical) + tests

- [X] T014 [P] [US2] Create `libs/shared/app_shared/url_safety.py` (pure, framework-agnostic — research D2): `validate_competitor_url(url) -> None` raising typed `UnsafeUrlError(reason: UnsafeUrlReason)`; steps in order — `urlsplit` parse (reject unparseable/missing host); scheme allow-list `{http, https}`; reject userinfo (`username`/`password` present); host classification — IP literal (incl. bracketed IPv6 and IPv4-mapped) rejected unless `is_global` (reject `is_loopback`/`is_private`(10/8·172.16/12·192.168/16·`fc00::/7`)/`is_link_local`(169.254/16·`fe80::/10`)/`is_reserved`/`is_multicast`/`is_unspecified` and the `169.254.169.254` metadata literal), else DNS name (lowercased) rejected if in `INTERNAL_HOSTNAMES` or ending with an `INTERNAL_HOST_SUFFIXES` entry. **No DNS resolution** (fetch-time is SPEC-07). Deny-list constants per D2. (FR-007, FR-008, FR-009)
- [X] T015 [P] [US2] Unit test `tests/unit/test_url_safety.py`: the full accept/deny corpus — public http(s) accepted; rejected: `localhost`, private (10/172.16/192.168), loopback (`127.0.0.1`/`::1`), link-local (`169.254.x`/`fe80::`), unique-local (`fc00::`), metadata `169.254.169.254`, IPv4-mapped `::ffff:169.254.169.254`, internal hostnames + suffixes (`*.internal`/`*.local`/`*.railway.internal`/compose service names/`metadata.google.internal`), userinfo `user:pass@host`, non-http(s) schemes (`ftp`/`file`/`gopher`); asserts each raises `UnsafeUrlError` with the right reason (IPv4 + IPv6 forms). (FR-007, FR-008, SC-004)
- [X] T016 [P] [US2] Create `libs/shared/app_shared/url_pattern.py` (pure — research D3): `URL_PATTERN_ALGORITHM_VERSION: int = 1`; `normalize_url(url) -> str` (canonical **identity**: lowercase scheme+host, strip `www.`/default-port/fragment/trailing-slash, **keep query**); `derive_url_pattern(url) -> str` (versioned **grouping**: drop scheme+query, split path, preserve leading locale prefix `^[a-z]{2}(-[a-z]{2})?$`, replace id-like segments with `:id` [all-digits / UUID-like / len≥8 mixed-alnum / len≥4 & digit-ratio≥0.5], replace slug after known product keys `products`/`product`/`p`/`item` with `*`); `derive_match_url_fields(url) -> (normalized_url, url_pattern, URL_PATTERN_ALGORITHM_VERSION)`. (FR-010, FR-011)
- [X] T017 [P] [US2] Unit test `tests/unit/test_url_pattern.py`: normalization corpus (host lowercased, scheme/`www.`/trailing-slash/fragment stripped, query kept in normalized); pattern corpus (scheme+query dropped, id-like → `:id`, UUID → `:id`, product slug → `*`, locale prefix `/ar/`·`/en/` preserved, ordinary short slug `iphone-15` NOT mistaken for id); `derive_match_url_fields` stamps `url_pattern_version == 1`. (FR-010, FR-011, SC-005)

### Schemas + endpoints

- [X] T018 [US2] Create `apps/api/app/schemas/matches.py` (Pydantic v2): `MatchCreate` (`product_variant_id` + `competitor_id` + `competitor_url` required; optional `competitor_variant_identifier`/`competitor_variant_sku`/`competitor_variant_options`/`external_title`/`scrape_profile_id`/`access_policy_id`/`priority`/`status`; **health fields NOT client-settable**, `product_id` derived server-side from the variant), `MatchUpdate` (PATCH), `MatchResponse` (incl. normalized_url/url_pattern/url_pattern_version/health fields), `{items, next_cursor}` list envelope; reuse `DeleteOutcome`. (FR-004, FR-012, FR-016, FR-017)
- [X] T019 [US2] Create `apps/api/app/routers/matches.py` single-record CRUD (per contracts/api-matches.md): `POST /v1/matches` (require `matches:write`; `validate_competitor_url` → `422 UNSAFE_URL` on reject; `derive_match_url_fields` stamps normalized/pattern/version; resolve `product_variant_id` in-workspace and **derive `product_id` from the variant's parent** [research D4]; consistency pre-check competitor+variant refs via `catalog.consistency.assert_refs_in_workspace` → `422 WORKSPACE_MISMATCH`/`404 NOT_FOUND`; health fields defaulted; 4-col unique dup → `409`), `GET /v1/matches` (require `matches:read`; optional workspace-scoped `product_variant_id`/`competitor_id` filter; keyset paginated), `GET /v1/matches/{id}` (`404` cross-ws), `PATCH /v1/matches/{id}` (require `matches:write`; if `competitor_url` changes → re-validate + re-derive; health fields untouched), `DELETE /v1/matches/{id}` (require `matches:write`; hard-delete now, structured for archive, returns `{id, outcome}`). Uses `scoped_select`/`scoped_get`. (FR-004, FR-005, FR-006, FR-007, FR-008, FR-009, FR-010, FR-012, FR-014, FR-015, FR-016, FR-017)
- [X] T020 [US2] Register the matches router in `apps/api/app/main.py` under `/v1` (depends on T019). (FR-012)
- [ ] T021 [P] [US2] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_matches_crud_live.py`: create with safe URL → normalized+pattern+version persisted, `product_id` = variant's parent; unsafe URL (localhost/private/metadata/userinfo/non-http) → `422 UNSAFE_URL`, not stored; unlimited matches per variant; exact-tuple duplicate → `409`; two raw URLs normalizing equal collide as same match. (SC-002, SC-003, SC-004)

**Checkpoint**: Save-time SSRF validation + URL normalization/pattern + single-match create verified here (pure-logic corpus exhaustive); live persistence deferred.

---

## Phase 5: User Story 3 - Bulk-upsert matches (Priority: P1)

**Goal**: Set-based, idempotent bulk match upsert — one `INSERT ... ON CONFLICT DO UPDATE` on the 4-col arbiter for all safe rows (bounded regardless of batch size), each URL safety-validated + normalized, unsafe rows rejected-and-reported (not aborting the safe set), in-batch last-wins, health fields never clobbered on re-push.

**Independent Test**: Bulk-upsert a batch → valid rows created with normalized URL+pattern+version; re-push with changes → matched rows (by variant+competitor+normalized URL) update in place, 0 duplicates; unsafe URL reported in `rejected[]` while safe rows still upsert; runs as a bounded statement count (compiled-SQL asserts one ON CONFLICT statement, no per-row loop).

### Core logic (DB-independent) + tests

- [X] T022 [P] [US3] Create `libs/shared/app_shared/matches/upsert.py` (pure, compiles statements — no execution, research D6): `match_conflict_key(row) = (product_variant_id, competitor_id, normalized_competitor_url)`; `prepare_match_urls(rows) -> (safe, rejected)` running `validate_competitor_url` + `derive_match_url_fields` per row, appending `{index, code:"UNSAFE_URL", reason, url}` to `rejected` on `UnsafeUrlError` and stamping normalized/pattern/version on safe rows (FR-013 reject-and-report); `variant_lookup_keys(rows)` + `resolve_match_variants(rows, *, by_external_id, by_sku, by_id) -> (resolved, unresolved)` mirroring the SPEC-04 variant-parent helpers (fills `product_variant_id` **and** `product_id` from the variant's parent); `build_matches_upsert(rows) -> Insert` = **one** `pg_insert(CompetitorProductMatch).values([...]).on_conflict_do_update(index_elements=["workspace_id","product_variant_id","competitor_id","normalized_competitor_url"], set_={updatable cols + updated_at=func.now()})` — updates `competitor_url`/`url_pattern`/`url_pattern_version`/`competitor_variant_*`/`external_title`/`scrape_profile_id`/`access_policy_id`/`priority`/`status`; **never** updates the 4 conflict cols, `product_id`/`workspace_id`/`id`/`created_at`, or the health fields. Reuse `catalog.upsert.dedup_last_wins` keyed by `match_conflict_key`. (FR-006, FR-009, FR-010, FR-013, FR-017)
- [X] T023 [P] [US3] Unit test `tests/unit/test_matches_upsert.py`: compile `build_matches_upsert` to `postgresql` dialect and assert `ON CONFLICT (workspace_id, product_variant_id, competitor_id, normalized_competitor_url) DO UPDATE SET ...` with health fields + `product_id` **absent** from the SET; **exactly one** statement (no per-row loop, SC-006); `dedup_last_wins` keeps last on the match key; `prepare_match_urls` splits safe/unsafe with the reject report shape; `resolve_match_variants` fills variant_id+product_id and returns unresolved. (FR-013, FR-017, SC-003, SC-006)
- [X] T024 [US3] Extend `apps/api/app/schemas/matches.py` with bulk DTOs: `MatchBulkUpsertRequest` (list of match records, URL required, health fields excluded), `MatchBulkUpsertResult` `{upserted: int, matches: [...], rejected: [{index, code, reason, url}]}`. (FR-013, SC-004)
- [X] T025 [US3] Add `POST /v1/matches/bulk-upsert` (require `matches:write`) to `apps/api/app/routers/matches.py`: `dedup_last_wins` → `prepare_match_urls` (collect `rejected`) → `resolve_match_variants` via one scoped `IN (...)` variant lookup (unresolved → consistency `422`) → competitor consistency pre-check (one scoped `IN (...)`) → `build_matches_upsert` executed once under the request workspace context; return `MatchBulkUpsertResult` with safe rows upserted and unsafe/unresolved reported. (FR-006, FR-009, FR-010, FR-012, FR-013, FR-014)
- [ ] T026 [P] [US3] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_matches_bulk_upsert_live.py`: set-based insert; re-push unchanged → 0 duplicates; re-push changed → in-place update (matched on 4-col key) with health fields preserved; mixed batch → unsafe reported in `rejected[]`, safe rows stored; bounded statement count asserted via query log. (SC-004, SC-006)

**Checkpoint**: Idempotent single-arbiter set-based upsert + reject-and-report verified via compiled SQL here; live idempotency deferred.

---

## Phase 6: User Story 4 - Competitor/match access is workspace-isolated and scope-gated (Priority: P1)

**Goal**: Prove end-to-end isolation + reference integrity + scope enforcement — no cross-workspace read/write (app filter + RLS fail-closed), match refs must resolve in-workspace, read-scoped credential cannot write, and the CI guard flags any unscoped competitor/match query.

**Independent Test**: Two populated workspaces → workspace-A caller reads/writes 0 of workspace-B's competitors/matches (incl. app-filter-omitted → RLS blocks, no-context → 0 rows fail-closed); a match referencing another workspace's variant/product/competitor → rejected; read-only credential write → refused, write credential → succeeds; scoping guard flags an introduced unscoped `select(Competitor)`/`select(CompetitorProductMatch)`.

- [X] T027 [P] [US4] Unit test `tests/unit/test_competitors_matches_scoping_guard.py`: `scripts/check_workspace_scoping.py` exits 0 on the current tree (both new models registered in `WORKSPACE_OWNED_MODELS`) AND flags a planted unscoped `select(Competitor)` / `select(CompetitorProductMatch)` (and a `scoped_get` omission). (FR-001, FR-002, SC-008)
- [X] T028 [P] [US4] Unit test `tests/unit/test_matches_scope_gating.py`: every competitors and matches route declares the correct `require_scopes` (read vs write per family) — assert via app route/dependency inspection AND a `TestClient` call with a fake principal lacking the scope → `403` (incl. `POST /v1/matches/bulk-upsert` requiring `matches:write`). (FR-015, SC-008)
- [X] T029 [P] [US4] Extend `tests/unit/test_workspace_consistency.py` (reuse `catalog.consistency.assert_refs_in_workspace`, research D7): match competitor/variant/product refs accepted when in-workspace, rejected (`WORKSPACE_MISMATCH`/`NOT_FOUND`) for a cross-workspace ref and a nonexistent ref; `current_price_id` not consistency-checked (soft ref). (FR-006, SC-007)
- [ ] T030 [P] [US4] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_competitors_matches_isolation_live.py`: workspace-A caller gets 0 rows of workspace-B competitors/matches by id and in lists; app-filter-omitted query → 0 other-workspace rows (RLS); no-context → 0 rows (fail closed); composite-FK cross-workspace ref → rejected; read-only credential write → `403`, write credential → `200`. (FR-002, FR-006, SC-004, SC-007, SC-008)

**Checkpoint**: Scope-gating + reference-consistency + guard proven here; live cross-workspace/RLS row denial deferred.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T031 [P] Extend `tests/unit/test_import_boundaries.py` to assert `app_shared.models.competitors_matches`, `app_shared.url_safety`, `app_shared.url_pattern`, and `app_shared.matches.*` import cleanly with **no** fastapi / scrapy / twisted / playwright imports.
- [ ] T032 Run the DB-independent validation from `specs/005-competitors-matches/quickstart.md`: full `tests/unit` suite green + `scripts/check_workspace_scoping.py` exit 0 (both new models) + `scripts/check_single_head.sh` single head + import-boundary green.
- [ ] T033 [P] ⏸ DEFERRED (needs live Postgres) Run the online migration on a Postgres host: `alembic upgrade head` creates both tables + both unique keys + composite FKs + RLS; `alembic downgrade` reverses cleanly (matches → competitors). (FR-001)
- [ ] T034 [P] ⏸ DEFERRED (needs live Postgres) Execute the live-DB section of `specs/005-competitors-matches/quickstart.md` (end-to-end request flows: scope refusal `403` / write `200`, create with safe URL → normalized+pattern, unsafe URL rejected, bulk idempotency + reject-report, cross-workspace + RLS denial). (SC-002, SC-004, SC-006, SC-007, SC-008)

---

## FR / SC Coverage

| Requirement | Task(s) |
|-------------|---------|
| FR-001 two workspace-owned tables + RLS in creating migration + registered | T003, T005, T006, T007, T008, T009, T027, T033 |
| FR-002 cross-ws blocked by app scoping AND RLS (fail closed) + CI guard | T005, T006, T008, T027, T030 |
| FR-003 competitor fields + domain unique per workspace | T001, T003, T010, T011 |
| FR-004 match field shapes (raw+normalized URL, pattern+version, ids, priority, status, health) | T001, T003, T007, T018 |
| FR-005 4-col match unique; unlimited matches per variant | T003, T006, T007, T019, T021 |
| FR-006 all match refs resolve in-workspace; current_price_id soft | T003, T006, T019, T022, T025, T029, T030 |
| FR-007 save-time SSRF deny (localhost/private/loopback/link-local/unique-local/metadata/internal names) | T014, T015, T019 |
| FR-008 reject embedded credentials (userinfo) | T014, T015, T019 |
| FR-009 validator applies on create+update+bulk; never stored | T014, T019, T022, T025 |
| FR-010 normalization + pattern derivation rules | T016, T017, T019, T022, T025 |
| FR-011 URL_PATTERN_ALGORITHM_VERSION stored per row; no version mixing | T016, T017 |
| FR-012 competitors+matches endpoints under /v1, nothing outside scope | T011, T012, T019, T020, T025 |
| FR-013 set-based bulk upsert, bounded statements, reject-and-report | T022, T023, T024, T025 |
| FR-014 cursor pagination default 50 / max 500 | T011, T019, T025 |
| FR-015 workspace context + scope-gating per family | T011, T019, T025, T028 |
| FR-016 delete hard-vs-archive + response indicates outcome | T010, T011, T018, T019 |
| FR-017 health defaults on creation, not client-settable, not reset on re-push | T001, T003, T007, T018, T022 |
| SC-001 competitor created; domain unique per workspace | T011, T013 |
| SC-002 match stores normalized URL + pattern + version 100% | T007, T017, T021, T034 |
| SC-003 unlimited matches per variant; only exact-tuple dup rejected | T007, T021, T023 |
| SC-004 100% unsafe URLs rejected on create/update/bulk, never stored | T015, T021, T026, T030, T034 |
| SC-005 normalization/pattern corpus + version stamped | T017 |
| SC-006 bulk bounded statements + 0 duplicates on re-push | T023, T026, T034 |
| SC-007 two-workspace 0 rows, RLS, no-context fail closed, 0 cross-ws refs | T008, T029, T030, T034 |
| SC-008 read-only 0 writes; write succeeds; CI guard fails on unscoped query | T027, T028, T030, T034 |

Every FR-001..FR-017 and SC-001..SC-008 maps to ≥1 task. Every security/isolation checklist item (CHK001–CHK043) is covered: deny-list completeness → T014/T015; application points → T019/T022/T025; normalization/version → T016/T017; RLS+scoping → T008/T027/T028; reference integrity → T003/T029; bulk reject-and-report → T022/T023/T024/T025; health/deletion/pagination → T003/T007/T011/T019; unit-vs-live split → the ⏸ DEFERRED tasks.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies (T001 enums + T002 package init).
- **Foundational (Phase 2)**: depends on Setup (T001 enums used by T003 models); **blocks all user stories**. T004/T005/T006 depend on T003; T007–T009 depend on their targets (T003/T006).
- **US1 (Phase 3)**, **US2 (Phase 4)**, **US3 (Phase 5)**, **US4 (Phase 6)**: each depends on Foundational. US2 matches reference competitors, so the `competitors` table (Foundational) must exist; the competitor *router* (US1) is independent of the match router. US3 bulk reuses the US2 URL-safety/pattern cores (T014/T016) and extends the US2 matches schema/router (T018/T019). US4 tests the routers built in US1–US3. Recommended order by dependency: **US1 → US2 → US3 → US4** (all P1).
- **Polish (Phase 7)**: after the desired stories (T031 import-boundary needs T014/T016/T022 to exist).

### Within a story

- Core (`app_shared/url_safety.py`, `url_pattern.py`, `matches/upsert.py`) + its unit test before the router that uses it.
- `schemas/*.py` before/with the routers that import them.
- Router file before its `main.py` registration.
- Deferred (⏸) tasks are authored anytime but only pass on a Postgres host.

### Parallel Opportunities

- Setup: T001, T002 in parallel.
- Foundational: unit tests T007, T008, T009 in parallel once T003/T006 land.
- US1: T013 (deferred live) authored in parallel with T010/T011.
- US2: T014/T015 (url_safety + test) parallel with T016/T017 (url_pattern + test); T021 parallel.
- US3: T022/T023 (core + test) parallel; T026 parallel.
- US4: T027, T028, T029, T030 all parallel.
- Polish: T031, T033, T034 parallel.

---

## Parallel Example: US2 pure security cores

```bash
# The two highest-value pure modules + their corpora run fully in parallel (different files):
Task: "Create libs/shared/app_shared/url_safety.py + tests/unit/test_url_safety.py"
Task: "Create libs/shared/app_shared/url_pattern.py + tests/unit/test_url_pattern.py"
```

## Parallel Example: Foundational unit tests

```bash
# After T003–T006 land, run the DB-independent shape/RLS/offline-migration tests together:
Task: "Unit test tests/unit/test_competitors_matches_models.py"
Task: "Unit test tests/unit/test_rls_competitors_matches.py"
Task: "Unit test tests/unit/test_migration_offline_competitors_matches.py"
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1 Setup → Phase 2 Foundational (both models + migration + RLS, all shape/RLS/offline-migration tests green).
2. Phase 3 US1 → competitor CRUD with domain uniqueness + delete-outcome.
3. **STOP & VALIDATE**: run the unit suite; author the deferred live CRUD test for the PG host.

### Incremental Delivery

1. Setup + Foundational → foundation ready.
2. US1 (competitors, MVP) → US2 (safe single-match create: SSRF validator + URL pattern) → US3 (set-based bulk-upsert + reject-and-report) → US4 (isolation/reference-integrity/scope proof).
3. Polish: import-boundary guard for the new app_shared modules, quickstart unit validation; run the ⏸ DEFERRED live-DB + online-migration tasks on a Postgres-capable host.

### Deferred (live-Postgres) tasks

T013, T021, T026, T030, T033, T034 — authored here, left unchecked `- [ ]`, marked ⏸ DEFERRED (needs live Postgres). They cover the live halves of SC-001/002/003/004/006/007/008, cross-workspace + RLS row denial, and the online migration run.

---

## Notes

- `[P]` = different files, no incomplete-task dependency.
- `app_shared` stays FastAPI-free and scrapy/twisted/playwright-free (T031 guards this); Pydantic API DTOs live in `apps/api` (plan §Constitution I).
- Reuse, do NOT rebuild: `pagination.py` keyset helpers, `catalog.upsert.dedup_last_wins`, `catalog.consistency.assert_refs_in_workspace`, `app.schemas.catalog.DeleteOutcome`, `deps.require_scopes`, `scoped_select`/`scoped_get`, `WorkspaceScopedBase`/`emit_rls_policy`/`enum_column`, existing `Scope.COMPETITORS_*`/`MATCHES_*`, the AST scoping guard.
- No new API-key scopes are minted (bulk-upsert is a write → `matches:write`).
- Do NOT commit — the orchestrator commits after this step.
