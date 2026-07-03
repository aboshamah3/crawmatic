# Phase 0 Research: Competitors & Matches

Source of truth: `PROJECT_SPEC.md` §11 (URL safety), §15 (URL pattern derivation + versioning), §22 (table shapes + unique constraints + enums + deletion semantics), §24 (endpoints + pagination + bulk-upsert), §32 (RLS, workspace-local FKs). Built on SPEC-02 (`Base`, `WorkspaceScopedBase`, `TimestampMixin`, `TZDateTime`, `enum_column`, `emit_rls_policy`, `NAMING_CONVENTION`), SPEC-03 (`WORKSPACE_OWNED_MODELS`, scoped helpers, `set_workspace_context`, `Scope` vocabulary incl. `competitors:*`/`matches:*`, the CI scoping guard), and SPEC-04 (composite workspace-local FK pattern `(workspace_id, ref_id) -> parent(workspace_id, id)`, `unique(workspace_id, id)` parents, the keyset cursor pagination helper, the set-based bulk-upsert + last-wins dedup pattern, and the workspace-consistency pre-check helper).

Each decision: **Decision / Rationale / Alternatives considered.**

---

## D1 — Enums + their string values and defaults

**Decision.** Add six `StrEnum`s to `app_shared/enums.py`, all rendered as `VARCHAR(32)` via `enum_column` (never a PG-native enum), storing the **exact uppercase tokens the doc writes in §22**:
- `LegalStatus`: `REVIEW_REQUIRED`, `APPROVED`, `DISABLED`
- `RobotsPolicy`: `RESPECT`, `REVIEW_REQUIRED`, `IGNORE_AFTER_APPROVAL`
- `MatchPriority`: `LOW`, `NORMAL`, `HIGH`, `CRITICAL`
- `CompetitorStatus`: `ACTIVE`, `ARCHIVED`
- `MatchStatus`: `ACTIVE`, `PAUSED`, `FAILED`, `ARCHIVED`
- `HealthStatus`: `HEALTHY`, `DEGRADED`, `FAILING`, `UNKNOWN`

Plan-level **defaults** at creation (client not required to supply them): competitor `status=ACTIVE`, `legal_status=REVIEW_REQUIRED`, `robots_policy=RESPECT`; match `priority=NORMAL`, `status=ACTIVE`, `health_status=UNKNOWN`, `consecutive_failures=0`, and null `success_rate_7d` / `current_price_id` / `last_scraped_at` / `last_success_at` / `last_failed_at` / `last_error_code` (FR-017).

**Rationale.** §22 lists these exact tokens for legal/robots/priority/match-status/health in uppercase; using them verbatim keeps the stored value identical to the authoritative doc and to the scope/error vocabularies. `legal_status=REVIEW_REQUIRED` is mandated by Constitution Principle VI ("competitors begin at REVIEW_REQUIRED"); `robots_policy=RESPECT` is the safe compliant default. The autospec-decisions note simplified match status to "active/archived" at specify-time, but §22 is authoritative and gives the full `ACTIVE/PAUSED/FAILED/ARCHIVED` set — reconciled in favor of the doc.

**Alternatives considered.** (a) Lowercase values to match the SPEC-04 `ProductStatus` (`active/archived`) — rejected: those catalog statuses were not enumerated in §22, whereas competitor/match enums are, in uppercase; no cross-table value comparison happens, so the casing divergence is harmless and doc-fidelity wins. (b) A DB `CHECK` or PG-native enum — rejected: §22 mandates string-backed, application-validated enums (the existing `_AppValidatedEnumString` mechanism).

---

## D2 — Save-time SSRF: what the validator does (and does NOT) do

**Decision.** `app_shared/url_safety.py` is a **pure** validator, `validate_competitor_url(url: str) -> None`, raising a typed `UnsafeUrlError(reason: UnsafeUrlReason)` on rejection (routers map to `422 {"error":{"code":"UNSAFE_URL", ...}}`). It performs, in order:
1. Parse with `urllib.parse.urlsplit`; reject unparseable / missing host.
2. **Scheme allow-list**: reject unless `scheme in {"http","https"}`.
3. **Userinfo rejection**: reject if `parsed.username`/`parsed.password` present (i.e. `user:pass@host`).
4. **Host classification**:
   - If the host is an **IP literal** (`ipaddress.ip_address` parses it, incl. bracketed IPv6): reject unless it is a *global* address — reject `is_loopback`, `is_private` (covers 10/8, 172.16/12, 192.168/16, and IPv6 unique-local `fc00::/7`), `is_link_local` (169.254/16, `fe80::/10`), `is_reserved`, `is_multicast`, `is_unspecified`, and the cloud metadata literal `169.254.169.254` (already caught by link-local). Master check: `not ip.is_global` plus the explicit belt-and-suspenders flags.
   - Else the host is a **DNS name** (lowercased): reject if it exactly matches an entry in `INTERNAL_HOSTNAMES` or ends with any suffix in `INTERNAL_HOST_SUFFIXES`.

It does **NOT** resolve DNS at save time.

Deny-list constants (plan-level, §11 + §4 internal networking):
- `INTERNAL_HOSTNAMES = {"localhost", "postgres", "redis", "pgbouncer", "api", "scheduler", "worker", "scrapyd-http", "scrapyd-browser", "metadata.google.internal"}` (loopback name + the docker-compose service names + cloud metadata name).
- `INTERNAL_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".railway.internal")` (platform-internal suffixes).

**Rationale.** §11 splits the control into save-time (string/parse/IP-literal/hostname) and fetch-time (authoritative DNS re-resolution + per-redirect re-validation, the SPEC-07 spider's job). Keeping save-time DNS-free makes the entire validator a pure function with zero I/O — fully unit-testable in this no-Postgres/no-network build env and the highest-value security surface to test exhaustively. `ipaddress`'s `is_private`/`is_link_local`/`is_global` already encode every deny range §11 names (IPv4 and IPv6), so the check is both exhaustive and standard-library-backed rather than hand-rolled CIDR math.

**Alternatives considered.** (a) Best-effort DNS resolution at save time — rejected as the mandatory path: it adds nondeterminism/latency and can't be unit-tested here, and §11 makes fetch-time the authoritative DNS check; a best-effort save-time resolve remains a permitted future enhancement but is not required by FR-007. (b) Hand-written CIDR range checks — rejected: `ipaddress` is more correct (handles IPv6, reserved, multicast) and less error-prone. (c) Returning a boolean/verdict object instead of raising — rejected: a typed exception with a reason enum gives the router one clean mapping to the `UNSAFE_URL` error and lets the bulk path collect rejections by catching it.

---

## D3 — URL normalization vs. pattern derivation, id-like thresholds, versioning

**Decision.** `app_shared/url_pattern.py`, pure, with a single `URL_PATTERN_ALGORITHM_VERSION: int = 1` and:
- `normalize_url(url) -> str` — the **canonical identity URL** stored as `normalized_competitor_url` and used in the match unique key: lowercase scheme + host, strip `www.`, strip default port (`:80`/`:443`), strip fragment, strip trailing slash, **keep the query string** (query can distinguish the product, e.g. `?variant=123`). Result keeps the scheme so it remains a fetchable URL.
- `derive_url_pattern(url) -> str` — the **versioned grouping pattern** stored as `url_pattern`: start from the normalized host+path, **remove scheme and query**, split the path into segments, preserve a leading locale prefix, replace id-like segments with `:id`, and replace the slug segment immediately after a known product path key with `*`. Example: `https://www.Competitor.com/ar/products/iphone-15/?utm=x#frag` → `competitor.com/ar/products/*`.
- `derive_match_url_fields(url) -> tuple[str, str, int]` — returns `(normalized_competitor_url, url_pattern, URL_PATTERN_ALGORITHM_VERSION)` in one call for the router/upsert path.

Plan-level id-like thresholds (behind version 1):
- **all digits**: `segment.isdigit()`
- **UUID-like**: matches `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$` (case-insensitive)
- **long mixed alphanumeric**: `len(segment) >= 8` and contains both a letter and a digit
- **mostly digits**: `len(segment) >= 4` and digit-ratio ≥ 0.5

Known product path keys: `products`, `product`, `p`, `item` (the segment after any of these → `*`), including when preceded by a preserved locale prefix (`/ar/products/<slug>` → `/ar/products/*`). Locale prefix = a leading segment matching `^[a-z]{2}(-[a-z]{2})?$`.

**Rationale.** §15 enumerates the normalization steps and the id/product-slug rules; the edge cases in the spec make the identity-vs-grouping split explicit ("the normalized URL retains what is needed to identify the target, while the derived *pattern* drops the query for grouping"). Concrete thresholds are inherently heuristic, so §15 mandates a stored `url_pattern_version` and "never mix versions in lookups" — the constant + per-row version column lets the thresholds evolve behind a version bump without corrupting existing join keys. Version 1 thresholds are deliberately conservative (length floors) so ordinary short slugs like `iphone-15` are NOT mistaken for ids while long hashes/UUIDs/numeric ids are.

**Alternatives considered.** (a) One function returning only the pattern (folding normalization in) — rejected: the unique key needs the *identity* normalized URL (query kept) which differs from the pattern (query dropped); they must be separate outputs. (b) Dropping the query from the normalized URL too — rejected: two genuinely different products distinguished only by `?variant=` would collide on the unique key. (c) Hardcoding thresholds without a version — rejected: §15 explicitly requires the version constant + column and forbids mixing versions.

---

## D4 — Match uniqueness, competitor `unique(workspace_id, id)`, and composite workspace-local FKs

**Decision.**
- `competitors`: `unique(workspace_id, domain)` (FR-003) **plus** `unique(workspace_id, id)` (new — so the match can composite-FK the competitor workspace-locally, exactly like SPEC-04 added it to `products`/`product_variants`).
- `competitor_product_matches`: `unique(workspace_id, product_variant_id, competitor_id, normalized_competitor_url)` (FR-005) — a variant may hold unlimited matches; only an exact tuple duplicate dedupes.
- Three **composite workspace-local FKs** on the match: `(workspace_id, product_id) → products(workspace_id, id)`, `(workspace_id, product_variant_id) → product_variants(workspace_id, id)`, `(workspace_id, competitor_id) → competitors(workspace_id, id)`, plus the plain `workspace_id → workspaces.id`. `current_price_id` is a **soft** reference (no FK, target `match_current_prices` is SPEC-07/09). `scrape_profile_id`/`access_policy_id` are plain nullable UUID references with **no FK** (targets SPEC-06/10).
- Variant↔product consistency (the match's `product_id` must be the variant's actual parent) is enforced in the router by **deriving** `product_id` from the resolved variant, not by trusting a client-supplied product_id independently.

**Rationale.** The composite `(workspace_id, ref_id) → parent(workspace_id, id)` FK pattern (SPEC-04 D3) makes a cross-workspace reference structurally impossible at the DB, not merely app-filtered — satisfying FR-006/Principle II. `current_price_id` must be a soft reference because §22 forbids FKs to soft/partitioned targets and that table does not exist yet; the same logic makes profile/policy ids plain nullable until their specs land.

**Alternatives considered.** (a) Single-column FKs to `products.id`/`competitors.id` — rejected: they wouldn't be workspace-local, reintroducing the cross-workspace-reference risk. (b) Trusting a client `product_id` on the match independently of the variant — rejected: it could point at a different product than the variant's real parent; deriving it from the variant guarantees consistency.

---

## D5 — Explicit, <63-byte constraint names for `competitor_product_matches`

**Decision.** The SPEC-02 `NAMING_CONVENTION` (`uq_%(table_name)s_%(column_0_N_name)s`, `fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s`) would produce names far over Postgres's 63-byte identifier limit for this table (e.g. the auto name for the 4-column unique would be `uq_competitor_product_matches_workspace_id_product_variant_id_competitor_id_normalized_competitor_url`, ~99 chars). So `competitor_product_matches` uses **explicit** short constraint names, mirroring the `product_group_items` precedent (which did the same with a documented comment). The `competitors` table's auto names all fit (≤38 chars) and stay convention-generated.

Explicit names (all ≤63 bytes, deterministic, unambiguous):
- `uq_cpm_ws_variant_competitor_norm_url` — the 4-col match unique.
- `fk_cpm_workspace_product_products` — `(workspace_id, product_id) → products(workspace_id, id)`.
- `fk_cpm_workspace_variant_variants` — `(workspace_id, product_variant_id) → product_variants(workspace_id, id)`.
- `fk_cpm_workspace_competitor_competitors` — `(workspace_id, competitor_id) → competitors(workspace_id, id)`.
- `fk_cpm_workspace_workspaces` — `(workspace_id) → workspaces(id)`.
- `pk_competitor_product_matches` (fits) and `ix_competitor_product_matches_workspace_id` (42 chars, fits) stay auto-named.

(`cpm` = competitor_product_matches, an unambiguous, stable shorthand.)

**Rationale.** Identical situation and resolution to `product_group_items` in SPEC-04 — explicit names keep the migration and ORM identical and deterministic while respecting the 63-byte cap. A unit test asserts every emitted constraint name is ≤63 bytes.

**Alternatives considered.** Letting the convention generate over-long names — rejected: Postgres silently truncates to 63 bytes, which can collide two constraints and makes ORM↔migration names disagree.

---

## D6 — Set-based match bulk-upsert: single arbiter + reject-and-report + ref resolution

**Decision.** `app_shared/matches/upsert.py`, pure (compiles statements, never executes):
- `prepare_match_urls(rows) -> (safe: list[dict], rejected: list[dict])` — for each row, run `validate_competitor_url` then `derive_match_url_fields`; on `UnsafeUrlError`, append `{index, code:"UNSAFE_URL", reason, url}` to `rejected` and drop the row; otherwise stamp `normalized_competitor_url`/`url_pattern`/`url_pattern_version` onto the row. This is the FR-013 "reject and report, don't abort the safe set" policy, unit-testable without a DB.
- `variant_lookup_keys(rows)` / `resolve_match_variants(rows, *, by_external_id, by_sku, by_id) -> (resolved, unresolved)` — mirror the SPEC-04 `variant_parent_lookup_keys`/`resolve_variant_parents` helpers: gather the variant identities needing a lookup, and after the router runs **one** scoped `select(ProductVariant.id, .external_id, .sku, .product_id).where(... IN (...))`, fill each row's `product_variant_id` **and** `product_id` (from the variant's parent). Unresolved rows (named a variant absent in this workspace) are rejected via the consistency helper.
- `build_matches_upsert(rows) -> Insert` — **one** `pg_insert(CompetitorProductMatch).values([...]).on_conflict_do_update(index_elements=["workspace_id","product_variant_id","competitor_id","normalized_competitor_url"], set_={... updatable cols ..., "updated_at": func.now()})`. The match has a **single** unique arbiter, so — unlike the catalog upsert — there is no identity-kind partitioning: the whole safe batch is one statement.
- `dedup_last_wins` is **reused unchanged** from `app_shared.catalog.upsert`, keyed by `match_conflict_key(row) = (product_variant_id, competitor_id, normalized_competitor_url)`.

Columns updated on conflict: `competitor_url`, `url_pattern`, `url_pattern_version`, `competitor_variant_identifier`, `competitor_variant_sku`, `competitor_variant_options`, `external_title`, `scrape_profile_id`, `access_policy_id`, `priority`, `status`, plus `updated_at=func.now()`. **Never** updated: the four conflict columns, `product_id`, `workspace_id`, `id`, `created_at`, and the **health fields** (`health_status`, `last_error_code`, `consecutive_failures`, `success_rate_7d`, `current_price_id`, `last_scraped_at`, `last_success_at`, `last_failed_at`) — those are owned by SPEC-07+ and must not be reset by a re-push.

**Rationale.** One arbiter → one statement is the simplest bounded set-based form (SC-006, Principle VIII). Reusing `dedup_last_wins` and the variant-resolution shape keeps this consistent with the catalog ingestion path the codebase already tests. Excluding the health fields from the conflict-update protects scraper-populated state from being clobbered by an idempotent match re-push.

**Alternatives considered.** (a) Per-row `INSERT ... ON CONFLICT` in a loop — rejected outright (SC-006, Principle VIII). (b) Updating health fields on conflict — rejected: it would erase live scraping state on every re-push. (c) A DB round-trip per URL for safety — rejected: the validator is pure/local, so the whole batch is validated in-process before the single statement.

---

## D7 — Reuse of the workspace-consistency pre-check for match references

**Decision.** Reuse `app_shared.catalog.consistency.assert_refs_in_workspace` (SPEC-04) verbatim for the match's competitor / variant / product references: the router builds a `{id: workspace_id}` map from a single scoped `IN (...)` lookup per referenced kind and calls the helper, turning a cross-workspace or nonexistent reference into a clean `422`/`404` (`WORKSPACE_MISMATCH` / `NOT_FOUND`) **before** the composite FK would otherwise raise a raw `IntegrityError` (500). The module is already generic (plain id sets/maps, no catalog coupling), so no new consistency code is needed — only new call sites in `routers/matches.py`.

**Rationale.** The helper is deliberately framework- and entity-agnostic; reusing it avoids duplicating Layer-2 isolation logic and keeps the "clean 422 before IntegrityError" behavior identical across catalog and matches. Layer 1 (the composite FKs) remains the structural guarantee.

**Alternatives considered.** A match-specific consistency module — rejected as needless duplication; the existing helper already does exactly this.

---

## D8 — Endpoints, scopes, pagination, deletion

**Decision.** Expose exactly the §24 competitors/matches endpoints and nothing else: `POST/GET/GET{id}/PATCH/DELETE /v1/competitors`; `POST/GET/GET{id}/PATCH/DELETE /v1/matches` + `POST /v1/matches/bulk-upsert`. Gate with the **already-defined** `Scope` members (`competitors:read/write`, `matches:read/write`) via `deps.require_scopes` — no new scope is minted. List endpoints reuse the SPEC-04 keyset cursor pagination (`clamp_limit`/`encode_cursor`/`decode_cursor`/`keyset_predicate`/`paginate`, default 50 / max 500). Delete hard-deletes now (no dependent history exists until SPEC-07), is structured so a future branch flips to `status = ARCHIVED`, and returns `DeleteOutcome{id, outcome}` (reused from `app.schemas.catalog`).

**Rationale.** All four scopes already exist in `app_shared.security.scopes.Scope` (added in SPEC-03 in anticipation of this feature); the pagination and delete-outcome mechanics are already built and tested in SPEC-04 — this feature only wires them to the two new routers, honoring §24/FR-012/FR-014/FR-016.

**Alternatives considered.** Minting a `matches:bulk` scope — rejected: §22's vocabulary has no such scope; bulk-upsert is a write and uses `matches:write`.

---

## D9 — Migration head, and the unit-vs-live test split

**Decision.** One hand-authored Alembic migration `<rev>_competitors_matches_tables.py`, `down_revision = c2987b29555e` (the current head, the SPEC-04 `catalog_tables` revision), creating both tables with the exact §22 shapes, the two unique keys, the composite workspace-local FKs (explicit names per D5), and calling `emit_rls_policy` on **both** in the same migration; `downgrade` drops matches then competitors. Unit-tested **here**: model/constraint/name shapes + `<63`-byte check, RLS render for both tables, the full SSRF corpus, the URL normalize/pattern corpus + version, the match upsert compiled-to-SQL (single ON CONFLICT on 4 cols, no per-row loop), `prepare_match_urls` split, consistency checks, scope-gating wiring, the CI scoping guard green with two new models, and `alembic upgrade head --sql` offline render. **Live-DB, authored + marked** (skipped without Postgres): actual create/upsert, RLS row denial, cross-workspace blocking, no-context fail-closed, online migration run, and the end-to-end request flows.

**Rationale.** Matches the SPEC-03/04 precedent exactly (hand-authored migration because there's no live Postgres to autogenerate against; single linear head enforced by `scripts/check_single_head.sh`). The SSRF validator and the URL-pattern algorithm are the highest-value pure logic and are exhaustively unit-tested here where they need no DB.

**Alternatives considered.** Autogenerating the migration — impossible without a live DB in this env; hand-authoring reproduces the ORM shapes deterministically (verified by the offline-render test).
