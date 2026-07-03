# Feature Specification: Competitors & Matches

**Feature Branch**: `005-competitors-matches`

**Created**: 2026-07-03

**Status**: Draft

**Input**: SPEC-05 from PROJECT_SPEC.md §35 — link a client's product variants to competitor product URLs, with save-time URL safety, versioned URL normalization, and workspace isolation.

## User Scenarios & Testing *(mandatory)*

The users of this feature are workspace operators and their integrations who register the competitor stores they want to monitor and manually link each of their product variants to specific competitor product URLs. This is the "manual match" half of the core monitoring loop: it produces the variant↔competitor-URL links that later specs will scrape and price. Because these URLs are user-supplied and internal services will later connect to them, every URL is safety-validated at save time and normalized into a stable, versioned pattern.

### User Story 1 - Register competitors (Priority: P1)

An operator registers a competitor (name + domain, with legal and robots policy defaults) so that product URLs can be attached to it and, later, scraped under the right legal/access rules.

**Why this priority**: Competitors are the parent entity every match attaches to; nothing can be matched without them.

**Independent Test**: Create a competitor with a domain → it is stored, workspace-scoped, with its legal-status/robots-policy/priority defaults; re-registering the same domain in the same workspace is rejected (unique per workspace); read/update/list/delete work.

**Acceptance Scenarios**:

1. **Given** a workspace, **When** a competitor is created with a name and domain, **Then** it is stored with a legal status, robots policy, and status, scoped to the workspace.
2. **Given** an existing competitor domain in a workspace, **When** another competitor with the same domain is created, **Then** it is rejected (domain is unique per workspace).
3. **Given** a competitor, **When** it is read, updated, listed, or deleted, **Then** the operation reflects workspace-scoped state and delete reports whether it hard-deleted or archived.

### User Story 2 - Link a variant to a competitor URL, safely (Priority: P1)

An operator creates a match linking one of their product variants to a specific competitor product URL. The URL is validated at save time to be a safe, public http(s) address (no private/internal/metadata targets, no embedded credentials), and it is normalized into a canonical URL plus a versioned pattern used to group similar URLs.

**Why this priority**: The match is the core deliverable of this feature and the unit that gets scraped later; the save-time URL safety check is a non-negotiable security control.

**Independent Test**: Create a match for a variant with a valid public product URL → it is stored with a normalized URL and a derived pattern carrying the algorithm version; submit a private/internal/metadata/credentialed URL → it is rejected at save time; the same variant can be matched to many competitors and URLs.

**Acceptance Scenarios**:

1. **Given** a variant and a competitor, **When** a match is created with a valid public http(s) product URL, **Then** it is stored linked to that variant, with a normalized URL, a derived URL pattern, and the pattern algorithm version recorded.
2. **Given** a match payload whose URL points at localhost, a private/loopback/link-local/unique-local range, a cloud metadata endpoint, or an internal service hostname, or contains embedded credentials (`user:pass@host`), or uses a non-http(s) scheme, **When** the match is saved, **Then** it is rejected at save time with a clear safety error and not stored.
3. **Given** a variant, **When** multiple matches are created for it across different competitors and different URLs, **Then** all are stored (a variant can have unlimited matches); only an exact duplicate of (variant, competitor, normalized URL) within the workspace is rejected.
4. **Given** a stored match, **When** its derived pattern is read, **Then** it reflects the normalization rules (host lowercased, scheme/`www.`/trailing-slash/fragment/query removed, locale prefixes preserved, id-like segments and product slugs generalized) and carries the current algorithm version.

### User Story 3 - Bulk-upsert matches (Priority: P1)

An integration pushes many matches at once; the system upserts them set-based, validating and normalizing each URL, so re-pushing an updated match list updates existing links (matched by variant+competitor+normalized URL) instead of duplicating them.

**Why this priority**: Real match lists are large and arrive in bulk; a correct, idempotent, set-based upsert is the primary ingestion path and a scale-safety requirement.

**Independent Test**: Bulk-upsert a batch of matches → all valid ones are created (each with normalized URL + pattern + version); re-push with changes → matched rows update in place (by variant+competitor+normalized URL), no duplicates; any URL that fails save-time safety validation is rejected without aborting the whole batch as unsafe-by-default.

**Acceptance Scenarios**:

1. **Given** a batch of match records, **When** bulk-upsert runs, **Then** each URL is safety-validated and normalized, and all valid records are inserted set-based (a bounded number of statements, not one per record).
2. **Given** a batch matching existing rows by (variant, competitor, normalized URL), **When** bulk-upsert runs again, **Then** matched rows update in place and no duplicates are created.
3. **Given** a batch containing an unsafe URL, **When** bulk-upsert runs, **Then** that record is rejected (reported back) and does not get stored; safe records are handled per the batch's error policy.

### User Story 4 - Competitor/match access is workspace-isolated and scope-gated (Priority: P1)

Every competitor and match operation runs under one workspace context and requires the appropriate capability; a caller cannot read or write another workspace's competitors or matches, and matches may only reference the caller's own variants/products/competitors.

**Why this priority**: Isolation is non-negotiable; matches also cross-reference catalog entities, so workspace-local reference integrity matters.

**Independent Test**: With two workspaces populated, confirm a workspace-A caller cannot read/write workspace-B competitors or matches (including when the application filter is omitted — row-level security blocks); confirm a match cannot reference another workspace's variant/product/competitor; confirm read vs. write capability is enforced.

**Acceptance Scenarios**:

1. **Given** two workspaces with data, **When** a workspace-A caller requests workspace B's competitor or match by id or in a list, **Then** it receives none of B's data (application scoping AND row-level security, fail closed when no context).
2. **Given** a match payload referencing a variant/product/competitor belonging to another workspace (or nonexistent), **When** it is saved, **Then** it is rejected — all references must resolve within the caller's workspace.
3. **Given** a read-capability-only credential, **When** it attempts a write, **Then** the write is refused; a write-capability credential succeeds.
4. **Given** the codebase, **When** the continuous-integration scoping guard runs, **Then** it fails if any unscoped query on a competitor/match model is introduced.

### Edge Cases

- What happens when a match URL resolves syntactically but targets a private/internal host by IP literal or by an obviously-internal hostname? It is rejected at save time (the deny rules apply to IP literals and known-internal names; full DNS-rebinding defense via fetch-time re-resolution is a later spec's spider concern).
- What happens when a match URL contains a query string that distinguishes the product (e.g. `?variant=123`)? The normalized URL retains what is needed to identify the target, while the derived *pattern* drops the query for grouping; the pattern is for grouping, the normalized URL for identity.
- What happens when two different raw URLs normalize to the same normalized URL for the same variant+competitor? They collide on the unique key and the second is treated as the same match (upsert), not a duplicate.
- What happens when the URL-pattern algorithm changes later? Each stored pattern carries the algorithm version; patterns from different versions are never mixed in lookups, and a later backfill (out of scope here) re-derives them.
- What happens when a competitor is deleted while it has matches (or, later, history)? Deletion hard-deletes only when no dependent history exists and otherwise archives by status; the response indicates which occurred.
- What happens to match health fields (health status, failure counters, last-scraped timestamps, current price reference) at creation? They are initialized to sensible defaults (unknown/pending health, zero failures, null timestamps, null current-price reference) and are populated by later scraping/pricing specs, not by this feature.
- What happens when a bulk batch mixes safe and unsafe URLs? Unsafe records are rejected and reported; the batch is not silently accepted with unsafe entries dropped without notice.
- What happens when a list request exceeds the maximum page size? The page size is capped at the maximum and results are paginated by cursor.

## Requirements *(mandatory)*

### Functional Requirements

**Data & isolation**
- **FR-001**: The system MUST provide two workspace-owned entities — competitors and competitor-product matches — each carrying `workspace_id` and each protected by row-level security enabled in the same migration that creates it, and both registered so the workspace-scoped repository helpers and the continuous-integration unscoped-query guard cover them.
- **FR-002**: Cross-workspace reads and writes of competitors/matches MUST be blocked both by application scoping AND by row-level security (fail closed when no workspace context), proven by automated tests.
- **FR-003**: A competitor MUST support name, domain, status, `legal_status` (REVIEW_REQUIRED/APPROVED/DISABLED), `robots_policy` (RESPECT/REVIEW_REQUIRED/IGNORE_AFTER_APPROVAL), optional default scrape-profile/access-policy references (plain nullable, targets in later specs), and optional per-competitor concurrency/rate caps; competitor `domain` MUST be unique per workspace.
- **FR-004**: A match MUST link a product variant (and its product) to a competitor via a competitor URL, storing the raw URL, a normalized URL, a derived URL pattern with its algorithm version, optional competitor-side identifiers (variant identifier/SKU/options/title), optional per-match scrape-profile/access-policy references, a priority (LOW/NORMAL/HIGH/CRITICAL), a status, and the health fields (health status, last error code, consecutive failures, 7-day success rate, current-price soft reference, last-scraped/success/failed timestamps).
- **FR-005**: A match MUST be unique on `(workspace_id, product_variant_id, competitor_id, normalized_competitor_url)`; a single variant MUST be allowed unlimited matches (many competitors, many URLs) subject only to that uniqueness.
- **FR-006**: All match foreign references (product, product variant, competitor) MUST resolve within the caller's workspace via workspace-local references; a reference to another workspace's entity or a nonexistent entity MUST be rejected. The current-price reference MUST be a soft reference (no foreign key).

**URL safety (save-time SSRF control)**
- **FR-007**: At save time, every competitor URL MUST be validated and rejected unless its scheme is http or https and its host is a public DNS name or public IP; the system MUST reject localhost, private ranges (10/8, 172.16/12, 192.168/16), loopback, link-local (169.254/16, fe80::/10), unique-local (fc00::/7), cloud metadata endpoints (e.g. 169.254.169.254), and internal service hostnames.
- **FR-008**: At save time, a URL containing embedded credentials (`user:pass@host`) MUST be rejected.
- **FR-009**: The save-time validator MUST apply equally on single-match create, match update, and bulk-upsert; an unsafe URL MUST never be stored. (Fetch-time DNS re-resolution and per-redirect re-validation are out of scope for this feature — they are the scraper's responsibility in a later spec.)

**URL normalization & versioned pattern**
- **FR-010**: The system MUST produce a normalized competitor URL and a derived URL pattern for every match, applying the normalization rules: lowercase host, remove scheme, remove `www.`, remove trailing slash, remove fragment, remove query string for the pattern, preserve locale prefixes (e.g. `/ar/`, `/en/`), split path into segments, replace id-like segments (all-digits / UUID-like / long mixed-alphanumeric / mostly-digits) with an id placeholder, and replace product-slug segments after known product path keys (`/products/`, `/product/`, `/p/`, `/item/`, locale-prefixed variants) with a wildcard.
- **FR-011**: The system MUST maintain a URL-pattern algorithm version constant and store the version on every row that stores a derived pattern; patterns from different algorithm versions MUST NOT be mixed in lookups. (The backfill/re-derivation maintenance task on version bump is out of scope for this feature.)

**Endpoints, bulk, pagination, deletion**
- **FR-012**: The system MUST expose the endpoints under `/v1` — competitors (create, list, get, update, delete) and matches (create, list, get, update, delete, bulk-upsert) — and nothing outside competitors/matches scope.
- **FR-013**: The system MUST provide set-based bulk match upsert that inserts-or-updates in a bounded number of statements (never one per record), keyed on the match uniqueness tuple, validating and normalizing each URL; unsafe records are rejected and reported rather than silently stored.
- **FR-014**: List endpoints MUST use cursor-based pagination with a default page size of 50 and a maximum of 500.
- **FR-015**: Every endpoint MUST run under the request's workspace context and be gated by the appropriate capability (competitor read/write, match read/write); a credential lacking the required capability MUST be refused.
- **FR-016**: Deletion of a competitor or match MUST hard-delete only when no dependent history exists and otherwise archive by status; because no history exists yet, deletes may hard-delete now, but the model and endpoint MUST be structured for archive-by-status and the response MUST indicate which occurred.
- **FR-017**: Match health fields MUST be initialized to sensible defaults on creation (unknown/pending health, zero consecutive failures, null success rate/current-price/last-* timestamps) and MUST NOT be required from the client; their population by scraping/pricing is out of scope for this feature.

### Key Entities *(include if data involved)*

- **Competitor**: A workspace-owned competitor store (name, unique-per-workspace domain, status, legal status, robots policy, optional default profile/policy references, optional concurrency/rate caps).
- **Competitor-product match**: A workspace-owned link from a product variant (and its product) to a competitor URL, carrying the raw + normalized URL, versioned pattern, optional competitor-side identifiers, priority, status, and health fields; unique per (workspace, variant, competitor, normalized URL).
- **URL safety verdict**: The save-time decision that a URL is a safe public http(s) target with no embedded credentials and no private/internal/metadata destination.
- **URL pattern**: The versioned, normalized grouping key derived from a competitor URL, used to join matches to learned strategies in later specs.
- **Bulk-match batch**: A set of match records ingested set-based, each URL-validated and normalized, keyed on the match uniqueness tuple.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A competitor can be created and its domain is unique per workspace (0 duplicate domains per workspace).
- **SC-002**: A match links to a product variant and stores a normalized URL plus a derived pattern with the current algorithm version, 100% of the time it is created.
- **SC-003**: A single variant can hold an unlimited number of matches; only exact `(variant, competitor, normalized URL)` duplicates within a workspace are rejected (0 such duplicates stored).
- **SC-004**: 100% of URLs targeting localhost, private/loopback/link-local/unique-local ranges, cloud metadata endpoints, internal hostnames, non-http(s) schemes, or containing embedded credentials are rejected at save time and never stored — on single create, update, AND bulk-upsert.
- **SC-005**: URL normalization and pattern derivation produce the documented canonical form for a representative corpus (host lowercased; scheme/`www.`/trailing-slash/fragment/query handled; locale prefixes preserved; id-like segments and product slugs generalized), and every stored pattern carries the algorithm version.
- **SC-006**: A bulk match upsert of any batch size runs in a bounded number of statements (not proportional to record count), and re-pushing an unchanged batch creates 0 duplicates.
- **SC-007**: In a two-workspace test, 0 rows of workspace B's competitors/matches are read or written by a workspace-A caller, including when the application filter is omitted (row-level security blocks) and when no workspace context is set (fail closed); and 0 matches referencing another workspace's variant/product/competitor are stored.
- **SC-008**: A read-capability-only credential completes 0 successful writes; a write-capability credential completes writes; the continuous-integration guard fails the build on 100% of introduced unscoped competitor/match queries.

## Assumptions

- This feature builds on the SPEC-02 database foundation, the SPEC-03 isolation machinery (workspace-scoped base/repository helpers, per-request workspace context, the API-key scope vocabulary including `competitors:read/write` and `matches:read/write`, the continuous-integration unscoped-query guard), and the SPEC-04 catalog (products + product_variants, the partial-unique-index + composite-workspace-local-FK pattern, the cursor pagination helper, and the set-based bulk-upsert pattern).
- Save-time URL safety validates the URL string and any IP-literal host against the deny rules and rejects embedded credentials and non-http(s) schemes; authoritative DNS re-resolution against the deny rules and per-redirect re-validation happen at fetch time in the scraper (a later spec) — this feature is not responsible for fetch-time checks. Whether save-time performs a best-effort DNS resolution is an implementation choice; the deny-list logic on IP literals and known-internal names is the mandatory, fully-testable core.
- The URL-pattern algorithm version is a single constant bumped when the derivation changes; the re-derivation/backfill maintenance task on a version bump is out of scope (a later maintenance spec).
- Match health fields exist as columns initialized to defaults; the scraping and pricing that populate them (observations, current prices, error codes, success rates) are SPEC-07+ and out of scope. `current_price_id` is a soft reference with no foreign key.
- Competitors/matches carry optional `scrape_profile_id`/`access_policy_id` references whose target tables (scrape profiles SPEC-06, access policies SPEC-10) do not exist yet; these are plain nullable references with no foreign key until those specs land.
- Scrape profiles, access policies, scraping/observations/prices/alerts, the domain strategy optimizer, and fetch-time URL re-validation are out of scope (SPEC-06+).
- Build/CI environment has no live PostgreSQL (no container engine here): DB-independent logic (model/constraint shapes and naming render, row-level-security DDL render, the save-time SSRF URL validator, URL normalization + pattern derivation + version constant, bulk-upsert statement construction, pagination, scope-gating wiring, workspace-consistency checks) is fully unit-tested here; acceptance items requiring a live database (actual create/upsert, row-level-security row denial, cross-workspace blocking, migration run, end-to-end request flows) are authored and validated on a PostgreSQL-capable host.
