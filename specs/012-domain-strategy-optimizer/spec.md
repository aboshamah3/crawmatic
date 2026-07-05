# Feature Specification: Domain Strategy Optimizer

**Feature Branch**: `012-domain-strategy-optimizer`

**Created**: 2026-07-05

**Status**: Draft

**Input**: User description: SPEC-12 — Domain Strategy Optimizer (roadmap §35.12; detail §14, §15; data models §22)

## Clarifications

### Session 2026-07-05

All three items below were resolved doc-first (autospec) from the master spec; none required
a user decision.

- Q: Where does the per-attempt learning signal come from — a separate discovery double-fetch,
  or the existing scrape pipeline? → A: The existing SPEC-07 spider per-attempt path
  (`request_attempts`) reports each attempt's `(method_type, method_name, outcome, price,
  currency, confidence, response_time)` into the buffered stats; no separate production
  double-fetch. Discovery mode is the only path that actively probes multiple methods, and
  only on a 3–10 URL sample. (source: doc §14 Atomic stats + SPEC-07 reuse)
- Q: Over what window is the rediscovery "success rate < 80%" and "3 consecutive failures"
  evaluated? → A: "3 consecutive failures" is tracked via the profile's `recent_failure_count`
  (incremented on failure of the preferred method, reset to 0 on a qualifying success);
  "success rate < 80%" is read from the per-method cumulative `strategy_attempt_stats.success_rate`
  combined with pending buffered deltas. (source: doc §22 fields `recent_failure_count`,
  `success_rate` + §14 flushed-DB-plus-pending-deltas rule)
- Q: How is discovery triggered — automatically, by an operator, or both? → A: Both. A new
  `(workspace, competitor, domain, url_pattern)` with no profile is created in
  `DISCOVERY_REQUIRED` and enqueues discovery on the `strategy_discovery` queue; an operator
  may also trigger a discovery run explicitly. Both converge on one discovery-run + profile-seed
  code path. (source: doc §14 "when a new competitor/domain pattern is added" + §26
  `strategy_discovery` queue)

## User Scenarios & Testing *(mandatory)*

The "actors" here are the scraping runtime (which records attempt outcomes and asks for a
starting strategy) and the operator/API consumer (who triggers discovery and inspects
learned strategies). Every table and query is workspace-scoped.

### User Story 1 - Learn and store the winning access/extraction method per domain + pattern (Priority: P1)

As the scraping system, after several scrape attempts against a competitor's domain and URL
template, I want the winning access method and extraction method to be learned and stored so
that the platform stops guessing and records what actually works for that
`workspace + competitor + domain + url_pattern`.

**Why this priority**: This is the core value of the optimizer — without a learned,
persisted winning combination there is nothing to consume later. It is the smallest slice
that delivers standalone value: an operator can already see which method won for a domain.

**Independent Test**: Seed a strategy profile for a `(workspace, competitor, domain,
url_pattern)`, feed a sequence of attempt outcomes (method used, success/failure, extracted
price + currency, confidence), and assert that once the promotion rule is satisfied the
profile stores the preferred access method, preferred extraction method, their confidences,
transitions to `ACTIVE`, and that a non-qualifying sequence does not promote. Also assert
`derive_url_pattern(url)` groups differing product URLs under one stable pattern with the
recorded algorithm version.

**Acceptance Scenarios**:

1. **Given** a profile in `DISCOVERY_REQUIRED`/`LEARNING` for a domain+pattern, **When** a
   method achieves 3 successful extractions across at least 3 different URLs of the same
   domain+pattern, each with confidence ≥ the configured threshold (default 0.85), a valid
   numeric price, and a valid currency when required, **Then** that method is promoted to
   the profile's preferred method (with confidence) and the profile becomes `ACTIVE`.
2. **Given** a method with 3 successes but only across 2 distinct URLs, **When** promotion is
   evaluated, **Then** the method is NOT promoted (distinct-URL requirement unmet).
3. **Given** a successful extraction whose confidence is below the threshold, or whose price
   is non-numeric/invalid, or whose required currency is missing, **When** promotion is
   evaluated, **Then** that attempt does not count toward the 3-confirmation total.
4. **Given** two URLs `https://www.example.com/products/red-shoe-123` and
   `http://example.com/products/blue-shoe-999?ref=x#frag`, **When** their patterns are
   derived, **Then** both normalize to the same `example.com/products/*` pattern stamped
   with the current `url_pattern_version`.
5. **Given** access and extraction are tracked separately, **When** an access method and an
   extraction method each satisfy the rule, **Then** both `preferred_access_method` and
   `preferred_extraction_method` are set with their own confidences.

---

### User Story 2 - Start future scrapes from the learned strategy (Priority: P2)

As the scraping runtime, when I am about to scrape a matched competitor URL, I want to look
up the learned strategy for its `workspace + competitor + domain + url_pattern` and start
from the preferred access + extraction method rather than walking the full escalation ladder,
so that resources are saved and success comes faster.

**Why this priority**: This is where the learning pays off in production. It depends on US1
having produced a profile, so it is P2.

**Independent Test**: Seed an `ACTIVE` profile with preferred methods, request the starting
strategy for a URL that derives to that pattern, and assert the preferred access + extraction
methods are returned first; then request a URL with no profile (or `DISCOVERY_REQUIRED`) and
assert the default full escalation ladder is returned instead.

**Acceptance Scenarios**:

1. **Given** an `ACTIVE` profile with `preferred_access_method = PROXY_HTTP` and
   `preferred_extraction_method = CSS_SELECTOR`, **When** the runtime resolves a strategy for
   a matching URL, **Then** it receives `PROXY_HTTP` + `CSS_SELECTOR` as the starting point.
2. **Given** no profile exists for the derived pattern, **When** the runtime resolves a
   strategy, **Then** it receives the default escalation ladder (and a new profile in
   `DISCOVERY_REQUIRED` is available to be created for that key).
3. **Given** a profile in `DISABLED`, **When** the runtime resolves a strategy, **Then** the
   learned preference is not applied (default behavior is used).
4. **Given** a strategy lookup, **When** the stored `url_pattern_version` differs from the
   current `URL_PATTERN_ALGORITHM_VERSION`, **Then** patterns from the mismatched version are
   never used for the lookup.

---

### User Story 3 - Discover a strategy for a brand-new domain from sample URLs (Priority: P3)

As an operator (or the system, automatically) adding a new competitor domain/template, I want
to run discovery on 3–10 sample matched URLs so that the platform tests candidate access and
extraction methods on a small sample, finds the winning combination, and seeds the learned
profile before large batches run.

**Why this priority**: Discovery bootstraps learning for domains with no history. US1 can
also learn passively from production attempts, so explicit discovery is valuable but P3.

**Independent Test**: Trigger a discovery run for a `(workspace, competitor, domain,
url_pattern)` with a set of sample URLs; assert a `strategy_discovery_runs` row is created
with `sample_size`, progresses through statuses, records `winning_access_method` /
`winning_extraction_method` on completion, and that the corresponding profile is seeded/updated
accordingly.

**Acceptance Scenarios**:

1. **Given** a new domain+pattern with 5 sample URLs, **When** discovery runs, **Then** a
   discovery-run record captures `sample_size = 5`, tests access then extraction methods, and
   records the winning combination on completion with `completed_at` set.
2. **Given** a discovery request with fewer than 3 or more than 10 sample URLs, **When** it is
   submitted, **Then** it is rejected/validated per the 3–10 sample bound.
3. **Given** discovery finds a winning combination, **When** it completes, **Then** the
   profile for that key is updated with the winning methods and moved out of
   `DISCOVERY_REQUIRED` (to `LEARNING` or `ACTIVE` per confirmation state).
4. **Given** discovery finds no working combination, **When** it completes, **Then** the run
   is recorded as such and the profile status reflects that discovery did not succeed.

---

### User Story 4 - Detect degradation and trigger rediscovery (Priority: P4)

As the scraping system, when a previously winning method starts failing, I want the profile to
be marked degraded and rediscovery to be triggered so that a changed site or blocked method
does not silently keep wasting attempts.

**Why this priority**: Keeps learned strategies healthy over time. Requires US1–US3 to exist
first, so it is P4.

**Independent Test**: Feed failure signals to an `ACTIVE` profile and assert the status
transitions to `DEGRADED` and a rediscovery trigger is raised when any configured
rediscovery condition is met; assert healthy signals do not trigger it.

**Acceptance Scenarios**:

1. **Given** an `ACTIVE` profile, **When** the preferred method records 3 consecutive
   failures, **Then** the profile transitions to `DEGRADED` and rediscovery is triggered.
2. **Given** an `ACTIVE` profile, **When** its success rate drops below 80%, **Then**
   rediscovery is triggered.
3. **Given** an `ACTIVE` profile, **When** repeated low confidence (< 0.75), repeated empty
   selector results, repeated 403/429, disappearing currency, unrealistic prices, or
   apparent template change is observed, **Then** rediscovery is triggered.
4. **Given** a periodic light re-check, **When** it runs against active profiles, **Then** it
   can detect degradation and enqueue rediscovery without a full batch having failed.

---

### User Story 5 - Buffer attempt stats and flush atomically without hot-row writes (Priority: P5)

As the platform operating at scale, when thousands of attempts hit a single domain in one job,
I want attempt counters buffered outside the primary store and flushed in atomic batched
updates so that no single stats row becomes a write hot-spot and the scraping runtime is never
blocked.

**Why this priority**: A scale-safety and correctness guarantee that underpins US1/US4's
counting. It is separable (US1 can be tested with direct writes) so it is P5, but it is
non-negotiable for production per the constitution.

**Independent Test**: Simulate many attempts for one `(profile, method_type, method_name)`
key, assert counters accumulate in the buffer with no per-attempt write to the primary stats
row, then trigger a flush and assert a single atomic increment-by-delta per key is applied and
the combined value read by promotion/rediscovery equals persisted value plus pending buffered
delta. Assert no blocking buffer/DB call occurs on the scraping (reactor) thread.

**Acceptance Scenarios**:

1. **Given** N attempts for one method key, **When** they are recorded, **Then** they update
   a buffered counter (atomic increment) and do NOT issue N primary-store writes.
2. **Given** buffered deltas exist, **When** a periodic flush or job finalization runs,
   **Then** each key is persisted with a single atomic `count = count + delta` update.
3. **Given** a flush has not yet run, **When** promotion/rediscovery evaluates a method,
   **Then** it reads persisted counts plus pending buffered deltas so decisions are not stale.
4. **Given** stats recording happens during scraping, **When** attempts are recorded, **Then**
   no blocking counter or database call executes on the reactor thread.

---

### Edge Cases

- **URL pattern ambiguity**: locale-prefixed paths (`/ar/products/<slug>`) must preserve the
  locale and still collapse the slug; ID-like segments (all-digits, UUID-like, long mixed
  alphanumeric, mostly-digits) collapse to `:id`; unknown path shapes derive deterministically.
- **Manual override**: a `url_pattern` override on the profile (or an access-rule override)
  takes precedence over the derived pattern.
- **Algorithm version bump**: when `URL_PATTERN_ALGORITHM_VERSION` changes, existing rows
  carry the old version; lookups must not mix versions, and a backfill re-derives/re-links (or
  re-queues discovery for) affected profiles.
- **Concurrent promotion**: two workers recording successes for the same key must not
  double-promote or corrupt counts; the unique key `(profile_id, method_type, method_name)`
  and atomic updates protect this.
- **Currency required but absent**: a success with a missing required currency does not count
  toward promotion and, if repeated on the preferred method, is a rediscovery signal.
- **Sample size out of bounds**: discovery with <3 or >10 samples is rejected.
- **Workspace isolation**: a query with no workspace context returns zero rows across all
  three tables; cross-workspace access is denied.
- **Profile absent at scrape time**: resolving a strategy for an unseen key returns the
  default ladder and does not error.

## Requirements *(mandatory)*

### Functional Requirements

#### URL pattern grouping (foundation)

- **FR-001**: System MUST provide `derive_url_pattern(url)` that normalizes a URL into a
  stable pattern by: parsing, lowercasing hostname, removing scheme, removing `www.`, removing
  trailing slash, removing fragment, removing query string, preserving locale prefixes (e.g.
  `/ar/`, `/en/`), splitting the path, replacing ID-like segments with `:id`, and replacing
  product-slug segments after known product path keys with `*`.
- **FR-002**: System MUST classify a segment as ID-like when it is all digits, UUID-like, a
  long mixed-alphanumeric ID, or mostly digits.
- **FR-003**: System MUST apply product-path rules: `/products/<slug>→/products/*`,
  `/product/<slug>→/product/*`, `/p/<x>→/p/*`, `/item/<x>→/item/*`,
  `/ar/products/<slug>→/ar/products/*`.
- **FR-004**: System MUST maintain a `URL_PATTERN_ALGORITHM_VERSION` constant and stamp
  `url_pattern_version` on every row that stores a derived pattern (domain strategy profiles;
  and continue to honor the version already stored on competitor matches).
- **FR-005**: Strategy lookups MUST NOT mix patterns from different algorithm versions; a
  version bump MUST be accompanied by a backfill maintenance task that re-derives and re-links
  (or re-queues discovery for) affected profiles.
- **FR-006**: System MUST allow a manual `url_pattern` override (on the profile, and honoring
  an access-rule pattern override) to take precedence over the derived pattern.

#### Strategy profiles & learning

- **FR-007**: System MUST maintain a domain strategy profile uniquely keyed by
  `(workspace_id, competitor_id, domain, url_pattern)`, with a status of `DISCOVERY_REQUIRED`,
  `LEARNING`, `ACTIVE`, `DEGRADED`, or `DISABLED`.
- **FR-008**: System MUST track access methods (`DIRECT_HTTP`, `DIRECT_HTTP_RETRY`,
  `PROXY_HTTP`, `PLAYWRIGHT_PROXY`) and extraction methods (`PLATFORM_PATTERN`, `JSON_LD`,
  `EMBEDDED_JSON`, `CSS_SELECTOR`, `XPATH`, `REGEX`, `PLAYWRIGHT_RENDERED_SELECTOR`)
  separately, with `method_type` of `ACCESS` or `EXTRACTION`.
- **FR-009**: System MUST record per-method attempt statistics keyed uniquely by
  `(domain_strategy_profile_id, method_type, method_name)`, tracking attempt/success/failure
  counts, success rate, and optional average response time, average confidence, and last
  success/failure timestamps. The raw per-attempt signal is sourced from the existing SPEC-07
  spider attempt path (no separate production double-fetch).
- **FR-010**: System MUST promote a method to the profile's preferred method only after 3
  successful extractions across at least 3 different URLs of the same domain+pattern, each
  with confidence ≥ the configured threshold (default 0.85), a valid numeric price, and a
  valid currency when currency is required.
- **FR-011**: On promotion, System MUST set the profile's `preferred_access_method` /
  `preferred_extraction_method` and corresponding `access_confidence` /
  `extraction_confidence`, update `confirmed_success_count`, and move the profile to `ACTIVE`.
- **FR-012**: System MUST record `last_discovery_at`, `last_success_at`, `last_failed_at`, and
  `recent_failure_count` on the profile as attempts are observed.

#### Strategy consumption

- **FR-013**: When resolving a starting strategy for a matched URL, System MUST return the
  profile's preferred access + extraction methods when the profile is `ACTIVE` (or `LEARNING`
  with a preferred method), and otherwise return the default escalation ladder.
- **FR-014**: System MUST NOT apply a learned preference from a `DISABLED` profile.
- **FR-015**: Strategy resolution MUST derive the lookup pattern via `derive_url_pattern` (or
  the manual override) at the current algorithm version and be workspace-scoped.

#### Discovery

- **FR-016**: System MUST support a discovery run over 3–10 sample matched URLs for a
  `(workspace, competitor, domain, url_pattern)` that tests candidate access methods then
  extraction methods and identifies the winning combination. Discovery MUST be triggerable
  both automatically (a new key with no profile is created `DISCOVERY_REQUIRED` and enqueues a
  discovery run) and by explicit operator request; both paths use one discovery-run +
  profile-seed code path.
- **FR-017**: System MUST persist each discovery run (`strategy_discovery_runs`) with
  `sample_size`, `status`, and, on completion, `winning_access_method` /
  `winning_extraction_method` and `completed_at`.
- **FR-018**: On discovery completion, System MUST seed/update the corresponding profile with
  the winning methods and move it out of `DISCOVERY_REQUIRED`; if discovery finds nothing, the
  run and profile MUST reflect that outcome.
- **FR-019**: System MUST reject a discovery request whose sample size is outside 3–10.

#### Rediscovery

- **FR-020**: System MUST trigger rediscovery (and mark the profile `DEGRADED`) when any of:
  3 consecutive failures for the preferred method (tracked via `recent_failure_count`,
  incremented on preferred-method failure and reset to 0 on a qualifying success); per-method
  cumulative success rate below 80% (read from `strategy_attempt_stats.success_rate` plus
  pending buffered deltas); selector returns empty repeatedly; price confidence below 0.75
  repeatedly; repeated 403/429; currency disappears; price values become unrealistic; template
  appears changed.
- **FR-021**: System MUST support a periodic light re-check that evaluates active profiles for
  degradation and enqueues rediscovery without requiring a full failed batch.

#### Atomic buffered stats (scale safety)

- **FR-022**: System MUST buffer per-method attempt counters in an atomic external counter
  store keyed by `(domain_strategy_profile_id, method_type, method_name)` rather than writing
  the primary stats row per attempt.
- **FR-023**: System MUST flush buffered counters to the primary store periodically and at job
  finalization using a single atomic `count = count + delta` update per key (no
  read-modify-write in application code).
- **FR-024**: Promotion and rediscovery decisions MUST read persisted counts plus pending
  buffered deltas so decisions reflect not-yet-flushed activity.
- **FR-025**: Stats recording MUST NOT perform blocking counter or database calls on the
  scraping (reactor) thread.

#### Isolation & integrity (non-negotiable)

- **FR-026**: All three tables MUST carry `workspace_id` (directly, or transitively for
  `strategy_attempt_stats` via its profile) and MUST be protected by row-level security so a
  query with no workspace context returns zero rows and cross-workspace access is denied.
- **FR-027**: System MUST enforce the unique constraints
  `domain_strategy_profiles(workspace_id, competitor_id, domain, url_pattern)` and
  `strategy_attempt_stats(domain_strategy_profile_id, method_type, method_name)`.
- **FR-028**: All identifiers MUST be UUIDv7.

### Key Entities *(include if feature involves data)*

- **Domain Strategy Profile** (`domain_strategy_profiles`): the learned strategy for one
  `workspace + competitor + domain + url_pattern`. Holds status, preferred access/extraction
  methods and their confidences, confirmed-success and recent-failure counts, discovery/
  success/failure timestamps, and `url_pattern_version`. Unique on
  `(workspace_id, competitor_id, domain, url_pattern)`.
- **Strategy Attempt Stats** (`strategy_attempt_stats`): per-method rolling statistics for a
  profile — attempt/success/failure counts, success rate, average response time, average
  confidence, last success/failure. Unique on
  `(domain_strategy_profile_id, method_type, method_name)`.
- **Strategy Discovery Run** (`strategy_discovery_runs`): a record of a discovery execution
  over a URL sample — workspace, competitor, domain, url_pattern, sample_size, status, winning
  access/extraction methods, created/completed timestamps.
- **URL Pattern** (derived value, not a table): the stable grouping key produced by
  `derive_url_pattern`, versioned by `URL_PATTERN_ALGORITHM_VERSION`, stored on profiles and
  on competitor matches; the join key between matches and learned strategies.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After a domain+pattern has produced 3 qualifying successes across ≥3 distinct
  URLs, 100% of subsequent scrapes for that key start from the learned preferred access +
  extraction method instead of the default ladder.
- **SC-002**: Two different product URLs that share a template resolve to the same derived
  pattern in 100% of cases across the documented normalization rules and locale prefixes.
- **SC-003**: Recording attempt stats for a single hot domain of N attempts results in at most
  a small bounded number of primary-store writes per flush interval (independent of N), and
  zero per-attempt primary-store writes.
- **SC-004**: When a preferred method meets any rediscovery condition, the profile is marked
  degraded and rediscovery is enqueued within one evaluation cycle 100% of the time.
- **SC-005**: A query issued without workspace context returns zero rows for all three tables,
  and no cross-workspace row is ever returned.
- **SC-006**: A discovery run over a 3–10 URL sample records a winning combination (or an
  explicit no-winner outcome) and seeds the profile, with sample sizes outside 3–10 rejected
  100% of the time.
- **SC-007**: No stats-recording code path executes a blocking counter or database call on the
  scraping runtime thread (verified by the project's reactor-safety checks).

## Assumptions

- The scraping runtime, `request_attempts` recording, and access-method escalation from
  SPEC-07, the scrape-profile/extraction-rule resolution from SPEC-06, the access-policy/proxy
  layer from SPEC-10, and the competitor-match `url_pattern`/`url_pattern_version` storage from
  SPEC-05 already exist and are reused — this feature layers learning on top of them rather
  than rebuilding fetch/extract.
- The mandated stack (Postgres with RLS, Redis for atomic counters, Twisted-reactor scraping,
  Celery for periodic/rediscovery tasks) from the constitution and §3/§8/§26/§28 applies; the
  external counter store is Redis and the primary store is Postgres.
- The confidence threshold (default 0.85), promotion counts (3 successes / 3 distinct URLs),
  and rediscovery thresholds (80% success rate, 0.75 confidence, 3 consecutive failures) are
  configurable with the documented defaults.
- Discovery may run as an operator-triggered action and/or automatically when a new
  domain+pattern first appears; both paths converge on the same discovery-run + profile-seed
  logic.
- The append-heavy `request_attempts` table already introduced in SPEC-07/SPEC-10 supplies the
  raw attempt signal; this feature's three tables are the learned/rolled-up layer and are not
  themselves high-write partitioned tables.
- Actual live method-testing during discovery exercises the real fetch/extract pipeline; where
  a live Postgres/Redis/Scrapyd/browser is unavailable in the build environment, those checks
  are authored as integration tests that skip cleanly (consistent with prior specs).
