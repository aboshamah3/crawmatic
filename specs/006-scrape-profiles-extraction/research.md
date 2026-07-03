# Phase 0 Research: Scrape Profiles & Extraction Rules

Source of truth: `PROJECT_SPEC.md` §9 (config philosophy + scrape-profile resolution chain + resolution caching), §16 (extraction strategy/regex), §17 (confidence defaults + min 0.75 + promotion 0.85), §18 (validation rules), §19 (money — Decimal/finite/scale), §20 (six variant strategies), §22 (`scrape_profiles` columns + enums + cookie guardrail), §24 (endpoints + pagination), §30 (legal/cookie guardrails), §32 (workspace isolation). Built on SPEC-02 (`Base`, `TimestampMixin`, `TZDateTime`, `enum_column`, `Money`, `emit_rls_policy`, `NAMING_CONVENTION`), SPEC-03 (`WORKSPACE_OWNED_MODELS`, scoped helpers, `set_workspace_context`, scopes, redis client, status cache pattern), SPEC-04 (partial-unique-index + `ON CONFLICT` inference precedent, keyset pagination, set-based upsert + `dedup_last_wins`, consistency helper), SPEC-05 (competitors/matches, the three assignment columns, explicit ≤63-byte constraint names).

Format: **Decision / Rationale / Alternatives considered** per resolved unknown.

---

## D1 — Extraction enums (`mode`, `adapter_key`, `variant_strategy`)

**Decision.** Add three `StrEnum`s to `app_shared/enums.py`, string-backed via `enum_column` → plain `VARCHAR(32)` (never a PG-native enum), exactly like every prior enum:
- `ScrapeProfileMode`: `HTTP`, `BROWSER`, `CUSTOM` (§22 "Scrape profile modes", uppercase).
- `AdapterKey`: `default_http`, `jsonld_first`, `selector_only`, `regex_only`, `shopify_product_json`, `woocommerce_store_api`, `playwright_rendered`, `custom_adapter` (§22 "Adapter keys", **lowercase** verbatim).
- `VariantStrategy`: `PAGE_SINGLE_PRICE`, `URL_HAS_VARIANT_SELECTED`, `HTML_VARIANT_TABLE`, `EMBEDDED_JSON_VARIANTS`, `SELECT_VARIANT_WITH_PLAYWRIGHT`, `CUSTOM_VARIANT_ADAPTER` (§20, uppercase).

**Rationale.** Tokens are copied verbatim from §22/§20 (mixed case is intentional — the doc lists adapter keys lowercase and modes/strategies uppercase). `enum_column`'s `_AppValidatedEnumString` validates membership at bind/result time and raises `ValueError` on an out-of-set value → a clean write-time rejection (FR-005), no DB `CHECK`.

**Alternatives considered.** PG-native `ENUM` — rejected project-wide (SPEC-02 [analyze A2]); SQLAlchemy `Enum` type — same rejection.

---

## D2 — Dual-scope model: nullable `workspace_id`, not `WorkspaceScopedBase`, not in `WORKSPACE_OWNED_MODELS`, and CI-guard coverage

**Decision.** `ScrapeProfile` subclasses `Base, TimestampMixin` only (NOT `WorkspaceScopedBase`) and declares its own **nullable**, indexed `workspace_id` with a nullable FK → `workspaces.id`. It is **not** added to `app_shared.repository.WORKSPACE_OWNED_MODELS`. All profile queries go through a dedicated `app_shared/profiles/repository.py`:
- reads that must see global rows use `visible_profiles_select(ws)` → `where(or_(ScrapeProfile.workspace_id == ws, ScrapeProfile.workspace_id.is_(None)))`;
- management (create/update/delete/target-of-tenant-write) uses `owned_profile_select(ws)`/`owned_profile_get(...)` → `where(ScrapeProfile.workspace_id == ws)` (never global).

**Rationale.** §9's terminal fallback is "a `scrape_profiles` row with `workspace_id IS NULL`" readable by every workspace; §22 marks `workspace_id` nullable. `WorkspaceScopedBase` forces `NOT NULL`, and `scoped_select`/`scoped_get` (which back `WORKSPACE_OWNED_MODELS`) constrain to `workspace_id = ctx`, hiding global rows — both are structurally wrong for a dual-scope table. The dedicated repository is the single sanctioned query path (same discipline as `app_shared.repository` for strictly-owned models). Both read and write helpers still reference `workspace_id` in their predicate, so the AST scoping guard (`scripts/check_workspace_scoping.py`) — which only flags *models in `WORKSPACE_OWNED_MODELS`* — is not weakened: `ScrapeProfile` is out of that set, so the guard is inert for it, and correctness rests on (a) the dual-scope repository being the only place profiles are queried, (b) the custom RLS pair (D4) as DB-level defense-in-depth, and (c) unit tests asserting the repository predicates. A future bare `select(ScrapeProfile)` is caught in review + by the RLS write/read policies, not the guard.

**Alternatives considered.**
- *Add `ScrapeProfile` to `WORKSPACE_OWNED_MODELS` and union global rows at each call site* — rejected: every read would need a manual global-union, easy to forget, and `scoped_get` would still 404 a legitimately-visible global row.
- *Extend the guard with a second "dual-scope" registry* — deferred as out-of-scope gold-plating; the guard's value is for the strictly-owned models, and the profile path is small and centralised. Recorded as a possible future hardening.
- *Sentinel "global" workspace row* — rejected (fakes a nonexistent tenant, breaks the FK, leaks into tenant queries).

---

## D3 — Uniqueness of `name` with a nullable `workspace_id`

**Decision.** Two **partial** unique indexes (mirroring the SPEC-04 `products` partial-unique precedent):
- `uq_scrape_profiles_workspace_id_name` on `(workspace_id, name)` `WHERE workspace_id IS NOT NULL` — tenant uniqueness (FR-003).
- `uq_scrape_profiles_name_global` on `(name)` `WHERE workspace_id IS NULL` — global uniqueness.

**Rationale.** A plain `UNIQUE(workspace_id, name)` treats `NULL` workspace ids as mutually distinct (SQL `NULL` semantics), so two global rows with the same `name` would both be allowed — wrong. Splitting into two partial indexes gives per-tenant uniqueness and single-global-namespace uniqueness. The tenant bulk-upsert (D9) infers the first index via `index_where=text("workspace_id IS NOT NULL")`, matching the predicate exactly (SPEC-04 `ON CONFLICT` inference requirement). Both names are ≤38 chars, well under the 63-byte cap — no explicit-name shortening needed (unlike SPEC-05's `cpm`).

**Alternatives considered.** `COALESCE(workspace_id, '00000000-...')` expression index — rejected: needs a magic UUID and complicates `ON CONFLICT` inference; two partial indexes are clearer and match precedent.

---

## D4 — Custom global-readable RLS policy pair

**Decision.** Add `emit_global_readable_rls_policy(table_name, *, workspace_column="workspace_id")` to `app_shared/models/rls.py`, returning (in order):
1. `ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;`
2. `ALTER TABLE {t} FORCE ROW LEVEL SECURITY;`
3. `CREATE POLICY {t}_workspace_read ON {t} FOR SELECT USING ({col} IS NULL OR {col} = NULLIF(current_setting('app.workspace_id', true), '')::uuid);`
4. `CREATE POLICY {t}_workspace_write ON {t} FOR ALL USING ({col} = NULLIF(current_setting('app.workspace_id', true), '')::uuid) WITH CHECK ({col} = NULLIF(current_setting('app.workspace_id', true), '')::uuid);`

**Rationale.** The standard `emit_rls_policy` USING `({col} = ctx)` makes `NULL`-workspace rows invisible to everyone (`NULL = ctx` → `NULL`, never true) — the opposite of the §9 requirement. With Postgres permissive policies, `SELECT` sees the OR of applicable `FOR SELECT`/`FOR ALL` `USING` clauses → own + global (FR-013 "global MAY be assigned by any workspace" depends on global being readable). `INSERT` is gated only by the `FOR ALL` `WITH CHECK` → `workspace_id` must equal the context, so a tenant can never insert a `NULL`-workspace (global) row (FR-021). `UPDATE`/`DELETE` are gated by the `FOR ALL` `USING` → a tenant can only mutate its own rows, never a global one (FR-021). Fail-closed `NULLIF(...)` semantics are preserved from the SPEC-02 emitter. Kept as a *separate* function so the existing `emit_rls_policy` (used by five prior tables) is untouched.

**Alternatives considered.** App-layer-only global visibility (standard RLS + app union) — rejected: leaves the DB layer unable to express read-shared/write-protected, breaking the two-layer model. A single `FOR ALL` policy with an OR in `USING` and a strict `WITH CHECK` — viable, but a dedicated `FOR SELECT` read policy makes the read-vs-write asymmetry explicit and easier to test.

---

## D5 — Promote the three assignment columns to FKs? (plain FK `ON DELETE SET NULL` vs composite vs soft)

**Decision.** Promote all three existing nullable columns to **plain** FKs → `scrape_profiles(id)` `ON DELETE SET NULL`, via `ALTER TABLE` in this spec's migration:
- `competitors.default_scrape_profile_id`
- `workspaces.default_scrape_profile_id`
- `competitor_product_matches.scrape_profile_id`

**Rationale.**
- A **workspace-local composite** FK `(workspace_id, scrape_profile_id) → scrape_profiles(workspace_id, id)` (the SPEC-04/05 pattern) is **wrong here**: it would force the referenced profile into the *same* workspace, making it impossible to assign a **global** (`workspace_id IS NULL`) profile — which §9/FR-013 explicitly allow. So the composite pattern is rejected for this table.
- A **plain** FK to the globally-unique PK `scrape_profiles(id)` works for both own-workspace and global targets and gives real referential integrity (no dangling id ever persists).
- Cross-workspace assignment is **not** preventable by a plain FK (any existing id passes), so it is enforced at write time by `assert_profile_assignable` (FR-013, app-layer) and defensively ignored at resolution (FR-017). This is the intended division: DB guarantees existence; app guarantees visibility.
- `ON DELETE SET NULL` directly satisfies FR-023: deleting a profile nulls every referencing assignment (across all tables, via FK triggers that bypass RLS), so no assignment is ever left pointing at a nonexistent profile, and resolution falls through cleanly — with no block-on-referenced check and no hot-path scan. (FK referential-integrity checks run with row-security effectively off, so assigning/deleting a global row is not blocked by RLS at the *constraint* level.)

**Alternatives considered.**
- *Leave the columns soft (no FK)* — rejected: FR-012 asks the table to actually back the references, and a soft ref can dangle after a delete (FR-023 violation) unless the app scans, which is exactly the hot-path scan Principle VIII forbids.
- *Block deletion while referenced* — rejected in favour of `SET NULL` (simpler, no cross-table existence scan, and "fall-through on delete" is an allowed FR-023 policy).

---

## D6 — Resolution chain, grouping, none-resolved, and identifying the "global default"

**Decision.** The pure core (`app_shared/profiles/resolution.py`) resolves per §9:
1. **Match override** (`competitor_product_matches.scrape_profile_id`) — per match, highest precedence.
2. **Domain-strategy preferred** — a tolerated **no-op returning `None`** in this spec (SPEC-12 builds `domain_strategy_profiles`); skipped cleanly (FR-015).
3. **Competitor default** (`competitors.default_scrape_profile_id`).
4. **Workspace default** (`workspaces.default_scrape_profile_id`).
5. **Global default** — the single global row (`workspace_id IS NULL`) whose `name == GLOBAL_DEFAULT_PROFILE_NAME` (constant `"global_default"`).

`resolve_group(...)` walks steps 2→5 (constant within a `(competitor_id, url_pattern)` group), each candidate id kept only if it is in the `visible_ids` set (own+global); a dangling/cross-workspace id is skipped (FR-017). `apply_match_override(group_result, override_id, visible_ids)` layers the per-match step 1 on top. When no step yields a visible id, the result is the explicit `NONE_RESOLVED` sentinel (FR-016) — never an error, never an arbitrary row.

**Rationale.** Steps 3–5 are invariant across all matches in a `(competitor_id, url_pattern)` group (same competitor, same workspace, same global), so they are resolved once per group (FR-018, SC-004); only the match override (step 1) is per-match, and it needs no DB walk (the id is already on the match row). The global default needs a deterministic identity with no dedicated "is_global_default" column in §22; a reserved `name` (`"global_default"`) is the least-invasive deterministic marker and keeps the terminal fallback a normal, editable global row. If that row is absent, step 5 yields nothing → `NONE_RESOLVED`.

**Alternatives considered.**
- *A new `is_default` boolean column on `scrape_profiles`* — rejected: §22 doesn't define it; a reserved name avoids a schema addition beyond the doc.
- *Resolve the match override inside the grouped walk* — rejected: overrides are per-match, so folding them into the group key would explode the group count and defeat the N+1 avoidance.

---

## D7 — Resolution caching (Redis, key, TTL, invalidation)

**Decision.** Cache the **group** resolution (steps 2–5 result) in Redis, keyed by `resolution_cache_key(ws, competitor_id, url_pattern)` = `f"profres:{ws}:{competitor_id}:{sha1(url_pattern)}"`, storing the resolved profile id or a `"none"` sentinel string, with TTL `Settings.PROFILE_RESOLUTION_CACHE_TTL_SECONDS` (default `30`, mirroring `STATUS_CACHE_TTL_SECONDS`). The `apps/api` orchestrator (`services/profile_resolution.py`) does Redis `GET` per distinct group, computes+`SET`s misses, and applies the per-match override after the cache lookup. Invalidation on a profile/assignment write is best-effort `invalidate_resolution_cache(redis, ws, competitor_id=None)` via a `SCAN`+`DEL` over the `profres:{ws}:*` (or `profres:{ws}:{competitor_id}:*`) prefix; correctness otherwise rests on the short TTL (FR-019, SC-005). All Redis access is fail-open for reads (a Redis miss/error just re-walks the chain) — never fail-closed, since a stale/absent cache only costs a recompute, not a security boundary.

**Rationale.** §9 mandates the exact key tuple and a short TTL, and permits "invalidate on writes **or** rely on the short TTL." `url_pattern` is hashed into the key to bound key length and avoid delimiter collisions. The override is applied *after* the cache because it is per-match and must not pollute the per-group cache entry. Writes are rare relative to resolutions, so a `SCAN`-based prefix delete off the hot path is acceptable; the TTL is the backstop.

**Alternatives considered.** Per-workspace generation counter embedded in the key (bump-to-invalidate, avoids `SCAN`) — a valid optimization, noted for a later spec; the TTL+SCAN approach is simpler and sufficient at this scale. Caching the whole resolved profile row — rejected: the profile can be re-read by id cheaply and caching the id keeps entries tiny and always-fresh-on-read.

---

## D8 — Default confidence values as DB-tunable config

**Decision.** `app_shared/profiles/confidence.py` exposes the §17 values as module constants — `DEFAULT_CONFIDENCE_RULES` (platform_variant_json 0.95, jsonld 0.95, embedded_json 0.90, css 0.85, xpath 0.85, regex 0.75, playwright 0.80, single_number 0.40), `DEFAULT_MIN_ACCEPTED_CONFIDENCE = 0.75`, `DEFAULT_PROMOTION_THRESHOLD = 0.85` — plus `resolve_confidence_rules(profile_rules) → dict` that overlays a profile's `confidence_rules` (validated to `[0,1]`) on top of the defaults.

**Rationale.** FR-011 requires these be "DB-tunable config, not hardcoded literals baked into the extractor." The accessor merges per-profile overrides (the DB-tunable part) over documented defaults, so the extractor (SPEC-07+) reads through `resolve_confidence_rules(profile.confidence_rules)` and never hardcodes a threshold. The constants are the *fallback*, not the source of truth — the profile's `confidence_rules` wins whenever present.

**Alternatives considered.** A `confidence_defaults` DB table — rejected as beyond §22 (no such table); per-profile `confidence_rules` already provides the tuning surface, with module constants as the documented default. `single_number` default (0.40, "reject by default") is stored as a default confidence but the reject decision belongs to SPEC-07's extractor, not here.

---

## D9 — Profile validators (regex ReDoS, cookie deny, validation/confidence/money) and bulk-upsert arbiter

**Decision.** `app_shared/profiles/validation.py` (pure) with a structured `ProfileValidationError(field, code, message)`:
- **Enums**: coerce `mode`/`adapter_key`/`variant_strategy` via the D1 enums (out-of-set → reject).
- **Regex** (`compile_regex_or_reject`): `re.compile` each `*_regex` (un-compilable → reject, FR-006); then a **heuristic** catastrophic-backtracking screen — reject patterns with nested unbounded quantifiers on a group (`(…+)+`, `(…*)*`, `(…+)*`, `(…*)+`), quantified groups containing an inner unbounded quantifier, or overlapping alternation under a `+`/`*` (e.g. `(a|a)+`). Screen depth is heuristic (plan-level, FR-006 "obvious catastrophic risk"), documented as best-effort — not a proof of safety.
- **Cookies** (`reject_session_cookies`): reject any cookie whose name matches the auth/session **deny heuristic** — an explicit deny-list of known names (`session`, `sessionid`, `sid`, `sess`, `phpsessid`, `jsessionid`, `asp.net_sessionid`, `connect.sid`, `auth`, `authorization`, `token`, `access_token`, `refresh_token`, `jwt`, `csrf`, `xsrf`, `remember`, `remember_me`, `login`, `logged_in`, `user`, `uid`, `account`) plus a case-insensitive substring screen for `session`/`auth`/`token`/`sid`/`csrf`/`xsrf`/`login`. Accept technical cookies (currency/locale/`cur`/`lang`/`locale`/`country`) (FR-007, §30). Heuristic name list is plan-level.
- **`validation_rules`** (`validate_validation_rules`): `required_currency` if present must be a 3-letter alpha code (uppercased); `min_price`/`max_price` if present go through `app_shared.money.parse_money` (Decimal, finite, scale ≤ 4, **non-negative**), with `min_price ≤ max_price`; `reject_if_text_contains`/`prefer_text_contains` if present must be lists of strings (FR-008/FR-022).
- **`confidence_rules`** (`validate_confidence_rules`): every present numeric value (per-method, minimum, promotion) must be a real number in `[0,1]` (FR-009).
- **Money reuse**: extract a pure `parse_money(value) -> Decimal` from `app_shared/money.py` (the existing `Money.process_bind_param` finite/scale/no-float logic, plus a non-negative check for price bounds) and call it from both the `Money` type and the validator, so §19 semantics have one implementation.

Bulk-upsert (`app_shared/profiles/upsert.py`): `build_profiles_upsert(rows)` → one `pg_insert(ScrapeProfile).values([...]).on_conflict_do_update(index_elements=["workspace_id","name"], index_where=text("workspace_id IS NOT NULL"), set_={updatable cols + updated_at=func.now()})`; `prepare_profiles(rows)` applies the validators per row and splits `(valid, rejected)` keyed by `(workspace_id, name)` (reject-and-report, FR-020); `dedup_last_wins` reused from `app_shared.catalog.upsert`.

**Rationale.** All correctness-critical checks are pure and unit-testable without a DB (the security surface: regex safety, cookie legality, money §19, confidence bounds). The bulk arbiter is the tenant partial unique (D3), so a single set-based statement covers the whole valid batch (SC-008) — and because it's tenant-only (`workspace_id` = caller, never NULL), it can never touch a global row. Extracting `parse_money` avoids a second, drifting money implementation (Principle VII).

**Alternatives considered.** A full ReDoS analyzer (e.g. automaton simulation) — rejected as beyond "obvious catastrophic risk"; the heuristic rejects the classic exponential shapes and is documented as best-effort. Validating money by hand in the validator — rejected (would drift from `Money`); shared `parse_money` instead.

---

## D10 — Scopes, assignment endpoints, and API surface

**Decision.**
- Add `SCRAPE_PROFILES_READ = "scrape_profiles:read"` and `SCRAPE_PROFILES_WRITE = "scrape_profiles:write"` to `app_shared.security.scopes.Scope`.
- CRUD + bulk per §24 + FR-020: `POST/GET/GET{id}/PATCH/DELETE /v1/scrape-profiles` and `POST /v1/scrape-profiles/bulk-upsert`.
- **Assignment**: competitor and match assignment reuse their **existing** `PATCH`/`POST`/`bulk` endpoints (they already accept `default_scrape_profile_id`/`scrape_profile_id`); this spec adds an `assert_profile_assignable` call there (FR-013). The **workspace default** has no existing endpoint, so add one small endpoint on the profiles router: `PUT /v1/scrape-profiles/workspace-default` `{profile_id: uuid|null}` — sets `workspaces.default_scrape_profile_id` for the caller's workspace after the visibility check; `null` clears it.

**Rationale.** §22's scope list predates this spec but §24 lists the `/v1/scrape-profiles` endpoints; the two-scope extension mirrors the exact per-entity precedent (`competitors:read/write`, `matches:read/write`). Assignment lives naturally on the referring entity's PATCH (already wired); only the workspace-level default lacks a home, so one narrow assignment endpoint is added rather than inventing a broad workspace-settings CRUD. `bulk-upsert` is not in §24's endpoint list but is mandated by FR-020 and follows the established products/matches bulk precedent.

**Alternatives considered.** Dedicated `/assign` endpoints per level — rejected as redundant with the existing competitor/match PATCH. A general workspace-update endpoint — rejected as out-of-scope (this spec only needs the default-profile pointer).

---

## D11 — Global-profile seeding (out-of-band)

**Decision.** This spec does **not** auto-seed any global (`workspace_id IS NULL`) profile. Global defaults, including the reserved `"global_default"` row, are created/managed via a privileged platform path (a superuser/`BYPASSRLS` connection or the migration/`seed_bootstrap` role), never via the tenant API. Resolution treats an absent global default as `NONE_RESOLVED` (FR-016).

**Rationale.** FORCE RLS + the write policy (D4) deliberately block global inserts from the tenant path; a global row can only be written by a connection that bypasses RLS. Seeding is therefore a documented platform operation, and its absence is a valid resolved state (terminal `NONE_RESOLVED`). The spec's assumption explicitly leaves the seeding mechanism plan-level, and not-seeding keeps the migration side-effect-free and re-runnable.

**Alternatives considered.** Seed a `"global_default"` row in the migration — deferred: the migration role's RLS posture is environment-dependent, and seeding config content is a platform/ops concern, not a schema migration; documented as an out-of-band step in quickstart.

---

## D12 — Migration head & shape

**Decision.** One migration `<rev>_scrape_profiles_table.py`, `down_revision = 'f4c8a391d5c9'` (verified current single head: `023a24e5717d → 55da7d6d939d → c2987b29555e → f4c8a391d5c9`). `upgrade()`: `create_table("scrape_profiles", ...)` with the §22 columns, PK, two partial unique indexes, the `workspace_id` index, and the nullable FK → `workspaces.id`; then `emit_global_readable_rls_policy("scrape_profiles")` executed statement-by-statement; then three `op.create_foreign_key(..., ondelete="SET NULL")` promoting `competitors.default_scrape_profile_id`, `competitor_product_matches.scrape_profile_id`, and `workspaces.default_scrape_profile_id`. `downgrade()`: drop the three FKs, then `drop_table("scrape_profiles")`. Hand-authored (no live Postgres for autogenerate), column/constraint shapes reproduce `app_shared.models.scrape_profiles` exactly. `scripts/check_single_head.sh` stays green (one head).

**Rationale.** Matches the SPEC-04/05 migration convention (RLS in the same migration that creates the workspace-owned table; hand-authored; single linear history). The FK promotions are `ALTER`s because the columns already exist (SPEC-03/05).

**Alternatives considered.** Two migrations (table, then FKs) — rejected: one atomic migration keeps the head linear and the change reviewable in one place.

---

## D13 — Unit-vs-live test split (no Docker daemon)

**Decision.** Unit-test here (DB/Redis-independent): model/partial-unique/index shapes + ≤63-byte names + documented defaults + enums; `emit_global_readable_rls_policy` DDL render; the full validator corpus (enums, regex compile+ReDoS, cookie technical-accept/session-reject, `validation_rules` currency/money-finite-scale-nonneg/min≤max/text-lists, `confidence_rules` [0,1]); `parse_money`; confidence-default merge; resolution chain ordering across all precedence combos + visibility fall-through + domain-strategy no-op + `NONE_RESOLVED` + grouping + cache-key determinism; bulk-upsert compile-to-SQL (one statement, partial-index arbiter) + last-wins + reject/valid split; dual-scope repository predicates; scope-gating wiring; migration offline `--sql` render (table + partial uniques + custom RLS + 3 `ON DELETE SET NULL` FKs); single head. Author-and-mark for a live PG+Redis host: real CRUD + round-trip, RLS row denial incl. **global read by all** and **global write-block via tenant path**, cross-workspace assignment rejection, `ON DELETE SET NULL` cascade, Redis TTL/invalidation, batch-resolution end-to-end (grouped, no per-match N+1), migration online run.

**Rationale.** Mirrors the SPEC-03/04/05 split exactly under the project's no-Docker-daemon constraint. Every security- and correctness-critical behavior (isolation predicates, money, regex/cookie safety, resolution precedence) is provable without a live stack; only actual row-level RLS enforcement and Redis timing need a host.

**Alternatives considered.** None — this is the established project constraint.
