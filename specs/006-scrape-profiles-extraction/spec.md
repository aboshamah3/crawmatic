# Feature Specification: Scrape Profiles & Extraction Rules

**Feature Branch**: `006-scrape-profiles-extraction`

**Created**: 2026-07-03

**Status**: Draft

**Input**: SPEC-06 from PROJECT_SPEC.md §35 — define DB-driven extraction configuration (`scrape_profiles`), the selectors/XPath/regex/JSON-LD/embedded-JSON/variant/validation/confidence rules, and a config-resolution service that returns the final scrape profile for a match. No real scraping execution in this spec.

## Clarifications

### Session 2026-07-03

All items below were resolved from the master specification (`PROJECT_SPEC.md` §9/§16/§17/§18/§19/§20/§22/§24/§30/§32) and the SPEC-01..05 foundation; no open ambiguity required a stakeholder decision. Plan-level details are noted as such.

- Q: What is the scrape-profile resolution order? → A: Match-level override (`competitor_product_matches.scrape_profile_id`) → domain strategy profile preferred extraction method → competitor default (`competitors.default_scrape_profile_id`) → workspace default (`workspaces.default_scrape_profile_id`) → global default (a `scrape_profiles` row with `workspace_id IS NULL`) (source: §9 "Scrape profile resolution"). The domain-strategy link's backing table (`domain_strategy_profiles`) is built in SPEC-12; in this spec that chain step is an optional lookup that is a no-op when the table/row is absent, and the chain proceeds to the competitor default (source: §14/§35 scoping).
- Q: Are the assignment reference columns already present? → A: Yes — `workspaces.default_scrape_profile_id`, `competitors.default_scrape_profile_id`, and `competitor_product_matches.scrape_profile_id` were created as plain nullable columns by SPEC-03/05. This spec supplies the `scrape_profiles` table they reference; whether to promote them to real FKs (workspace-consistent composite FK) or leave them soft is a plan-level decision, but resolution MUST treat a dangling/cross-workspace reference as "not set" and fall through (source: §22, SPEC-05 clarifications).
- Q: How is the resolved config cached and de-N+1'd? → A: Resolution MUST batch per `(competitor_id, url_pattern)` for a whole refresh batch (not per match) and cache the resolved profile in Redis with a short TTL keyed by `(workspace_id, competitor_id, url_pattern)`; invalidate on writes to any profile row that could change a resolution, or rely on the short TTL. Exact TTL value and key-serialization are plan-level (source: §9 "Resolution caching").
- Q: What does "return the final profile" mean when nothing is assigned anywhere? → A: The global default (`workspace_id IS NULL`) profile is the terminal fallback; if no global default exists either, resolution returns an explicit "no profile resolved" result (not an error row) so callers can decide (source: §9, §16 "Failed: PRICE_NOT_FOUND" is a later-spec concern, not here).
- Q: Is any extraction/scraping executed here? → A: No. This spec only stores and reads config and resolves which profile applies. Selector/XPath/regex/JSON-LD/embedded-JSON values are stored as opaque, syntactically-validated strings/flags; actually running them against HTML is SPEC-07+ (source: §35 "No real scraping execution yet").
- Q: What validation applies to `validation_rules`/`confidence_rules`/`variant_selector_config`/`price_transform_rules` JSON? → A: They are validated for JSON shape and value sanity (e.g. `min_price`/`max_price` are non-negative finite Decimals with `min_price ≤ max_price`; `required_currency` a 3-letter code; confidence values in `[0,1]`; string lists are lists of strings), but their runtime semantics are applied by the extractor in later specs. Money bounds follow §19 (Decimal, finite, scale ≤ 4) (source: §17/§18/§19).
- Q: Cookie guardrail? → A: `cookies` may carry only non-identifying technical cookies (currency/locale); session/authentication cookies are rejected by validation at write time. The concrete deny heuristic (known auth/session cookie-name patterns) is a plan-level constant (source: §30 legal guardrails, §22 config guardrails).
- Q: Regex safety? → A: Stored `*_regex` values MUST compile and SHOULD be screened for catastrophic-backtracking risk before being accepted, since later specs run them against untrusted HTML; rejecting an un-compilable or obviously catastrophic pattern at write time is in scope, the deep ReDoS analysis depth is plan-level (source: §16 regex, §7 correctness).
- Q: Bulk create/upsert of profiles? → A: Support the project's set-based reject-and-report bulk-upsert pattern (as catalog/matches) keyed by `(workspace_id, name)`: valid rows upserted as a set, invalid rows rejected and reported, never aborting the whole batch (source: §24, SPEC-04/05 precedent).
- Q: Workspace isolation / global rows? → A: Profiles with `workspace_id` are workspace-owned and RLS-isolated exactly like prior entities; profiles with `workspace_id IS NULL` are read-only shared/global defaults readable by all workspaces but only manageable by a platform/global path (not via a tenant's workspace-scoped writes) (source: §32, §9 global default).
- Q: Live-Postgres/Redis acceptance items here? → A: DB/Redis-independent logic (model/RLS DDL render, profile validators incl. cookie/regex/money/JSON-shape checks, resolution-chain ordering over in-memory inputs, batch-grouping, cache key derivation, bulk-upsert construction, pagination, scope-gating) is unit-tested here; live CRUD, RLS row denial, cross-workspace, Redis TTL/invalidation, migration run, and e2e are deferred to a live PG/Redis host (source: no-docker-daemon constraint, prior specs).

## User Scenarios & Testing *(mandatory)*

The users of this feature are workspace operators and their integrations who configure **how** a competitor page should be read for a price — which extraction methods to try, which selectors/XPath/regex to use, how to validate the extracted price, and how confident an extraction must be to be trusted. This is the database-driven configuration layer described by "the code is the engine; the database controls config" (§9). Nothing here scrapes a live page; it defines and resolves the configuration that later spiders (SPEC-07+) will read. A key deliverable is a **resolution service** that, given a match, returns the single scrape profile that actually applies to it after walking the match → competitor → workspace → global override chain.

### User Story 1 - Create and manage scrape profiles (Priority: P1)

An operator creates a scrape profile that captures a reusable extraction recipe: a mode (HTTP/BROWSER/CUSTOM), an adapter key, which structured-data methods are enabled (JSON-LD, platform patterns, embedded JSON), the price/old-price/currency/stock/title selectors/XPath/regex, a variant strategy, and the validation/confidence/transform rule bundles. The profile is workspace-scoped and can be read, updated, listed, and deleted.

**Why this priority**: The `scrape_profiles` table is the parent entity everything else in this spec references; without it there is nothing to assign or resolve.

**Independent Test**: Create a profile with a name, mode, adapter key, some selectors, and a `validation_rules` bundle → it is stored workspace-scoped with sane defaults for unset fields; read/update/list (paginated)/delete work and are workspace-isolated; a second profile with the same `(workspace_id, name)` is rejected as a duplicate.

**Acceptance Scenarios**:

1. **Given** a workspace, **When** a scrape profile is created with a name, a mode, an adapter key, and one or more extraction fields, **Then** it is stored scoped to the workspace with the provided fields and documented defaults for the rest (timeouts, the three `*_enabled` booleans, variant strategy).
2. **Given** an existing profile name in a workspace, **When** another profile with the same name is created, **Then** it is rejected (name is unique per workspace).
3. **Given** a stored profile, **When** it is read, updated, listed, or deleted, **Then** the operation reflects workspace-scoped state and cannot see or touch another workspace's profiles.
4. **Given** a profile payload with an invalid enum (mode/adapter_key/variant_strategy), an un-compilable regex, a session/auth cookie, or a malformed rules bundle, **When** it is saved, **Then** it is rejected at save time with a clear, field-specific validation error and not stored.

### User Story 2 - Assign a profile to a competitor or a match (Priority: P1)

An operator assigns a scrape profile as the default for a competitor, or overrides it on a specific match, or sets a workspace-wide default — so that a page can be read with the most specific applicable recipe.

**Why this priority**: Assignment is what makes resolution meaningful; the override levels are the inputs to the resolution chain.

**Independent Test**: Set `competitor_product_matches.scrape_profile_id`, `competitors.default_scrape_profile_id`, and `workspaces.default_scrape_profile_id` to existing profiles → each is accepted only if the referenced profile is visible to that workspace (own workspace or a global profile); pointing at another workspace's profile is rejected; clearing a reference (null) is accepted.

**Acceptance Scenarios**:

1. **Given** a profile and a match in the same workspace, **When** the match's `scrape_profile_id` is set to that profile, **Then** the assignment is stored and the match now overrides at the highest precedence.
2. **Given** a profile, **When** it is set as a competitor's `default_scrape_profile_id` or a workspace's `default_scrape_profile_id`, **Then** the assignment is stored at that precedence level.
3. **Given** a profile owned by workspace B, **When** workspace A tries to assign it to A's match/competitor/workspace, **Then** the assignment is rejected (cannot reference another workspace's profile); a global (`workspace_id IS NULL`) profile MAY be assigned by any workspace.
4. **Given** an assigned profile, **When** the assignment reference is cleared, **Then** resolution for the affected matches falls through to the next level.

### User Story 3 - Resolve the final profile for a match (Priority: P1)

A caller (later a refresh batch) asks "what scrape profile applies to this match?" and gets back a single resolved profile, computed by walking match-override → domain-strategy-preferred → competitor-default → workspace-default → global-default, efficiently for a whole batch of matches at once, with the result cached briefly.

**Why this priority**: This is the core deliverable and the acceptance criterion "Config resolution returns the final profile for a match"; it is the interface every later scraping spec depends on.

**Independent Test**: Given in-memory matches with various combinations of set/unset overrides at each level, resolution returns the expected profile at the correct precedence for each; a batch of many matches sharing a `(competitor_id, url_pattern)` is resolved with one grouped lookup, not one per match; a repeated resolution is served from the cache; a write that changes a relevant profile invalidates (or is superseded by the TTL of) the cached entry.

**Acceptance Scenarios**:

1. **Given** a match with its own `scrape_profile_id` set, **When** resolution runs, **Then** that profile is returned regardless of competitor/workspace/global defaults.
2. **Given** a match with no override but whose competitor has a default, **When** resolution runs, **Then** the competitor default is returned; with no competitor default, the workspace default; with none of those, the global default.
3. **Given** a match where none of match/competitor/workspace/global provides a profile, **When** resolution runs, **Then** an explicit "no profile resolved" result is returned (not an error, not an arbitrary row).
4. **Given** a batch of matches grouped by `(competitor_id, url_pattern)`, **When** resolution runs for the batch, **Then** each distinct group is resolved once (no per-match N+1) and each match receives its group's resolved profile.
5. **Given** a previously resolved `(workspace_id, competitor_id, url_pattern)`, **When** it is resolved again within the TTL, **Then** the cached profile is returned without re-walking the chain; **When** a relevant profile row is written, **Then** the cache entry is invalidated or expires so the next resolution reflects the change.
6. **Given** a domain-strategy step whose backing table does not yet exist, **When** resolution runs, **Then** that step is skipped cleanly and the chain proceeds to the competitor default.

### User Story 4 - Store and read validation & confidence rules (Priority: P2)

An operator stores validation rules (required currency, min/max price, reject/prefer text lists) and confidence rules (per-method default confidences, minimum accepted confidence, promotion threshold) on a profile, and reads them back exactly as stored, so later extraction can apply them as tunable DB config rather than hardcoded constants.

**Why this priority**: Required by the acceptance criterion "Validation/confidence rules are stored and readable," but depends on the profile existing (US1), so it is P2.

**Independent Test**: Store a `validation_rules` bundle and a `confidence_rules` bundle → they are persisted and read back byte-for-byte after schema validation; invalid bundles (e.g. `min_price` > `max_price`, confidence outside `[0,1]`, non-list text field, over-scale money) are rejected.

**Acceptance Scenarios**:

1. **Given** a profile, **When** a `validation_rules` bundle (`required_currency`, `min_price`, `max_price`, `reject_if_text_contains`, `prefer_text_contains`) is stored, **Then** it is persisted and returned identically on read.
2. **Given** a profile, **When** a `confidence_rules` bundle (per-method confidences, minimum accepted confidence, promotion threshold) is stored, **Then** it is persisted and returned identically on read, and unspecified values fall back to the documented defaults when read through the resolution/accessor.
3. **Given** a rules bundle violating a constraint (min > max, confidence outside `[0,1]`, non-finite/over-scale money, currency not a 3-letter code, text fields not lists of strings), **When** it is stored, **Then** it is rejected with a clear error.

### Edge Cases

- **Global vs workspace profile management**: a tenant's workspace-scoped write path MUST NOT create, edit, or delete a `workspace_id IS NULL` global profile; global profiles are readable by all but managed only via a platform/global path.
- **Dangling/cross-workspace assignment**: if an assignment column references a deleted or another-workspace profile, resolution treats it as unset and falls through rather than erroring or leaking the foreign row.
- **Deleting an assigned profile**: deletion is blocked while the profile is referenced, or the references are nulled/fall-through on delete — the chosen policy MUST leave no assignment pointing at a nonexistent profile (plan-level choice, but resolution must remain safe either way).
- **All extraction fields empty**: a profile with a mode/adapter but no selectors/regex is valid config (later extraction may rely solely on JSON-LD/platform patterns); it is not rejected.
- **Un-compilable or catastrophic regex** in any `*_regex` field is rejected at write time.
- **Session/auth cookie** in `cookies` is rejected; a currency/locale cookie is accepted.
- **Over-scale or non-finite money** (`min_price`/`max_price` with > 4 decimals, `NaN`, `Infinity`) is rejected, never silently rounded (§19).
- **Cache staleness bound**: a stale cached resolution is acceptable only up to the TTL; a profile write that changes resolution MUST be reflected within TTL (or immediately, if invalidation is implemented).
- **Bulk-upsert partial failure**: in a bulk create/upsert, invalid rows are rejected and reported while valid rows are upserted; the batch is never aborted wholesale.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST provide a `scrape_profiles` entity carrying, at minimum: `id` (UUIDv7), nullable `workspace_id` (NULL = global/shared default), `name`, `mode` (HTTP|BROWSER|CUSTOM), `adapter_key` (one of the eight defined adapter keys), the booleans `jsonld_enabled`/`platform_patterns_enabled`/`embedded_json_enabled`, the nullable extraction fields (`price_selector`/`price_xpath`/`price_regex`, `old_price_*`, `currency_*`, `stock_*`, `title_selector`/`title_xpath`), `variant_strategy` (one of the six defined strategies), nullable JSON `variant_selector_config`/`price_transform_rules`/`validation_rules`/`confidence_rules`/`headers`/`cookies`, `wait_for_selector` nullable, `request_timeout_ms`, nullable `browser_timeout_ms`, and `created_at`/`updated_at`.
- **FR-002**: System MUST allow creating a scrape profile and MUST persist all provided fields, applying documented defaults for fields not supplied (the three `*_enabled` booleans, `variant_strategy`, `request_timeout_ms`, mode/adapter where a sensible default exists).
- **FR-003**: System MUST enforce that profile `name` is unique per workspace (per `(workspace_id, name)`), rejecting duplicates.
- **FR-004**: System MUST allow reading, updating, listing (paginated), and deleting scrape profiles, with every operation workspace-scoped so no workspace can read or mutate another workspace's profiles (RLS + workspace-scoped query helpers, per Principle II).
- **FR-005**: System MUST validate `mode`, `adapter_key`, and `variant_strategy` against their allowed enum values and reject invalid values at write time.
- **FR-006**: System MUST validate each `*_regex` value by compiling it, rejecting patterns that fail to compile, and SHOULD reject patterns exhibiting obvious catastrophic-backtracking risk (they will later run against untrusted HTML).
- **FR-007**: System MUST reject any `cookies` entry that is a session or authentication cookie, accepting only non-identifying technical cookies (e.g. currency/locale), per the legal/config guardrails.
- **FR-008**: System MUST validate the `validation_rules` bundle: `required_currency` (if present) is a valid 3-letter currency code; `min_price`/`max_price` (if present) are finite non-negative Decimals with scale ≤ 4 and `min_price ≤ max_price`; `reject_if_text_contains`/`prefer_text_contains` (if present) are lists of strings. Invalid bundles are rejected.
- **FR-009**: System MUST validate the `confidence_rules` bundle: any per-method confidence, minimum-accepted-confidence, and promotion-threshold values present are numbers in `[0,1]`. Invalid bundles are rejected.
- **FR-010**: System MUST store `validation_rules` and `confidence_rules` (and `variant_selector_config`, `price_transform_rules`, `headers`) such that they are read back exactly as stored (round-trip fidelity) after validation.
- **FR-011**: System MUST expose the documented default confidence values (Platform variant JSON 0.95, JSON-LD 0.95, embedded JSON 0.90, CSS 0.85, XPath 0.85, regex 0.75, Playwright 0.80, single bare number 0.40 — which is below the default minimum, so effectively rejected), a default minimum accepted confidence of 0.75, and a default promotion threshold of 0.85, used when a profile's `confidence_rules` does not override them — as DB-tunable config, not hardcoded literals baked into the extractor.
- **FR-012**: System MUST let a scrape profile be assigned as a match override (`competitor_product_matches.scrape_profile_id`), a competitor default (`competitors.default_scrape_profile_id`), or a workspace default (`workspaces.default_scrape_profile_id`), providing the target `scrape_profiles` table these existing nullable columns reference.
- **FR-013**: System MUST reject an assignment that references a profile not visible to the assigning workspace (another workspace's profile), while permitting assignment of a global (`workspace_id IS NULL`) profile by any workspace; clearing an assignment (null) MUST be permitted.
- **FR-014**: System MUST provide a config-resolution capability that, given a match, returns the single resolved scrape profile by walking, in order: match override → domain-strategy preferred extraction method → competitor default → workspace default → global default.
- **FR-015**: System MUST treat the domain-strategy step as optional: when its backing table/row is absent (SPEC-12 not yet built), the step is skipped cleanly and resolution proceeds to the competitor default.
- **FR-016**: System MUST return an explicit "no profile resolved" result (not an error, not an arbitrary row) when no level in the chain — including the global default — supplies a profile.
- **FR-017**: System MUST treat a dangling or cross-workspace assignment reference as unset during resolution, falling through to the next level rather than erroring or returning the foreign profile.
- **FR-018**: System MUST resolve configuration for a whole batch of matches by grouping on `(competitor_id, url_pattern)` and resolving each distinct group once, avoiding per-match N+1 database access at 10,000–20,000 matches per refresh.
- **FR-019**: System MUST cache the resolved profile in Redis with a short TTL keyed by `(workspace_id, competitor_id, url_pattern)`, serve subsequent resolutions from the cache within the TTL, and invalidate (or rely on TTL expiry for) cache entries when a profile row that could change the resolution is written.
- **FR-020**: System MUST support set-based bulk create/upsert of profiles keyed by `(workspace_id, name)` using the project's reject-and-report policy: valid rows are upserted as a set, invalid rows are rejected and reported, and the batch is never aborted wholesale.
- **FR-021**: System MUST prevent a tenant's workspace-scoped write path from creating, editing, or deleting a global (`workspace_id IS NULL`) profile; global profiles are readable by all workspaces but managed only via a platform/global path.
- **FR-022**: System MUST enforce money correctness for any monetary value in the rules bundles: Decimal/NUMERIC semantics, reject `NaN`/`Infinity`, and reject values with more decimal places than scale 4 rather than rounding (§19). This is the *general* §19 money rule that FR-008 specializes for `validation_rules.min_price`/`max_price`; both are satisfied by the one shared money-boundary validator.
- **FR-023**: System MUST ensure profile deletion cannot leave an assignment pointing at a nonexistent profile (either block deletion while referenced or null/fall-through the references), keeping resolution safe.

### Key Entities *(include if feature involves data)*

- **ScrapeProfile**: a reusable, DB-driven extraction recipe. Owned by a workspace, or global when `workspace_id` is NULL. Holds mode/adapter, the structured-data enable flags, the selector/XPath/regex extraction fields for price/old-price/currency/stock/title, the variant strategy and its config, the validation/confidence/transform rule bundles, request/browser timeouts, headers, and technical-only cookies. Referenced (assigned) by matches, competitors, and workspaces.
- **ValidationRules** (embedded JSON on ScrapeProfile): required currency, min/max price bounds, reject-if-text-contains and prefer-text-contains lists — the rules a later extractor applies before accepting a price.
- **ConfidenceRules** (embedded JSON on ScrapeProfile): tunable per-method confidence scores, minimum accepted confidence, and promotion threshold — overriding documented defaults.
- **ResolvedProfile** (transient, not persisted): the outcome of the resolution chain for a match or a `(competitor_id, url_pattern)` group — either a specific ScrapeProfile (from whichever precedence level supplied it) or an explicit "none resolved" marker.
- **Assignment references** (existing columns, now backed by this table): `competitor_product_matches.scrape_profile_id`, `competitors.default_scrape_profile_id`, `workspaces.default_scrape_profile_id`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can create a scrape profile with extraction fields and a validation-rules bundle, then read it back with every field — including the rules bundles — identical to what was submitted.
- **SC-002**: An operator can assign a profile as a match override, a competitor default, and a workspace default, and each assignment is accepted only when the profile is visible to the workspace.
- **SC-003**: For any match, the resolution service returns exactly the profile dictated by the precedence order (match → competitor → workspace → global), and returns an explicit "none resolved" when no level supplies one — verified across all precedence combinations.
- **SC-004**: Resolving a batch of at least 10,000 matches that share a small number of `(competitor_id, url_pattern)` groups performs a number of chain walks/database lookups proportional to the number of distinct groups, not to the number of matches (no per-match N+1).
- **SC-005**: A second resolution of the same `(workspace_id, competitor_id, url_pattern)` within the TTL is served from cache, and a relevant profile write is reflected in resolution within the TTL (or immediately if invalidated).
- **SC-006**: 100% of invalid profile writes — bad enum, un-compilable regex, session/auth cookie, min_price > max_price, confidence outside `[0,1]`, non-finite or over-scale money — are rejected with a clear, field-specific error and never persisted.
- **SC-007**: No workspace can read, update, delete, or assign another workspace's scrape profile; global profiles are readable by all but not mutable through a tenant write path.
- **SC-008**: A bulk create/upsert of profiles with a mix of valid and invalid rows upserts all valid rows and reports every rejected row with its reason, without aborting the batch.

## Assumptions

- The assignment columns `workspaces.default_scrape_profile_id`, `competitors.default_scrape_profile_id`, and `competitor_product_matches.scrape_profile_id` already exist (created nullable by SPEC-03/05); this spec supplies the referenced `scrape_profiles` table and decides (at plan time) whether to promote them to workspace-consistent FKs.
- `domain_strategy_profiles` (the domain-strategy optimizer table) does not yet exist; the resolution chain's domain-strategy step is a tolerated no-op until SPEC-12 builds it.
- No live scraping, HTML parsing, or price observation occurs in this spec; stored selectors/XPath/regex/JSON are validated for syntax/shape only and executed later (SPEC-07+).
- Redis is available for resolution caching (as established for status caching in SPEC-03); DB and Redis are provisioned per prior specs. Where no live Postgres/Redis is available in the build environment, DB/Redis-dependent tests skip cleanly and the DB/Redis-independent logic is fully unit-tested here (per the project's no-Docker-daemon constraint).
- UUIDv7 app-generated IDs, Decimal-only money (NUMERIC(18,4)), RLS workspace isolation, PgBouncer-transaction-pooling compatibility, and the set-based reject-and-report bulk pattern established in SPEC-01..05 all apply unchanged.
- Global (`workspace_id IS NULL`) default profiles are seeded/managed by a platform/global path; the exact seeding mechanism is a plan-level detail.
