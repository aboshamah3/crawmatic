# Feature Specification: Scrapyd HTTP Spider MVP

**Feature Branch**: `007-scrapyd-http-spider`

**Created**: 2026-07-03

**Status**: Draft

**Input**: User description: SPEC-07 — Scrapyd HTTP Spider MVP (see PROJECT_SPEC §35.07, §8, §11, §16–§19, §22).

## Overview

This feature delivers the first working vertical slice of the scraping runtime: a single,
generic, database-configurable HTTP spider (`generic_price_spider`) that runs inside Scrapyd,
fetches a competitor product page **safely**, extracts a price using database-configured
strategies, validates it, and persists a `PriceObservation` (plus a `request_attempt` audit
row and an updated `match_current_price`). The slice is proven end-to-end against **local
fixture HTML pages**, not real competitor sites.

The spider **stops at persistence**. Alert computation, variant price states, alert events,
and webhook delivery are the responsibility of a later `price_analysis` task (SPEC-09) and are
explicitly out of scope here. Distributed rate limiting, in-flight dedup, the domain strategy
optimizer, proxies/access policies, and the browser spider are later specs; this spider uses
`DIRECT_HTTP` only.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Extract and persist a price from a product page (Priority: P1)

An operator has a competitor product match configured with a scrape profile and a product URL.
When a scrape job runs the `generic_price_spider` through Scrapyd for that match, the spider
loads the match and its resolved scrape profile from the database, fetches the product page,
extracts the price via the configured extraction strategy (JSON-LD first), and saves a
validated `PriceObservation` for the matched product variant.

**Why this priority**: This is the core MVP proof — it demonstrates the entire Scrapyd → Scrapy
→ extract → persist path works. Without it there is no scraping capability at all.

**Independent Test**: Seed one workspace, product, variant, competitor, match, and scrape
profile; serve a JSON-LD fixture page at the match URL; schedule `generic_price_spider` with
`workspace_id`, `scrape_job_id`, `match_ids`; assert exactly one `price_observations` row is
written with the correct price, currency, `extraction_method=JSON_LD`, confidence ≥ threshold,
`success=true`, and that `match_current_prices` is updated to point at it.

**Acceptance Scenarios**:

1. **Given** a match whose product page exposes a valid JSON-LD `Product`/`Offer` with price and
   currency, **When** the spider fetches and extracts, **Then** a `price_observation` is saved
   with `success=true`, the numeric price as `NUMERIC(18,4)`, the currency, `extraction_method`,
   `extraction_confidence`, `selector_used`, and `scraped_at`.
2. **Given** the same match, **When** extraction succeeds, **Then** `match_current_prices` for
   `(workspace_id, match_id)` is upserted (unique constraint) with the latest price and a soft
   reference to the observation.
3. **Given** the spider run, **When** any target is attempted, **Then** exactly one
   `request_attempt` row records the URL, access method (`DIRECT_HTTP`), status code, response
   time, and success/error for that attempt.
4. **Given** the spider receives `workspace_id`, `scrape_job_id`, and `match_ids` arguments,
   **When** it loads data, **Then** every DB query is scoped to `workspace_id` and no rows from
   other workspaces are ever read or written.

---

### User Story 2 - Block unsafe (SSRF) fetches at connection time (Priority: P1)

Competitor URLs are user-supplied and the spider fetches them from inside the internal network.
Before any connection completes, the spider must re-resolve the host and validate the connected
IP against the deny rules, and must re-validate every redirect hop — defeating DNS rebinding and
records that changed after save-time validation.

**Why this priority**: Constitution Principle VI (Internal-Only & Legally Compliant Access) is
NON-NEGOTIABLE. A spider that can be steered to internal addresses is a security hole; this guard
must ship with the very first spider.

**Independent Test**: Configure a match whose URL resolves to a private/loopback/link-local IP (or
a public URL that 302-redirects to one); run the spider; assert the fetch is refused, **no** price
observation with a price is persisted, and a `request_attempt` (and failure `price_observation`)
records the safety error code.

**Acceptance Scenarios**:

1. **Given** a URL whose host resolves to a private/loopback/link-local/unique-local/metadata IP,
   **When** the spider attempts to connect, **Then** the request is refused before body download,
   and the failure is recorded with an SSRF/unsafe-URL error code.
2. **Given** a public URL that redirects to an internal IP, **When** the spider follows the
   redirect, **Then** the redirect hop is re-validated and refused.
3. **Given** a URL with a disallowed scheme or embedded userinfo (`user:pass@host`), **When** the
   spider processes it, **Then** it is rejected without a network fetch.
4. **Given** any SSRF rejection, **When** persistence runs, **Then** no observation is marked
   `success=true` for that target.

---

### User Story 3 - Multiple extraction strategies with confidence and price validation (Priority: P2)

Different competitor pages expose prices differently. The spider tries the configured extraction
strategies in order (JSON-LD → CSS selector → regex for this MVP), attaches a confidence to each
result, and validates the candidate price before accepting it — because a wrong price is worse
than a missing one.

**Why this priority**: Broadens the spider beyond a single happy path and enforces the correctness
guardrails (confidence threshold, price validation rules) that make observations trustworthy.

**Independent Test**: Serve three fixtures (JSON-LD, CSS-selector-only, regex-only); with matching
DB-configured selectors/rules, assert each yields a `price_observation` with the expected
`extraction_method` and default confidence (JSON-LD 0.95, CSS 0.85, regex 0.75); serve a fixture
whose only candidate is a discount/"save X"/old price and assert it is rejected.

**Acceptance Scenarios**:

1. **Given** a page with only a CSS-selectable price and a configured `price_selector`, **When**
   JSON-LD extraction finds nothing, **Then** the spider falls back to CSS and records
   `extraction_method=CSS` with confidence 0.85.
2. **Given** a page whose price is only reachable by a configured regex rule, **When** earlier
   strategies fail, **Then** the spider extracts via regex with confidence 0.75.
3. **Given** an extracted candidate whose confidence is below the accepted threshold (default
   0.75), **When** validation runs, **Then** the observation is saved with `success=false` and a
   `PRICE_NOT_FOUND`/low-confidence error, and `match_current_prices` is not updated with a bad
   price.
4. **Given** a candidate that is non-positive, non-finite (NaN/Infinity), has more decimal places
   than the column scale, is an old/installment/discount/shipping value, or matches a
   `reject_if_text_contains` rule, **When** validation runs, **Then** the candidate is rejected
   (not silently rounded or accepted).
5. **Given** a page whose price currency differs from the client variant currency, **When** the
   observation is saved, **Then** it is stored with `comparable=false` and a `CURRENCY_MISMATCH`
   warning, and is excluded from comparison.

---

### User Story 4 - Authenticated dispatch to Scrapyd (Priority: P2)

The worker schedules the spider by calling Scrapyd's `schedule.json`. Because Scrapyd's HTTP API
can accept code uploads (`addversion.json`), the scraping service requires basic auth and every
schedule call must authenticate.

**Why this priority**: An unauthenticated Scrapyd node reachable on the internal network is remote
code execution. Authenticated dispatch is required for the spider to be operable safely, but it is
a thin client wrapper on top of the spider, hence P2.

**Independent Test**: Point the dispatch client at a Scrapyd endpoint requiring basic auth; assert a
call with correct credentials schedules `generic_price_spider` (returns a jobid) and a call with
missing/incorrect credentials is rejected (401) and does not schedule.

**Acceptance Scenarios**:

1. **Given** configured Scrapyd credentials, **When** the worker dispatches
   `generic_price_spider` with the spider arguments, **Then** the authenticated `schedule.json`
   call succeeds and returns a Scrapyd `jobid`.
2. **Given** missing or wrong credentials, **When** a schedule call is made, **Then** Scrapyd
   rejects it and no spider run starts.
3. **Given** the spider arguments (`workspace_id`, `scrape_job_id`, `match_ids`, `mode`), **When**
   dispatch runs, **Then** they are passed through to the scheduled spider unchanged.

---

### User Story 5 - Reactor-safe, batched persistence (Priority: P3)

The spider runs on Twisted's reactor. DB writes must not block the reactor, and observations,
request attempts, and current-price updates are written in small batches (flush every N items or
T seconds), never one commit per item.

**Why this priority**: Correctness and scale hygiene mandated by §8. For the fixture-scale MVP it
is not directly user-visible, but the persistence design must be right from the start so later
large jobs do not serialize on the pooler or stall the reactor.

**Independent Test**: Run the spider over a batch of N fixture matches; assert all N observations
persist, that DB writes are dispatched off the reactor thread (async driver or `deferToThread`),
and that persistence occurs in batched flushes rather than one commit per item (e.g. observed
commit count << N, or explicit flush-boundary assertions).

**Acceptance Scenarios**:

1. **Given** a spider run over N matches, **When** items are produced, **Then** the pipeline
   buffers and flushes in batches by size or time, and all N rows are persisted by spider close.
2. **Given** any DB call inside a pipeline or middleware, **When** it executes, **Then** it does
   not block the reactor thread (async driver integrated with the reactor, or wrapped in
   `deferToThread`).
3. **Given** a partial batch at spider close, **When** the spider finishes, **Then** the final
   buffer is flushed so no observations are lost.

---

### Edge Cases

- **Page fetch fails (timeout, DNS failure, 4xx/5xx)**: a `request_attempt` records the failure with
  a status/error code, and a `price_observation` with `success=false` and an appropriate error code
  is written; `match_current_prices` is not updated with a price.
- **Match URL missing or match not found for workspace**: the target is skipped/failed with a clear
  error; no cross-workspace read occurs.
- **No extraction strategy yields a price (`PRICE_NOT_FOUND`)**: a failure observation is recorded.
- **Only a single unlabeled number on the page**: confidence 0.40 → below threshold → rejected by
  default.
- **Robots policy disallows the path**: the per-domain robots middleware skips/records the target
  per the resolved `robots_policy` (not Scrapy's process-global `ROBOTSTXT_OBEY`).
- **Duplicate dispatch of the same job/batch**: dispatch is idempotent (guarded so a retried
  schedule call does not double-run the same batch).
- **Retention drops old partitions**: soft references (`match_current_prices.observation_id`) may
  dangle; readers tolerate it because the current-state row carries all needed fields.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a single generic HTTP spider `generic_price_spider` that runs
  under Scrapyd and accepts arguments `workspace_id`, `scrape_job_id`, `match_ids`, and `mode`.
- **FR-002**: The spider MUST load the target matches and their configuration from the database,
  every query scoped to the provided `workspace_id` (workspace isolation is non-negotiable).
- **FR-003**: The spider MUST use the cached resolved scrape profile per match (per §9 resolution
  caching) rather than re-running the full resolution chain for each match.
- **FR-004**: The spider MUST fetch competitor product URLs using `DIRECT_HTTP` only in this slice
  (no proxies, no browser, no access-policy dispatch).
- **FR-005**: The system MUST enforce URL safety at fetch time: allow only `http`/`https`; reject
  userinfo; and deny private, loopback, link-local, unique-local, cloud-metadata, and internal
  addresses validated against the **resolved IP at connection time**, re-validating every redirect
  hop. This MUST reuse/extend the existing save-time validator (`validate_competitor_url`).
- **FR-006**: The system MUST implement robots handling as a custom per-request downloader
  middleware that resolves `robots_policy` per competitor/domain from cached config, not Scrapy's
  process-global `ROBOTSTXT_OBEY`.
- **FR-007**: The spider MUST attempt extraction strategies in configured order — for this MVP:
  JSON-LD/structured data, CSS selector, and regex — falling back to the next on failure and
  stopping at `PRICE_NOT_FOUND` if none succeed.
- **FR-008**: Every extraction result MUST carry a confidence value, with defaults tunable via DB
  configuration (JSON-LD 0.95, CSS 0.85, regex 0.75; a single unlabeled number 0.40). The default
  minimum accepted confidence is 0.75.
- **FR-009**: The system MUST validate a candidate price before accepting it: it MUST be a
  `Decimal`/`NUMERIC(18,4)` value, `> 0`, finite (reject NaN/Infinity), have no more decimal places
  than the column scale (reject, do not round), match required currency if configured, satisfy
  min/max rules, meet the confidence threshold, and be rejected if it is an old/installment/
  discount/shipping value or matches configured `reject_if_text_contains` rules. Validation rules
  come from `scrape_profiles.validation_rules`.
- **FR-010**: Monetary values MUST use `Decimal` in Python and `NUMERIC(18,4)` in Postgres; floats
  MUST NOT be used for prices anywhere in the path.
- **FR-011**: When the competitor currency differs from the client variant currency, the system
  MUST save the observation with `comparable=false`, record a `CURRENCY_MISMATCH` warning, and
  exclude it from comparison (no FX conversion in v1).
- **FR-012**: The system MUST create the `price_observations` and `request_attempts` tables as
  monthly-partitioned tables from birth (partitioned by `scraped_at` and `created_at`
  respectively), with the partition key included in the primary key, per the §22 partitioned-table
  rules, with an initial migration and at least the current + next month partitions.
- **FR-013**: For each attempted target, the spider MUST write exactly one `request_attempt` row
  capturing url, access method, status code, response time, success, and error code/message.
- **FR-014**: On success, the spider MUST write a `price_observation` row and upsert
  `match_current_prices` (unique on `workspace_id, match_id`) with a soft reference to the
  observation; on failure it MUST write a `success=false` observation and MUST NOT overwrite the
  current price with a bad value.
- **FR-015**: The spider MUST update the scrape job target state for each processed match.
- **FR-016**: Persistence MUST be batched (flush by item count or elapsed time, and a final flush at
  spider close), never one commit per item.
- **FR-017**: All DB access inside pipelines/middlewares MUST be reactor-safe (async driver
  integrated with the reactor, or wrapped in `deferToThread`), and this choice MUST be made once in
  `libs/scrape-core`. DB connections MUST go through PgBouncer with a small per-process pool.
- **FR-018**: The worker MUST dispatch the spider via an authenticated Scrapyd `schedule.json`
  call; the scraping service MUST require basic auth; unauthenticated/incorrect calls MUST be
  rejected and MUST NOT start a run.
- **FR-019**: Dispatch MUST be idempotent so a retried schedule call for the same
  `scrape_job_id`/batch does not double-run the batch (guard via a dispatch key and/or persisted
  Scrapyd jobid).
- **FR-020**: The spider MUST NOT compute alerts, variant price states, alert events, or webhook
  events; it stops at persistence (those belong to later specs).
- **FR-021**: The feature MUST be demonstrable end-to-end using local fixture HTML pages (JSON-LD,
  CSS, regex fixtures), with no requests to real competitor sites in tests.
- **FR-022**: Shared spider code (spider base, pipelines, middlewares, extraction, reactor-safe DB
  access) MUST live in `libs/scrape-core`; the Scrapyd service app (`apps/scrapers`) packages the
  Scrapy project. The one-way dependency rule (`apps → libs`; `scrape-core` may depend on `shared`,
  not vice versa) MUST hold.

### Key Entities *(include if feature involves data)*

- **PriceObservation** (`price_observations`, partitioned monthly by `scraped_at`): an immutable
  record of one extraction attempt result for a match/variant — price, old_price, currency,
  stock_status, raw_title, success, comparable, error_code/message, extraction_method,
  extraction_confidence, selector_used, scraped_at, plus workspace/match/product/variant/job refs.
- **RequestAttempt** (`request_attempts`, partitioned monthly by `created_at`): an audit record of a
  single HTTP fetch attempt — url, access_method, proxy refs (nullable/unused here), status_code,
  response_time_ms, success, error_code/message, attempt_number, plus workspace/job/match refs.
- **MatchCurrentPrice** (`match_current_prices`, unique on `workspace_id, match_id`): the latest
  known price snapshot for a match — price, old_price, currency, stock_status, comparable, a soft
  `observation_id` reference, success, extraction_method/confidence, scraped_at, updated_at.
  (Model may already exist from earlier specs; the spider updates it.)
- **Match / ScrapeProfile (resolved)**: existing entities the spider reads — the competitor product
  match (URL, currency expectations, robots policy source) and its resolved scrape profile
  (extraction selectors/rules, validation_rules, confidence config, mode).
- **Fixture pages**: local HTML test assets exercising JSON-LD, CSS-selector, and regex extraction
  paths, plus SSRF-redirect and rejection cases.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Given a seeded match and a JSON-LD fixture page, a single `generic_price_spider` run
  through Scrapyd produces exactly one successful `price_observation` with the correct price and
  currency and updates `match_current_prices` — the full path works end-to-end.
- **SC-002**: 100% of attempted fetches to private/loopback/link-local/internal IPs (directly or via
  redirect) are refused before body download, and none result in a `success=true` observation.
- **SC-003**: Each of the three extraction strategies (JSON-LD, CSS, regex) correctly extracts a
  price from its corresponding fixture with the expected default confidence, and every price below
  the 0.75 confidence threshold or failing a validation rule is rejected rather than saved as a
  valid price.
- **SC-004**: No price is ever stored as a floating-point value; every persisted price is a
  `NUMERIC(18,4)` decimal, and NaN/Infinity/over-scale/non-positive candidates are rejected 100% of
  the time.
- **SC-005**: An authenticated dispatch schedules the spider (returns a jobid) while an
  unauthenticated dispatch is rejected 100% of the time; a retried dispatch of the same batch does
  not produce duplicate runs.
- **SC-006**: Over a batch of N fixture matches, all N observations persist with fewer DB commits
  than N (batched flushing), and no DB call blocks the reactor thread.
- **SC-007**: The entire feature is validated using fixtures only — the test suite makes zero
  network requests to real competitor domains.

## Assumptions

- The `competitor_product_matches`, `scrape_profiles` (and their resolution/caching), `products`,
  `product_variants`, and `competitors` models from SPEC-04/05/06 exist and are reused; this spec
  adds `price_observations`, `request_attempts`, and the `match_current_prices` write path.
- `match_current_prices` and `variant_price_states` schemas are defined in §22; this spec creates
  `match_current_prices` if not already present and writes to it, but does not populate
  `variant_price_states` (that is the price_analysis task's job in a later spec).
- Scrape-profile resolution and its Redis cache (SPEC-06/§9) are available to the spider; the spider
  consumes the cached resolved config rather than re-resolving per match.
- The `price_analysis` Celery task and job orchestration (dispatch of jobs, batching into
  Scrapyd calls at scale) are later specs; this slice proves the spider path and provides an
  authenticated dispatch client, exercised via tests/fixtures rather than a full scheduler.
- A live Postgres/Redis/Scrapyd stack is unavailable in the build environment (no container engine),
  so live end-to-end runs are authored as tests that skip cleanly; DB/reactor/network-independent
  logic (extraction, validation, URL safety, confidence, batching boundaries) is fully unit-tested.
- Confidence defaults and the minimum-accepted-confidence threshold are seeded as DB-tunable config
  with the §17 defaults; the promotion threshold (0.85) belongs to the strategy optimizer (later
  spec) and is not exercised here.
- Fixture pages are served locally (in-process test server or file/`data:`-backed responses); no
  real competitor site is contacted in any test.
