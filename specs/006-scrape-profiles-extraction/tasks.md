---
description: "Dependency-ordered task list for SPEC-06 Scrape Profiles & Extraction Rules"
---

# Tasks: Scrape Profiles & Extraction Rules

**Input**: Design documents from `/specs/006-scrape-profiles-extraction/`

**Prerequisites**: plan.md, spec.md, research.md (D1–D11), data-model.md, contracts/ (10 files), quickstart.md

**Tests**: Every DB/Redis-**independent** behavior is unit-tested **here** (validators, resolution ordering/grouping/cache-key over in-memory inputs, confidence-default merge, bulk-upsert compile-to-SQL, dual-scope repository predicates, model/index/partial-unique shapes, global-readable RLS/DDL offline render, scope-gating wiring, single head). Live-Postgres/Redis acceptance tests (real CRUD, RLS row denial incl. global-read + global-write-block, cross-workspace assignment, Redis TTL/invalidation, online migration run, e2e batch resolution) are **authored + DEFERRED** — no Docker daemon / live Postgres/Redis in this build env. Deferred tasks stay unchecked `- [ ]` and are marked ⏸ DEFERRED (needs live Postgres/Redis).

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: `[US1]`/`[US2]`/`[US3]`/`[US4]` maps a task to a spec.md user story (Setup/Foundational/Polish carry no story label)
- Every task lists an exact repo-relative file path

---

## Scope Boundary (read first)

**IN SCOPE — exactly one new table + three FK promotions and their surface:**

- Table: `scrape_profiles` (**dual-scope** — workspace rows RLS-isolated; global `workspace_id IS NULL` rows read-shared + tenant-unwritable). Uses `Base + TimestampMixin` (NOT `WorkspaceScopedBase`), is NOT in `WORKSPACE_OWNED_MODELS`, gets the custom `emit_global_readable_rls_policy` (research D2/D4).
- FK promotions: `workspaces.default_scrape_profile_id`, `competitors.default_scrape_profile_id`, `competitor_product_matches.scrape_profile_id` → **plain** FK `scrape_profiles(id)` `ON DELETE SET NULL` (research D5).
- Endpoints under `/v1`: scrape-profiles (create/list/get/update/delete + `POST /bulk-upsert` + `PUT /workspace-default`); assignment enforcement wired into the existing competitors/matches routers.
- Behaviors: write-time profile validation (enum, regex compile + ReDoS screen, cookie session/auth deny, `validation_rules`/`confidence_rules`/money shape), DB-tunable confidence defaults, dual-scope repository (own+global read, own-only manage), cross-workspace assignment rejection, set-based bulk-upsert on the partial-index arbiter with reject-and-report, batch/grouped/Redis-cached config resolution with an explicit none-resolved sentinel, cursor pagination reuse, new scopes `scrape_profiles:read/write`.

**OUT OF SCOPE (do NOT build — later specs):** any execution of a stored selector/XPath/regex/JSON (SPEC-07+); `domain_strategy_profiles` (SPEC-12 — the domain-strategy resolution step is a tolerated `None` no-op here); access_policies / proxy_providers / domain_access_rules (SPEC-10); price_observations / prices / alerts. No automatic global-default seed (globals are created out-of-band via a BYPASSRLS platform path, research D11). Reuse unchanged: `pagination.py` keyset helpers, `catalog.upsert.dedup_last_wins`, `app.schemas.catalog.DeleteOutcome`, `deps.require_scopes`, `enum_column`, `Base`/`TimestampMixin`, `app_shared.money`, `app_shared.redis_client`, the AST scoping guard (ScrapeProfile intentionally excluded — research D2).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Enums, scopes, config, the pure money boundary, and the empty package that later files import. All DB/Redis-independent.

- [X] T001 [P] Extend `libs/shared/app_shared/enums.py` with three `StrEnum`s (research D1), string-backed via `enum_column` → `VARCHAR(32)`, tokens verbatim from §22/§20: `ScrapeProfileMode` (`HTTP`/`BROWSER`/`CUSTOM`), `AdapterKey` (`default_http`/`jsonld_first`/`selector_only`/`regex_only`/`shopify_product_json`/`woocommerce_store_api`/`playwright_rendered`/`custom_adapter` — lowercase), `VariantStrategy` (`PAGE_SINGLE_PRICE`/`URL_HAS_VARIANT_SELECTED`/`HTML_VARIANT_TABLE`/`EMBEDDED_JSON_VARIANTS`/`SELECT_VARIANT_WITH_PLAYWRIGHT`/`CUSTOM_VARIANT_ADAPTER`). (FR-001, FR-005)
- [X] T002 [P] Extend `libs/shared/app_shared/security/scopes.py` with `SCRAPE_PROFILES_READ = "scrape_profiles:read"` and `SCRAPE_PROFILES_WRITE = "scrape_profiles:write"` in the `Scope` vocabulary (per-entity precedent). (FR-004, FR-021)
- [X] T003 [P] Extend `libs/shared/app_shared/config.py` with `PROFILE_RESOLUTION_CACHE_TTL_SECONDS: int = 30` (short-TTL resolution cache, FR-019).
- [X] T004 [P] Extend `libs/shared/app_shared/money.py`: extract a **pure** `parse_money(value) -> Decimal` boundary (Decimal-only; reject `NaN`/`Infinity`; reject scale > 4 instead of rounding; non-negative; no float) reused by both `Money.process_bind_param` AND `profiles.validation`. §19 semantics. (FR-008, FR-022)
- [X] T005 [P] Create `libs/shared/app_shared/profiles/__init__.py` (empty package init for the framework-agnostic validators/confidence/resolution/repository/upsert core).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The `ScrapeProfile` ORM model, the custom global-readable RLS helper, model registration, the three FK-promotion ORM edits, and the single migration that creates the table + partial uniques + custom RLS + the three `ON DELETE SET NULL` FKs. **No user story can be implemented until this phase is complete.**

**⚠️ CRITICAL**: Blocks all of Phase 3–6.

- [X] T006 Create `libs/shared/app_shared/models/scrape_profiles.py` — `ScrapeProfile(Base, TimestampMixin)` (NOT `WorkspaceScopedBase`, research D2), per data-model.md: **nullable** indexed `workspace_id` + nullable FK → `workspaces.id` (NULL = global); the §22 columns (`name` Text; `mode`/`adapter_key`/`variant_strategy` via `enum_column`→`VARCHAR(32)` with documented defaults `HTTP`/`default_http`/`PAGE_SINGLE_PRICE`; the three `*_enabled` Boolean default `True`; the nullable `price_*`/`old_price_*`/`currency_*`/`stock_*`/`title_*` selector/xpath/regex Text; `JSONB` `variant_selector_config`/`price_transform_rules`/`validation_rules`/`confidence_rules`/`headers`/`cookies`; `wait_for_selector` Text; `request_timeout_ms` Integer default `30000`; nullable `browser_timeout_ms` Integer); **two partial unique indexes** — `uq_scrape_profiles_workspace_id_name` on `(workspace_id, name)` `WHERE workspace_id IS NOT NULL` and `uq_scrape_profiles_name_global` on `(name)` `WHERE workspace_id IS NULL`; explicit `ix_scrape_profiles_workspace_id`. (FR-001, FR-002, FR-003)
- [X] T007 [P] Extend `libs/shared/app_shared/models/rls.py` with `emit_global_readable_rls_policy(table)` → ENABLE + FORCE RLS, a `FOR SELECT` policy `USING (workspace_id IS NULL OR workspace_id = NULLIF(current_setting('app.workspace_id', true),'')::uuid)` (read own+global, fail-closed on own rows with no context, globals still visible) and a `FOR ALL` write policy `USING (workspace_id = <ctx>) WITH CHECK (workspace_id = <ctx>)` (tenant writes own-only, can never write a global row). (FR-004, FR-021)
- [X] T008 Re-export `ScrapeProfile` from `libs/shared/app_shared/models/__init__.py` so `Base.metadata` sees it for Alembic offline render; leave `WORKSPACE_OWNED_MODELS` in `repository.py` **unchanged** (ScrapeProfile deliberately excluded — dual-scope, research D2) (depends on T006).
- [X] T009 [P] Extend `libs/shared/app_shared/models/identity.py`: promote `workspaces.default_scrape_profile_id` to a plain FK → `scrape_profiles(id)` `ON DELETE SET NULL` (nullable; not composite — a global profile must be assignable by any workspace) (depends on T006). (FR-012, FR-023)
- [X] T010 [P] Extend `libs/shared/app_shared/models/competitors_matches.py`: promote `competitors.default_scrape_profile_id` and `competitor_product_matches.scrape_profile_id` to plain FKs → `scrape_profiles(id)` `ON DELETE SET NULL` (depends on T006). (FR-012, FR-023)
- [X] T011 Author the Alembic migration `alembic/versions/<rev>_scrape_profiles_table.py` with `down_revision = "f4c8a391d5c9"` (current single head): `create_table("scrape_profiles", ...)` (exact §22 shapes — `String(32)` enums, `JSONB` bundles, `Integer` timeouts), the two partial unique indexes with their `postgresql_where` predicates, the explicit `ix_scrape_profiles_workspace_id`, the nullable `workspace_id → workspaces.id` FK; then `emit_global_readable_rls_policy("scrape_profiles")`; then `ALTER` the three existing nullable columns into FKs `→ scrape_profiles(id)` `ON DELETE SET NULL` on `competitors`/`competitor_product_matches`/`workspaces`. FK-safe `downgrade()` drops the three FKs then the table; single head (depends on T006, T007, T008). (FR-001, FR-021, FR-023)
- [X] T012 [P] Unit test `tests/unit/test_scrape_profiles_models.py`: column set/types/nullability; **nullable** `workspace_id` + FK; both partial unique indexes present with exact `WHERE` predicates (`workspace_id IS NOT NULL` / `workspace_id IS NULL`); `ix_scrape_profiles_workspace_id`; documented defaults (`mode=HTTP`, `adapter_key=default_http`, three `*_enabled=True`, `variant_strategy=PAGE_SINGLE_PRICE`, `request_timeout_ms=30000`); enum columns render `VARCHAR(32)`; **every emitted constraint/index name ≤63 bytes** (depends on T006). (FR-001, FR-002, FR-003, SC-001)
- [X] T013 [P] Unit test `tests/unit/test_rls_scrape_profiles.py`: `emit_global_readable_rls_policy` renders ENABLE + FORCE + a `FOR SELECT` policy with the `(workspace_id IS NULL OR workspace_id = <ctx>)` USING clause + a `FOR ALL` write policy with own-only `USING` **and** `WITH CHECK` (depends on T007). (FR-004, FR-021, SC-007)
- [X] T014 [P] Unit test `tests/unit/test_migration_offline_scrape_profiles.py`: `alembic upgrade head --sql` renders `scrape_profiles` + both partial uniques + the custom global-readable RLS statements + the three `ON DELETE SET NULL` FK alterations; assert single head (`down_revision = f4c8a391d5c9`) (depends on T011). (FR-001, FR-021, FR-023)
- [X] T015 [P] Extend `tests/unit/test_scopes.py`: assert `scrape_profiles:read` and `scrape_profiles:write` are in the `Scope` vocabulary (depends on T002). (FR-004)

**Checkpoint**: Model + custom RLS + FK promotions + migration in place; DB-independent shape/partial-unique/RLS/offline-migration/scope tests green. User stories can begin.

---

## Phase 3: User Story 1 - Create and manage scrape profiles (Priority: P1) 🎯 MVP

**Goal**: Scrape-profile CRUD + set-based bulk-upsert with write-time validation. Create with name/mode/adapter/selectors/rule-bundles applying documented defaults; `name` unique per workspace; read/get/list (cursor-paginated)/update/delete workspace-scoped through the dual-scope repository (own+global read, own-only manage); invalid payloads rejected at save with a field-specific error; a mixed bulk batch upserts valid rows and reports rejected rows without aborting.

**Independent Test**: Create a profile with a name/mode/adapter/selectors/`validation_rules` → stored workspace-scoped with defaults for unset fields; duplicate `(workspace_id, name)` → `409`; read/update/list/delete are workspace-isolated; a payload with a bad enum / un-compilable regex / session cookie / malformed rules bundle → `422` field-specific, not stored; a global (`workspace_id IS NULL`) id is not manageable via the tenant path (`404`).

### Core logic (DB/Redis-independent) + tests

- [X] T016 [P] [US1] Create `libs/shared/app_shared/profiles/validation.py` (pure, framework-agnostic): `ProfileValidationError(field, code, message)`; `coerce_enums(payload)` (mode/adapter_key/variant_strategy against the T001 enums); `compile_regex_or_reject(pattern)` (compile + catastrophic-backtracking heuristic, applied to every `*_regex`, FR-006); `reject_session_cookies(cookies)` (auth/session cookie-name deny-list + heuristic; accept currency/locale technical cookies, FR-007); `validate_validation_rules(bundle)` (`required_currency` 3-letter; `min_price`/`max_price` via `money.parse_money` finite/scale/non-neg + `min ≤ max`; `reject_if_text_contains`/`prefer_text_contains` = list[str], FR-008/FR-022); `validate_confidence_rules(bundle)` (values in `[0,1]`, FR-009); `validate_profile(payload)` facade (depends on T001, T004). (FR-005, FR-006, FR-007, FR-008, FR-009, FR-022)
- [X] T017 [P] [US1] Unit test `tests/unit/test_profile_validation.py`: enum accept + reject corpus; regex compile-ok / un-compilable-reject / catastrophic-pattern-reject corpus; cookie technical-accept / session-auth-reject corpus; **positive empty-extraction case** — a profile with a valid mode/adapter but no selectors/xpath/regex is ACCEPTED, not rejected (spec Edge Cases "all extraction fields empty"). (Rules-bundle + money corpus added in US4/T043.) (FR-005, FR-006, FR-007, SC-006)
- [X] T018 [P] [US1] Create `libs/shared/app_shared/profiles/repository.py` (dual-scope query helpers): `visible_profiles_select(ws)` (own OR global, read/list), `owned_profile_select(ws)` / `owned_profile_get(session, id, ws)` (own-only, manage — never global/other-ws), `profile_visibility_map(...)`, `GLOBAL_DEFAULT_PROFILE_NAME = "global_default"` (depends on T006). (FR-004, FR-021)
- [X] T019 [P] [US1] Unit test `tests/unit/test_profiles_repository.py`: compile `visible_profiles_select` → emits `(workspace_id = <ctx> OR workspace_id IS NULL)`; `owned_profile_select` → emits `workspace_id = <ctx>` only (no global disjunct). (assert_profile_assignable cases added in US2/T031.) (depends on T018). (FR-004, FR-021, SC-007)
- [X] T020 [P] [US1] Create `libs/shared/app_shared/profiles/upsert.py` (pure, compiles statements — no execution): `build_profiles_upsert(rows) -> Insert` = one `pg_insert(ScrapeProfile).values([...]).on_conflict_do_update(index_elements=["workspace_id","name"], index_where=text("workspace_id IS NOT NULL"), set_={updatable cols + updated_at=func.now()})` (tenant-only arbiter, never writes a global row); `dedup_last_wins` keyed by `(workspace_id, name)` (reused from `catalog.upsert`); `prepare_profiles(rows) -> (valid, rejected)` running the T016 validators per row (reject-and-report, FR-020) (depends on T006, T016). (FR-020)
- [X] T021 [P] [US1] Unit test `tests/unit/test_profiles_upsert.py`: compile `build_profiles_upsert` to the `postgresql` dialect and assert `ON CONFLICT (workspace_id, name) WHERE workspace_id IS NOT NULL DO UPDATE SET ...` in **exactly one** statement (no per-row loop, SC-008); `dedup_last_wins` keeps last on `(workspace_id, name)`; `prepare_profiles` splits valid/rejected with the reject-report shape (depends on T020). (FR-020, SC-008)

### Schemas + endpoints

- [X] T022 [US1] Create `apps/api/app/schemas/scrape_profiles.py` (Pydantic v2, FastAPI-coupled): `ScrapeProfileCreate` (name+mode+adapter_key with server-side defaults; optional selectors/xpath/regex + JSON bundles + timeouts), `ScrapeProfileUpdate` (PATCH — all optional), `ScrapeProfileResponse`, `{items, next_cursor}` list envelope; `ScrapeProfileBulkUpsertRequest`, `ScrapeProfileBulkUpsertResult{upserted, profiles, rejected:[{index, name, field, code, reason}]}`; `WorkspaceDefaultProfileAssignment{profile_id: uuid|null}`. Reuse `app.schemas.catalog.DeleteOutcome` (depends on T001). (FR-001, FR-002, FR-010, FR-020)
- [X] T023 [US1] Create `apps/api/app/routers/scrape_profiles.py` (per contracts/api-scrape-profiles.md): `POST /v1/scrape-profiles` (require `scrape_profiles:write`; run `validate_profile` → `422` field-specific on reject; apply defaults; own-scoped insert; duplicate `(workspace_id, name)` → `409`), `GET /v1/scrape-profiles` (require `scrape_profiles:read`; `visible_profiles_select` own+global; keyset pagination via `pagination.py`, default 50 / max 500), `GET /v1/scrape-profiles/{id}` (read own+global; `404` otherwise), `PATCH /v1/scrape-profiles/{id}` (require write; `owned_profile_get` → `404` on a global/other-ws id via the tenant path, FR-021; re-`validate_profile`), `DELETE /v1/scrape-profiles/{id}` (require write; own-only hard delete → `{id, outcome}`), `POST /v1/scrape-profiles/bulk-upsert` (require write; `dedup_last_wins` → `prepare_profiles` (collect `rejected`) → `build_profiles_upsert` executed once under workspace context → `ScrapeProfileBulkUpsertResult`) (depends on T016, T018, T020, T022). (FR-002, FR-003, FR-004, FR-005, FR-006, FR-007, FR-008, FR-009, FR-020, FR-021)
- [X] T024 [US1] Register the scrape-profiles router in `apps/api/app/main.py` under `/v1` (depends on T023). (FR-004)
- [X] T025 [P] [US1] Unit test `tests/unit/test_scrape_profiles_scope_gating.py`: every scrape-profiles route declares the correct `require_scopes` (read for GET/list; write for POST/PATCH/DELETE/bulk-upsert) — assert via app route/dependency inspection AND a `TestClient` call with a principal lacking the scope → `403` (depends on T023). (FR-004, SC-007)
- [X] T026 [P] [US1] Unit test `tests/unit/test_scrape_profiles_routes_registered.py`: the scrape-profiles router (CRUD + bulk-upsert) is mounted under `/v1` in `main.py` (depends on T024). (FR-004)
- [ ] T027 [P] [US1] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_scrape_profiles_crud_live.py`: create with `validation_rules`/`confidence_rules` bundles → read back **byte-identical** (round-trip fidelity, FR-010); documented defaults applied; unique name per workspace → `409`; read/update/list/delete persistence; invalid payloads (enum/regex/cookie/rules) → `422`. (SC-001, SC-006)
- [ ] T028 [P] [US1] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_scrape_profiles_isolation_live.py`: cross-workspace profile invisible to writes; a global (`workspace_id IS NULL`) profile readable by **every** workspace; the tenant path **cannot** create/edit/delete a global row (RLS write-policy block, FR-021); no workspace context → zero own rows (globals still visible via the `IS NULL` disjunct). (SC-007)
- [ ] T029 [P] [US1] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_scrape_profiles_bulk_upsert_live.py`: mixed valid/invalid batch → all valid upserted, every invalid reported in `rejected[]`, batch never aborted; re-push updates in place (matched on `(workspace_id, name)`), 0 duplicates; bounded statement count; never writes a global row. (SC-008)

**Checkpoint**: Profile CRUD + validation + set-based bulk-upsert verified here (pure-logic corpus + compiled SQL + scope/route wiring); live CRUD/isolation/bulk deferred. MVP demoable.

---

## Phase 4: User Story 2 - Assign a profile to a competitor or a match (Priority: P1)

**Goal**: A profile can be assigned as a match override, a competitor default, or a workspace default; each assignment is accepted only when the profile is visible to the workspace (own or global), a cross-workspace/dangling reference is rejected at write (`422`), and clearing (null) is permitted.

**Independent Test**: Set `matches.scrape_profile_id` / `competitors.default_scrape_profile_id` / `workspaces.default_scrape_profile_id` to an own or global profile → accepted; point at another workspace's profile → rejected; clear (null) → accepted; `assert_profile_assignable` returns OK for None/visible and rejects cross-ws/dangling.

- [X] T030 [US2] Extend `libs/shared/app_shared/profiles/repository.py` with `assert_profile_assignable(session, ws, profile_id)`: `None` → OK (clearing); a visible (own or global) id → OK; a cross-workspace or dangling id → reject (FR-013). Reuses `visible_profiles_select` (depends on T018). (FR-013, FR-017)
- [X] T031 [P] [US2] Extend `tests/unit/test_profiles_repository.py`: `assert_profile_assignable` accepts `None` and a visible id (over an in-memory/mock visibility set), rejects a cross-workspace id and a dangling id (depends on T030). (FR-013, SC-002)
- [X] T032 [US2] Extend `apps/api/app/routers/competitors.py`: call `assert_profile_assignable` wherever `default_scrape_profile_id` is set (create + update) → a cross-workspace/dangling reference is a clean `422` (`WORKSPACE_MISMATCH`); null clears (depends on T030). (FR-012, FR-013)
- [X] T033 [US2] Extend `apps/api/app/routers/matches.py`: call `assert_profile_assignable` wherever `scrape_profile_id` is set (create + update + bulk-upsert) → cross-workspace/dangling → `422`; null clears (depends on T030). (FR-012, FR-013)
- [X] T034 [US2] Add `PUT /v1/scrape-profiles/workspace-default` to `apps/api/app/routers/scrape_profiles.py` (require `scrape_profiles:write`; `assert_profile_assignable` on the body `profile_id`; set `workspaces.default_scrape_profile_id`; accept null to clear) (depends on T023, T030). (FR-012, FR-013)
- [ ] T035 [P] [US2] ⏸ DEFERRED (needs live Postgres) Author `tests/integration/test_profile_assignment_live.py`: assign a profile as competitor default, match override, and workspace default — accepted for own+global, rejected cross-workspace (`422`), cleared with null; deleting a referenced profile nulls every reference (`ON DELETE SET NULL`, FR-023). (SC-002)

**Checkpoint**: Assignment visibility enforcement + workspace-default endpoint wired; `assert_profile_assignable` predicate proven here; live cross-workspace rejection + `ON DELETE SET NULL` deferred.

---

## Phase 5: User Story 3 - Resolve the final profile for a match (Priority: P1)

**Goal**: A batch-oriented config-resolution capability: for a match (or a `(competitor_id, url_pattern)` group) return the single scrape profile by walking match-override → domain-strategy(no-op `None`) → competitor-default → workspace-default → global-default, skipping non-visible (dangling/cross-ws) ids, returning an explicit `NONE_RESOLVED` when nothing supplies one; grouped per `(competitor_id, url_pattern)` (no per-match N+1) and Redis-cached with a short TTL keyed by `(workspace_id, competitor_id, url_pattern)`, invalidated on relevant writes (or superseded by TTL).

**Independent Test**: In-memory matches with every combination of set/unset overrides → resolution returns the expected profile at the correct precedence; a batch sharing a `(competitor_id, url_pattern)` resolves once per distinct group; `NONE_RESOLVED` when nothing supplies one; the domain-strategy step is a clean no-op; `resolution_cache_key` is deterministic + collision-free per tuple.

### Core logic (DB/Redis-independent) + tests

- [X] T036 [P] [US3] Create `libs/shared/app_shared/profiles/resolution.py` (pure core): `ResolvedProfile` + `NONE_RESOLVED` sentinel; `group_key(match) = (competitor_id, url_pattern)`; `group_matches(rows)` (one bucket per distinct group); `resolve_group(competitor_default, workspace_default, global_default, visible_ids, domain_strategy=None)` walking domain-strategy(no-op `None`) → competitor → workspace → global, keeping each candidate only if in `visible_ids` else falling through (FR-014/FR-015/FR-016/FR-017); `apply_match_override(group_result, override_id, visible_ids)` (match override at highest precedence, after the cached group result); `resolution_cache_key(workspace_id, competitor_id, url_pattern)` (deterministic; `hashlib` on `url_pattern`). (FR-014, FR-015, FR-016, FR-017, FR-018)
- [X] T037 [P] [US3] Unit test `tests/unit/test_profile_resolution.py`: chain ordering across all precedence combos (match/competitor/workspace/global); visibility fall-through (dangling/cross-ws id → unset → next level); domain-strategy `None` no-op skip; `NONE_RESOLVED` when nothing supplies one; `group_matches` yields one result per distinct group; `apply_match_override` beats the group result (depends on T036). (FR-014, FR-015, FR-016, FR-017, FR-018, SC-003)
- [X] T038 [P] [US3] Unit test `tests/unit/test_profile_resolution_cache_key.py`: `resolution_cache_key` deterministic for a given `(workspace_id, competitor_id, url_pattern)` and collision-free across distinct tuples (depends on T036). (FR-019, SC-005)

### Cache-driving orchestrator

- [X] T039 [US3] Create `apps/api/app/services/profile_resolution.py` (cache orchestrator, `apps/api` — keeps `app_shared` Redis-driving-free): load the **bounded** inputs (workspace default; competitor defaults via one `IN (...)` over distinct competitor ids; global default by reserved `GLOBAL_DEFAULT_PROFILE_NAME` + `workspace_id IS NULL`; visible-id set via one `IN (...)`), drive `resolution.resolve_group` per group with Redis `get`/`set` (TTL = `PROFILE_RESOLUTION_CACHE_TTL_SECONDS`) keyed by `resolution_cache_key`, plus `invalidate_resolution_cache(redis, ws, competitor_id)` best-effort prefix delete on profile/assignment writes. Internal batch API (SPEC-07 refresh), not a new public endpoint (depends on T036, T018, T003). (FR-018, FR-019)
- [ ] T040 [P] [US3] ⏸ DEFERRED (needs live Postgres + Redis) Author `tests/integration/test_profile_resolution_live.py`: end-to-end precedence match→competitor→workspace→global returns exactly the dictated profile; `NONE_RESOLVED` when nothing supplies one; a batch of ≥10k matches over a few `(competitor_id, url_pattern)` groups performs lookups proportional to groups, not matches (no per-match N+1); a second resolution within the TTL is a Redis cache hit; a relevant write is reflected within the TTL (or immediately if invalidated). (SC-003, SC-004, SC-005)

**Checkpoint**: Pure resolution core (ordering/grouping/visibility/none-resolved) + deterministic cache key proven here; orchestrator wired; live batch/N+1/Redis-TTL deferred.

---

## Phase 6: User Story 4 - Store and read validation & confidence rules (Priority: P2)

**Goal**: The `validation_rules` and `confidence_rules` bundles are shape-validated at write and read back exactly as stored; the documented default confidences (§17) + minimum accepted (0.75) + promotion threshold (0.85) are DB-tunable config exposed via an accessor that overlays a profile's overrides — never hardcoded literals.

**Independent Test**: A `validation_rules` bundle (currency/min/max/text-lists) and a `confidence_rules` bundle round-trip after validation; invalid bundles (`min_price > max_price`, confidence outside `[0,1]`, non-list text field, non-finite/over-scale money, bad currency) → rejected; `resolve_confidence_rules` returns §17 defaults where unspecified and the profile's overrides where present.

- [ ] T041 [P] [US4] Create `libs/shared/app_shared/profiles/confidence.py`: `DEFAULT_CONFIDENCE_RULES` (§17: platform_variant_json 0.95, jsonld 0.95, embedded_json 0.90, css 0.85, xpath 0.85, regex 0.75, playwright 0.80, single_number 0.40), `DEFAULT_MIN_ACCEPTED_CONFIDENCE = 0.75`, `DEFAULT_PROMOTION_THRESHOLD = 0.85`; `resolve_confidence_rules(profile_rules)` overlays a profile's validated overrides over the defaults (DB-tunable, FR-011). (FR-011)
- [ ] T042 [P] [US4] Unit test `tests/unit/test_confidence_defaults.py`: `DEFAULT_*` values match §17 exactly; `resolve_confidence_rules` returns defaults where unspecified and overrides where present; unknown keys handled per contract (depends on T041). (FR-011, SC-001)
- [ ] T043 [P] [US4] Extend `tests/unit/test_profile_validation.py` with the rules corpus: `validation_rules` — `required_currency` 3-letter accept/reject, `min_price`/`max_price` money finite + scale ≤ 4 + non-negative + `min ≤ max` accept/reject, `reject_if_text_contains`/`prefer_text_contains` list[str] accept / non-list reject; `confidence_rules` values in `[0,1]` accept / outside reject (depends on T016). (FR-008, FR-009, FR-022, SC-006)
- [ ] T044 [P] [US4] Extend `tests/unit/test_money.py`: the extracted `parse_money` boundary — accepts finite Decimal ≤ scale 4 non-negative; rejects `NaN`/`Infinity`, scale > 4 (no rounding), negative, and float input (depends on T004). (FR-022, SC-006)

**Checkpoint**: Confidence defaults accessor + rules-bundle/money validator corpus proven here; live round-trip of the bundles is covered by T027.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T045 [P] Extend `tests/unit/test_import_boundaries.py` to assert `app_shared.models.scrape_profiles` and `app_shared.profiles.*` (validation, confidence, resolution, repository, upsert) import cleanly with **no** fastapi / scrapy / twisted / playwright imports (depends on T006, T016, T018, T020, T036, T041).
- [ ] T046 Run the DB/Redis-independent validation from `specs/006-scrape-profiles-extraction/quickstart.md`: full `tests/unit` suite green + `uv run python scripts/check_workspace_scoping.py` exit 0 (ScrapeProfile intentionally out of the guarded set, research D2) + `bash scripts/check_single_head.sh` single head + import-boundary green.
- [ ] T047 [P] ⏸ DEFERRED (needs live Postgres) Run the online migration on a Postgres host: `alembic upgrade head` creates `scrape_profiles` + both partial uniques + the custom global-readable RLS + promotes the three `ON DELETE SET NULL` FKs; `alembic downgrade` reverses cleanly (drop FKs → drop table). (FR-001, FR-021, FR-023)
- [ ] T048 [P] ⏸ DEFERRED (needs live Postgres + Redis) Execute the live section of `specs/006-scrape-profiles-extraction/quickstart.md` end-to-end: CRUD round-trip + `422` rejections; cross-workspace + global-read + global-write-block RLS denial; assignment accept/reject + `ON DELETE SET NULL`; bulk reject-and-report + idempotency; batch resolution precedence + no-N+1 + Redis TTL/invalidation. (SC-001, SC-002, SC-003, SC-004, SC-005, SC-006, SC-007, SC-008)

---

## FR / SC Coverage

| Requirement | Task(s) |
|-------------|---------|
| FR-001 `scrape_profiles` entity (§22 shape, nullable ws_id) | T001, T006, T011, T012, T014, T022, T047 |
| FR-002 create persists all fields + documented defaults | T006, T012, T022, T023, T027 |
| FR-003 name unique per workspace (partial unique) | T006, T012, T023, T027 |
| FR-004 read/update/list(paginated)/delete workspace-scoped | T002, T015, T018, T019, T023, T024, T025, T026 |
| FR-005 enum validation at write | T001, T016, T017, T023 |
| FR-006 `*_regex` compile + ReDoS reject | T016, T017, T023 |
| FR-007 cookie session/auth deny | T016, T017, T023 |
| FR-008 `validation_rules` currency/money/text-list | T004, T016, T043, T044 |
| FR-009 `confidence_rules` `[0,1]` | T016, T043 |
| FR-010 rules/JSON round-trip fidelity | T022, T027 |
| FR-011 DB-tunable confidence defaults accessor | T041, T042 |
| FR-012 assignable as match/competitor/workspace default | T009, T010, T032, T033, T034 |
| FR-013 reject cross-ws assignment; allow global; null clears | T030, T031, T032, T033, T034, T035 |
| FR-014 resolution chain match→competitor→workspace→global | T036, T037, T039, T040 |
| FR-015 domain-strategy step optional no-op | T036, T037 |
| FR-016 explicit none-resolved (`NONE_RESOLVED`) | T036, T037, T040 |
| FR-017 dangling/cross-ws ref unset at resolution | T030, T036, T037 |
| FR-018 batch grouped per `(competitor_id, url_pattern)`, no N+1 | T036, T037, T039, T040 |
| FR-019 Redis short-TTL cache + invalidation | T003, T036, T038, T039, T040 |
| FR-020 set-based bulk-upsert reject-and-report | T020, T021, T023, T029 |
| FR-021 tenant path cannot write global rows | T007, T013, T018, T019, T023, T028 |
| FR-022 money §19 (finite/scale≤4/non-neg, no round) | T004, T016, T043, T044 |
| FR-023 delete leaves no dangling ref (`ON DELETE SET NULL`) | T009, T010, T011, T014, T035, T047 |
| SC-001 create + read back every field identical | T012, T022, T027, T042 |
| SC-002 assign only when visible; each precedence level | T031, T032, T033, T034, T035 |
| SC-003 precedence-correct resolution + none-resolved | T037, T040 |
| SC-004 batch lookups proportional to groups, not matches | T036, T039, T040 |
| SC-005 cache hit within TTL; write reflected within TTL | T038, T039, T040 |
| SC-006 100% invalid writes rejected field-specific | T017, T027, T043, T044 |
| SC-007 no cross-ws read/write/assign; globals read-only via tenant | T013, T019, T025, T028 |
| SC-008 bulk upserts valid, reports rejected, no abort | T021, T029 |

Every FR-001..FR-023 and SC-001..SC-008 maps to ≥1 task.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies (T001–T005 all `[P]`, different files).
- **Foundational (Phase 2)**: depends on Setup (T001 enums → T006 model; T002 scopes → T015; T004 money → later validation). **Blocks all user stories.** T008/T009/T010/T011 depend on T006; T007 is `[P]` (different file); T011 also depends on T007/T008; unit tests T012–T015 depend on their targets.
- **US1 (Phase 3)**: depends on Foundational. Core modules T016/T018/T020 (+ tests T017/T019/T021) are `[P]`; schemas T022 then router T023 then `main.py` T024; wiring tests T025/T026.
- **US2 (Phase 4)**: depends on US1 (T018 repository, T023 router). T030 extends the repository; T032/T033/T034 wire it into routers.
- **US3 (Phase 5)**: depends on Foundational; the orchestrator T039 also depends on T018 (repository) + T003 (TTL config). Pure core T036 (+ tests T037/T038) is independent of US1/US2.
- **US4 (Phase 6)**: depends on Foundational; T041 independent; T043 extends the US1 validation test (T016); T044 depends on T004.
- **Polish (Phase 7)**: after the desired stories (T045 import-boundary needs all `app_shared.profiles.*` modules to exist).

### Within a story

- `app_shared` core module + its unit test before the `apps/api` router/service that uses it.
- `schemas/*.py` before/with the router that imports them; router before its `main.py` registration.
- Deferred (⏸) tasks are authored anytime but only pass on a Postgres/Redis host.

### Parallel Opportunities

- Setup: T001–T005 all in parallel.
- Foundational: T007 parallel with T006; unit tests T012, T013, T014, T015 in parallel once their targets land.
- US1: core trio T016/T017, T018/T019, T020/T021 in parallel; wiring tests T025/T026 parallel; deferred T027/T028/T029 parallel.
- US2: T031 + deferred T035 parallel; router edits T032/T033 are different files (parallel), T034 after T023.
- US3: T036 → T037/T038 parallel; deferred T040 parallel.
- US4: T041/T042, T043, T044 all parallel (different files).
- Polish: T045, T047, T048 parallel.

---

## Parallel Example: US1 pure `app_shared` cores

```bash
# The three DB-independent profile cores + their corpora run fully in parallel (different files):
Task: "Create libs/shared/app_shared/profiles/validation.py + tests/unit/test_profile_validation.py"
Task: "Create libs/shared/app_shared/profiles/repository.py + tests/unit/test_profiles_repository.py"
Task: "Create libs/shared/app_shared/profiles/upsert.py + tests/unit/test_profiles_upsert.py"
```

## Parallel Example: Foundational unit tests

```bash
# After T006/T007/T011 land, run the DB-independent shape/RLS/offline-migration/scope tests together:
Task: "Unit test tests/unit/test_scrape_profiles_models.py"
Task: "Unit test tests/unit/test_rls_scrape_profiles.py"
Task: "Unit test tests/unit/test_migration_offline_scrape_profiles.py"
Task: "Extend tests/unit/test_scopes.py"
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1 Setup → Phase 2 Foundational (model + custom RLS + FK promotions + migration; all shape/RLS/offline/scope tests green).
2. Phase 3 US1 → profile CRUD + validation + set-based bulk-upsert through the dual-scope repository.
3. **STOP & VALIDATE**: run the unit suite; author the deferred live CRUD/isolation/bulk tests for the PG/Redis host.

### Incremental Delivery

1. Setup + Foundational → foundation ready.
2. US1 (CRUD + management, MVP) → US2 (assignment enforcement) → US3 (batch/grouped/cached resolution) → US4 (validation/confidence rule bundles + defaults).
3. Polish: import-boundary guard for the new `app_shared.profiles.*` modules, quickstart unit validation; run the ⏸ DEFERRED live-DB/Redis + online-migration tasks on a Postgres+Redis host.

### Deferred (live-Postgres/Redis) tasks

T027, T028, T029, T035, T040, T047, T048 — authored here, left unchecked `- [ ]`, marked ⏸ DEFERRED (needs live Postgres/Redis). They cover the live halves of SC-001..SC-008: real CRUD round-trip + `422` rejections, RLS row denial (cross-workspace + global-read + global-write-block), cross-workspace assignment + `ON DELETE SET NULL`, bulk reject-and-report + idempotency, end-to-end batch resolution (no per-match N+1) + Redis TTL/invalidation, and the online migration upgrade/downgrade.

---

## Notes

- `[P]` = different files, no incomplete-task dependency.
- `app_shared` stays FastAPI-free and scrapy/twisted/playwright-free (T045 guards this); Pydantic API DTOs, routers, scope-gating, and the Redis-driving resolution orchestrator live in `apps/api` (plan §Constitution I).
- `ScrapeProfile` is **dual-scope**: `Base + TimestampMixin` (not `WorkspaceScopedBase`), **not** in `WORKSPACE_OWNED_MODELS`, custom `emit_global_readable_rls_policy`; every profile query goes through `app_shared.profiles.repository` (research D2/D4).
- Reuse, do NOT rebuild: `pagination.py` keyset helpers, `catalog.upsert.dedup_last_wins`, `app.schemas.catalog.DeleteOutcome`, `deps.require_scopes`, `enum_column`, `app_shared.money.parse_money`, `app_shared.redis_client`, the AST scoping guard (ScrapeProfile intentionally excluded).
- Two new scopes minted: `scrape_profiles:read`/`scrape_profiles:write`.
- Do NOT commit — the orchestrator commits after this step.
