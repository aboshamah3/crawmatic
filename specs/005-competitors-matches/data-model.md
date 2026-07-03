# Phase 1 Data Model: Competitors & Matches

Source of truth: `PROJECT_SPEC.md` §22 (table shapes + unique constraints + enums + deletion semantics), §32 (RLS, workspace-local FKs), §21 (UUIDv7/TIMESTAMPTZ), §11 (save-time URL safety), §15 (versioned URL pattern). Built on SPEC-02 (`Base`, `WorkspaceScopedBase`, `TimestampMixin`, `TZDateTime`, `enum_column`, `emit_rls_policy`, `NAMING_CONVENTION`), SPEC-03 (`WORKSPACE_OWNED_MODELS`, scoped helpers, `set_workspace_context`), SPEC-04 (composite workspace-local FK pattern + `unique(workspace_id, id)` parents + keyset pagination + set-based upsert + consistency helper).

Both tables are **workspace-owned** → `WorkspaceScopedBase` (`workspace_id NOT NULL`, indexed), added to `WORKSPACE_OWNED_MODELS`, and get `emit_rls_policy(...)` in the creating migration.

---

## Enums (`app_shared/enums.py`, extend)

| Enum | Values | Column(s) |
|------|--------|-----------|
| `LegalStatus` | `REVIEW_REQUIRED`, `APPROVED`, `DISABLED` | `competitors.legal_status` |
| `RobotsPolicy` | `RESPECT`, `REVIEW_REQUIRED`, `IGNORE_AFTER_APPROVAL` | `competitors.robots_policy` |
| `CompetitorStatus` | `ACTIVE`, `ARCHIVED` | `competitors.status` |
| `MatchPriority` | `LOW`, `NORMAL`, `HIGH`, `CRITICAL` | `competitor_product_matches.priority` |
| `MatchStatus` | `ACTIVE`, `PAUSED`, `FAILED`, `ARCHIVED` | `competitor_product_matches.status` |
| `HealthStatus` | `HEALTHY`, `DEGRADED`, `FAILING`, `UNKNOWN` | `competitor_product_matches.health_status` |

All `StrEnum`, string-backed via `enum_column(...)` → rendered as `VARCHAR(32)` (never a PG-native enum). Uppercase tokens verbatim from §22 (research D1). `ARCHIVED` is the terminal state for FR-016 archive-by-status deletion.

---

## Entity: Competitor (`competitors`)

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | `Uuid` (UUIDv7 PK) | no | from `Base` |
| `workspace_id` | `Uuid` | no | from `WorkspaceScopedBase`, indexed; FK → `workspaces.id` |
| `name` | `Text` | no | required |
| `domain` | `Text` | no | required; unique per workspace |
| `status` | `VARCHAR(32)` (`CompetitorStatus`) | no | default `ACTIVE` |
| `legal_status` | `VARCHAR(32)` (`LegalStatus`) | no | default `REVIEW_REQUIRED` (Principle VI) |
| `robots_policy` | `VARCHAR(32)` (`RobotsPolicy`) | no | default `RESPECT` |
| `default_scrape_profile_id` | `Uuid` | yes | plain nullable ref, **no FK** (target SPEC-06) |
| `default_access_policy_id` | `Uuid` | yes | plain nullable ref, **no FK** (target SPEC-10) |
| `max_concurrent_requests` | `Integer` | yes | optional per-competitor cap |
| `max_requests_per_minute` | `Integer` | yes | optional per-competitor cap |
| `created_at` / `updated_at` | `TIMESTAMPTZ` | no | from `TimestampMixin` |

**Constraints / indexes** (all auto-named by `NAMING_CONVENTION`; all ≤38 chars, fit the 63-byte cap)
- `unique(workspace_id, id)` — enables composite-FK targeting by matches (research D4).
- `unique(workspace_id, domain)` — FR-003 (one competitor per domain per workspace).
- FK `workspace_id → workspaces.id`.

**Deletion (FR-016).** Hard-delete while no dependent history exists (true in this spec — observations/attempts are SPEC-07+); structured for archive-by-status (`status = ARCHIVED`); response indicates which occurred.

---

## Entity: CompetitorProductMatch (`competitor_product_matches`)

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | `Uuid` (UUIDv7 PK) | no | from `Base` |
| `workspace_id` | `Uuid` | no | indexed; FK → `workspaces.id` |
| `product_id` | `Uuid` | no | composite FK; **derived from the variant's parent** (not trusted independently) |
| `product_variant_id` | `Uuid` | no | composite FK; the matched sellable unit |
| `competitor_id` | `Uuid` | no | composite FK |
| `competitor_url` | `Text` | no | raw user-supplied URL (safety-validated at save time) |
| `normalized_competitor_url` | `Text` | no | canonical identity URL (part of the unique key) |
| `url_pattern` | `Text` | no | versioned grouping pattern (§15) |
| `url_pattern_version` | `Integer` | no | `URL_PATTERN_ALGORITHM_VERSION` at derivation time |
| `competitor_variant_identifier` | `Text` | yes | competitor-side variant id |
| `competitor_variant_sku` | `Text` | yes | competitor-side SKU |
| `competitor_variant_options` | `JSONB` | yes | competitor-side options |
| `external_title` | `Text` | yes | competitor-side title |
| `scrape_profile_id` | `Uuid` | yes | plain nullable ref, **no FK** (target SPEC-06) |
| `access_policy_id` | `Uuid` | yes | plain nullable ref, **no FK** (target SPEC-10) |
| `priority` | `VARCHAR(32)` (`MatchPriority`) | no | default `NORMAL` |
| `status` | `VARCHAR(32)` (`MatchStatus`) | no | default `ACTIVE` |
| `health_status` | `VARCHAR(32)` (`HealthStatus`) | no | default `UNKNOWN` (FR-017) |
| `last_error_code` | `Text` | yes | default null (populated SPEC-07+) |
| `consecutive_failures` | `Integer` | no | default `0` (FR-017) |
| `success_rate_7d` | `NUMERIC(5,4)` | yes | default null; a 0..1 statistic, **not money** (research D1) |
| `current_price_id` | `Uuid` | yes | **soft** reference, no FK (target `match_current_prices`, SPEC-07/09) |
| `last_scraped_at` | `TIMESTAMPTZ` | yes | default null |
| `last_success_at` | `TIMESTAMPTZ` | yes | default null |
| `last_failed_at` | `TIMESTAMPTZ` | yes | default null |
| `created_at` / `updated_at` | `TIMESTAMPTZ` | no | from `TimestampMixin` |

**Constraints / indexes** (explicit short names — research D5; all ≤63 bytes)
- `unique(workspace_id, product_variant_id, competitor_id, normalized_competitor_url)` — name `uq_cpm_ws_variant_competitor_norm_url` (FR-005). A variant may hold **unlimited** matches; only an exact tuple duplicate dedupes.
- **Composite FK** `(workspace_id, product_id) → products(workspace_id, id)` — name `fk_cpm_workspace_product_products`.
- **Composite FK** `(workspace_id, product_variant_id) → product_variants(workspace_id, id)` — name `fk_cpm_workspace_variant_variants`.
- **Composite FK** `(workspace_id, competitor_id) → competitors(workspace_id, id)` — name `fk_cpm_workspace_competitor_competitors`.
- FK `workspace_id → workspaces.id` — name `fk_cpm_workspace_workspaces`.
- `pk_competitor_product_matches` (auto) and `ix_competitor_product_matches_workspace_id` (auto, 42 chars) fit the cap.

All three entity FKs are **workspace-local** by construction → a cross-workspace reference is structurally impossible (research D4). `current_price_id`, `scrape_profile_id`, `access_policy_id` carry **no** FK.

**Health defaults at creation (FR-017).** `health_status=UNKNOWN`, `consecutive_failures=0`, and null `success_rate_7d` / `current_price_id` / `last_error_code` / `last_scraped_at` / `last_success_at` / `last_failed_at`. Never required from the client; populated by SPEC-07+ scraping/pricing. A match re-push (bulk-upsert) **never** overwrites these (research D6).

---

## Isolation & RLS summary (§32, Principle II)

| Table | Workspace-owned | RLS | In `WORKSPACE_OWNED_MODELS` |
|-------|-----------------|-----|-----------------------------|
| `competitors` | yes | **yes** (`emit_rls_policy` in creating migration) | yes |
| `competitor_product_matches` | yes | **yes** | yes |

RLS policy per table (from `emit_rls_policy`): `ENABLE` + `FORCE` ROW LEVEL SECURITY + `USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)` — fail-closed to **zero rows** when no context is set. The request session has `set_workspace_context` applied by the SPEC-03 auth seam before any competitor/match query. All reads/writes go through `scoped_select`/`scoped_get` (app-layer filter) — the two-layer model. The AST CI guard (`scripts/check_workspace_scoping.py`) covers both new models via `WORKSPACE_OWNED_MODELS`.

---

## URL safety verdict (save time, §11 — see `contracts/url-safety.md`)

Every `competitor_url` is validated on create / update / bulk-upsert. Accept iff: scheme ∈ {http, https}; no userinfo (`user:pass@host`); host is a public IP literal (not private/loopback/link-local/unique-local/reserved/multicast/unspecified/metadata) **or** a DNS name not in the internal deny-list/suffixes. Rejected URLs are never stored (single/update → `422 UNSAFE_URL`; bulk → reported in `rejected[]`, safe rows still upserted). No DNS resolution at save time (fetch-time is SPEC-07).

---

## URL identity & pattern (save time, §15 — see `contracts/url-pattern.md`)

- `normalized_competitor_url` = canonical **identity** (lowercase scheme+host, strip `www.`/default-port/fragment/trailing-slash, **keep query**) — part of the unique key.
- `url_pattern` = versioned **grouping** key (drop scheme+query, `:id` for id-like segments, `*` for product-slug segments after known keys, preserve locale prefixes).
- `url_pattern_version` = `URL_PATTERN_ALGORITHM_VERSION` (currently `1`). Patterns from different versions are never mixed in lookups; backfill on a version bump is out of scope (FR-011).

---

## Invariants & state

- **Match identity.** Exactly one competitor URL ↔ exactly one `product_variant` (and its `product`); a variant → unlimited matches, bounded only by the 4-col unique (Principle III, FR-005). `product_id` is derived from the variant's parent so it is always consistent.
- **Reference integrity (FR-006).** product / variant / competitor refs resolve in-workspace via composite FKs (Layer 1) + the consistency pre-check (Layer 2 → clean `422`). `current_price_id` is a soft ref.
- **Save-time safety (FR-007/008/009).** No unsafe/credentialed/non-http(s) URL is ever stored, on any write path.
- **Versioned pattern (FR-010/011).** Every stored pattern carries its algorithm version.
- **Health defaults (FR-017).** As above; owned by SPEC-07+ and never reset by a re-push.
- **Deletion (FR-016).** Hard-delete now; structured for archive-by-status; outcome reported.
- **Bulk set-based (FR-013, SC-006).** One `ON CONFLICT DO UPDATE` on the match key for all safe rows; bounded regardless of batch size; unsafe rows rejected-and-reported.
