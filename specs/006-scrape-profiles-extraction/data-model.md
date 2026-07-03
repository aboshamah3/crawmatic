# Phase 1 Data Model: Scrape Profiles & Extraction Rules

Source of truth: `PROJECT_SPEC.md` §22 (`scrape_profiles` columns + modes/adapter-keys), §20 (variant strategies), §17 (confidence defaults), §18 (`validation_rules` shape), §19 (money), §9 (resolution chain + global default), §32 (RLS). Built on SPEC-02 (`Base`, `TimestampMixin`, `TZDateTime`, `enum_column`, `Money`/`parse_money`, `NAMING_CONVENTION`), SPEC-03 (`set_workspace_context`, scopes), SPEC-04 (partial-unique-index + `ON CONFLICT` inference, keyset pagination, `dedup_last_wins`), SPEC-05 (the three assignment columns; explicit ≤63-byte names).

`scrape_profiles` is the project's first **dual-scope** table (research D2): workspace rows (`workspace_id NOT NULL`) are RLS-isolated; global rows (`workspace_id IS NULL`) are read-shared and tenant-unwritable. It therefore uses `Base + TimestampMixin` (NOT `WorkspaceScopedBase`), is NOT in `WORKSPACE_OWNED_MODELS`, and gets the custom `emit_global_readable_rls_policy` (research D4).

---

## Enums (`app_shared/enums.py`, extend)

| Enum | Values | Column |
|------|--------|--------|
| `ScrapeProfileMode` | `HTTP`, `BROWSER`, `CUSTOM` | `scrape_profiles.mode` |
| `AdapterKey` | `default_http`, `jsonld_first`, `selector_only`, `regex_only`, `shopify_product_json`, `woocommerce_store_api`, `playwright_rendered`, `custom_adapter` | `scrape_profiles.adapter_key` |
| `VariantStrategy` | `PAGE_SINGLE_PRICE`, `URL_HAS_VARIANT_SELECTED`, `HTML_VARIANT_TABLE`, `EMBEDDED_JSON_VARIANTS`, `SELECT_VARIANT_WITH_PLAYWRIGHT`, `CUSTOM_VARIANT_ADAPTER` | `scrape_profiles.variant_strategy` |

All `StrEnum`, string-backed via `enum_column(...)` → `VARCHAR(32)` (never PG-native). Tokens verbatim from §22/§20 (adapter keys lowercase; modes/strategies uppercase — research D1).

New scope tokens (`app_shared/security/scopes.py`): `SCRAPE_PROFILES_READ = "scrape_profiles:read"`, `SCRAPE_PROFILES_WRITE = "scrape_profiles:write"`.

---

## Entity: ScrapeProfile (`scrape_profiles`)

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | `Uuid` (UUIDv7 PK) | no | from `Base` |
| `workspace_id` | `Uuid` | **yes** | indexed; nullable FK → `workspaces.id`; **NULL = global default** |
| `name` | `Text` | no | unique per scope (two partial uniques below) |
| `mode` | `VARCHAR(32)` (`ScrapeProfileMode`) | no | default `HTTP` |
| `adapter_key` | `VARCHAR(32)` (`AdapterKey`) | no | default `default_http` |
| `jsonld_enabled` | `Boolean` | no | default `True` |
| `platform_patterns_enabled` | `Boolean` | no | default `True` |
| `embedded_json_enabled` | `Boolean` | no | default `True` |
| `price_selector` | `Text` | yes | — |
| `price_xpath` | `Text` | yes | — |
| `price_regex` | `Text` | yes | compiled + ReDoS-screened at write (FR-006) |
| `old_price_selector` | `Text` | yes | — |
| `old_price_xpath` | `Text` | yes | — |
| `old_price_regex` | `Text` | yes | compiled + ReDoS-screened |
| `currency_selector` | `Text` | yes | — |
| `currency_xpath` | `Text` | yes | — |
| `currency_regex` | `Text` | yes | compiled + ReDoS-screened |
| `stock_selector` | `Text` | yes | — |
| `stock_xpath` | `Text` | yes | — |
| `stock_regex` | `Text` | yes | compiled + ReDoS-screened |
| `title_selector` | `Text` | yes | — |
| `title_xpath` | `Text` | yes | — |
| `variant_strategy` | `VARCHAR(32)` (`VariantStrategy`) | no | default `PAGE_SINGLE_PRICE` |
| `variant_selector_config` | `JSONB` | yes | opaque shape (round-trip fidelity) |
| `price_transform_rules` | `JSONB` | yes | opaque shape (round-trip fidelity) |
| `validation_rules` | `JSONB` | yes | shape-validated (FR-008/FR-022) |
| `confidence_rules` | `JSONB` | yes | shape-validated `[0,1]` (FR-009) |
| `wait_for_selector` | `Text` | yes | — |
| `request_timeout_ms` | `Integer` | no | default `30000` (documented) |
| `browser_timeout_ms` | `Integer` | yes | — |
| `headers` | `JSONB` | yes | round-trip fidelity |
| `cookies` | `JSONB` | yes | technical-only; session/auth rejected (FR-007) |
| `created_at` / `updated_at` | `TIMESTAMPTZ` | no | from `TimestampMixin` |

**Documented defaults (FR-002).** `mode=HTTP`, `adapter_key=default_http`, all three `*_enabled=True`, `variant_strategy=PAGE_SINGLE_PRICE`, `request_timeout_ms=30000`; every nullable extraction/JSON/timeout field defaults to NULL. A profile with a mode/adapter but **no** selectors/regex is valid (Edge Case: "All extraction fields empty") — later extraction may rely on JSON-LD/platform patterns alone.

**Constraints / indexes** (all auto-named by `NAMING_CONVENTION`; all ≤38 chars, well under the 63-byte cap — no explicit shortening needed)
- **Partial unique** `uq_scrape_profiles_workspace_id_name` on `(workspace_id, name)` `WHERE workspace_id IS NOT NULL` — per-tenant name uniqueness (FR-003), and the bulk-upsert conflict arbiter (research D3/D9).
- **Partial unique** `uq_scrape_profiles_name_global` on `(name)` `WHERE workspace_id IS NULL` — single global namespace uniqueness.
- `ix_scrape_profiles_workspace_id` — the `WorkspaceScopedBase`-style index, declared explicitly here since we don't inherit the mixin.
- FK `workspace_id → workspaces.id` (nullable — a NULL passes the FK; a global row references no workspace).

**Not in `WORKSPACE_OWNED_MODELS`** (research D2): dual-scope. All queries go through `app_shared.profiles.repository` (dual-scope helpers), never `scoped_select`/`scoped_get`.

---

## Assignment references — promoted to FKs (research D5, FR-012/FR-023)

Three **existing** nullable columns (created by SPEC-03/05) are promoted to **plain** FKs → `scrape_profiles(id)` `ON DELETE SET NULL` via `ALTER TABLE` in this spec's migration. Plain (not composite) because a global (`workspace_id IS NULL`) profile must be assignable by any workspace — a workspace-local composite FK would forbid that.

| Table.column | New FK | On delete |
|--------------|--------|-----------|
| `competitors.default_scrape_profile_id` | `→ scrape_profiles(id)` | `SET NULL` |
| `workspaces.default_scrape_profile_id` | `→ scrape_profiles(id)` | `SET NULL` |
| `competitor_product_matches.scrape_profile_id` | `→ scrape_profiles(id)` | `SET NULL` |

`SET NULL` guarantees no assignment ever dangles after a profile delete (FR-023); cross-workspace assignment (which a plain FK cannot block) is rejected at write by `assert_profile_assignable` (FR-013) and ignored at resolution (FR-017). The `default_access_policy_id`/`access_policy_id` columns are untouched (SPEC-10).

---

## Isolation & RLS summary (§32, Principle II — dual-scope)

| Table | Scope | RLS | In `WORKSPACE_OWNED_MODELS` |
|-------|-------|-----|-----------------------------|
| `scrape_profiles` | dual (workspace rows + global `NULL` rows) | **yes** — `emit_global_readable_rls_policy` in the creating migration | **no** (dual-scope; uses `app_shared.profiles.repository`) |

RLS policy pair (research D4):
- `scrape_profiles_workspace_read` — `FOR SELECT USING (workspace_id IS NULL OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)` → every workspace reads its own rows **and** all global rows; fail-closed on own rows when no context (but global rows still visible via the `IS NULL` disjunct).
- `scrape_profiles_workspace_write` — `FOR ALL USING (workspace_id = <ctx>) WITH CHECK (workspace_id = <ctx>)` → a tenant can INSERT/UPDATE/DELETE only its own rows; it can never write a global (`NULL`) row (FR-021).

Application layer (`app_shared.profiles.repository`): reads via `visible_profiles_select(ws)` (own OR global); management via `owned_profile_select(ws)`/`owned_profile_get(...)` (own only → a global or other-workspace id yields "not found" on the tenant write path). Two-layer model preserved: app filter + DB RLS, either alone fails safe.

---

## Transient: ResolvedProfile (not persisted)

The output of the resolution chain for a match or a `(competitor_id, url_pattern)` group (research D6): either a specific `ScrapeProfile` id (with the precedence level that supplied it) or the explicit `NONE_RESOLVED` sentinel. Never an error, never an arbitrary row. Chain (§9): match override → domain-strategy (no-op `None`, FR-015) → competitor default → workspace default → global default (`name == "global_default"`, `workspace_id IS NULL`). Each candidate is kept only if visible (own or global); dangling/cross-workspace ids fall through (FR-017). Group results are Redis-cached keyed by `(workspace_id, competitor_id, url_pattern)` with short TTL (research D7); the per-match override is applied after the cache lookup.

---

## Confidence defaults (transient config accessor — §17, FR-011)

`app_shared/profiles/confidence.py`: `DEFAULT_CONFIDENCE_RULES` (platform_variant_json 0.95, jsonld 0.95, embedded_json 0.90, css 0.85, xpath 0.85, regex 0.75, playwright 0.80, single_number 0.40), `DEFAULT_MIN_ACCEPTED_CONFIDENCE = 0.75`, `DEFAULT_PROMOTION_THRESHOLD = 0.85`; `resolve_confidence_rules(profile.confidence_rules)` overlays a profile's validated overrides over the defaults. DB-tunable via `confidence_rules`, never hardcoded in the extractor.

---

## Invariants & state

- **Dual scope (FR-001/FR-021).** `workspace_id` nullable; NULL = global. Tenant path reads own+global, writes own-only (RLS + repository).
- **Name uniqueness (FR-003).** Per-tenant (partial unique WHERE ws IS NOT NULL) and single-global (partial unique WHERE ws IS NULL).
- **Write-time validation (FR-005/006/007/008/009/022).** Enums, regex compile+ReDoS, cookie session/auth deny, `validation_rules` currency/money(§19)/text-lists, `confidence_rules` [0,1] — all reject before persistence; valid JSON bundles round-trip byte-identical (FR-010).
- **Assignment integrity (FR-012/013/017/023).** Plain FKs `ON DELETE SET NULL`; cross-workspace rejected at write, ignored at resolution; delete never dangles.
- **Resolution (FR-014/015/016/018/019).** Precedence-ordered, domain-strategy no-op, explicit none-resolved, grouped-per-(competitor_id,url_pattern), Redis-cached short TTL.
- **Bulk set-based (FR-020, SC-008).** One `ON CONFLICT (workspace_id, name) WHERE workspace_id IS NOT NULL DO UPDATE` for all valid tenant rows; invalid rows reject-and-reported; never writes global.
- **Deletion (FR-023).** Tenant hard-delete of an own profile; `SET NULL` clears every reference; global rows undeletable via tenant path.
