# Feature Specification: Access Policies, Proxies & Request Attempts

**Feature Branch**: `010-access-policies-proxies`

**Created**: 2026-07-04

**Status**: Draft

**Input**: User description: "SPEC-10 — Access Policies, Proxies, Request Attempts (crawmatic increment 10). Controlled access behavior for scraping: decide direct vs proxy per request, store proxy provider config with encrypted credentials, apply per-domain access overrides, and log every scrape request attempt to an append-heavy partitioned table."

## Clarifications

### Session 2026-07-04

All items below were resolved from the master project spec (`PROJECT_SPEC.md`); none required
operator input. Recorded here to bind the spec to the canonical decisions.

- Q: What are the allowed `access_method` values recorded on a request attempt / chosen by a
  policy? → A: `DIRECT_HTTP`, `DIRECT_HTTP_RETRY`, `PROXY_HTTP`, `PLAYWRIGHT_PROXY` (doc §11); no
  external scraping APIs.
- Q: How does an access strategy map to an attempt sequence for an unknown domain? → A: the
  fallback chain is Direct HTTP → Direct HTTP retry (backoff) → HTTP via internal proxy pool →
  internal Playwright via proxy → fail (needs tuning); learned domains start from the preferred
  learned method instead of attempt 1 (doc §11).
- Q: What encryption mechanism protects proxy credentials, and how is rotation handled? → A:
  Fernet symmetric encryption with the key in an environment variable and a `key_version` column;
  rotation supports decrypting old key versions, re-encrypting records, then retiring the old key
  (doc §33).
- Q: What proxy-provider `status` values are used? → A: default to `ACTIVE` / `DISABLED` (doc
  silent on the enum; low-impact, deferred detail confirmable in planning).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Configure how competitor sites are accessed (Priority: P1)

An operator managing a workspace needs to describe *how* the system should reach competitor
websites: whether to go direct or through a proxy, how many times to retry, which proxy
provider and country to use, and what per-minute/hour/day request ceilings apply. They create
one or more named **access policies** and, optionally, register **proxy providers** whose
credentials must be stored securely. Both global (system-provided) defaults and
workspace-owned entries exist; a workspace only ever sees the global defaults plus its own
entries.

**Why this priority**: Nothing else in this feature is usable without the configuration that
describes access behavior. An access policy is the unit that every scrape consults to decide
direct-vs-proxy. This slice alone delivers value: an operator can express and store access
strategy and proxy credentials safely, workspace-isolated, ready for the engine to consume.

**Independent Test**: Create an access policy and a proxy provider through the API; confirm the
policy round-trips with all strategy/retry/rate fields, the proxy password is never returned in
plaintext and is unreadable at rest, and a second workspace cannot see or mutate the first
workspace's entries (and sees system-global entries read-only).

**Acceptance Scenarios**:

1. **Given** an authenticated workspace, **When** it creates an access policy with a strategy,
   retry count, proxy-usage flags, and rate ceilings, **Then** the policy is persisted and
   retrievable with every field intact.
2. **Given** a proxy provider is created with a username and password, **When** the record is
   stored and later read back, **Then** the password is encrypted at rest and never exposed in
   plaintext through the API.
3. **Given** two workspaces, **When** workspace B lists or fetches access policies/proxy
   providers, **Then** it sees only its own entries plus system-global defaults and can neither
   read nor modify workspace A's tenant-owned entries.
4. **Given** no workspace context is set, **When** the access-config tables are queried, **Then**
   zero tenant rows are returned.
5. **Given** a proxy provider `base_url` that resolves to a private/loopback/metadata address or
   carries embedded credentials, **When** it is saved, **Then** the request is rejected with a
   validation error.

### User Story 2 - Drive direct-vs-proxy behavior during a scrape (Priority: P2)

When a scrape runs for a match, the system must resolve the **effective access policy** for that
target and then carry out the access strategy: try direct first or proxy first per the policy,
retry on failure up to the configured limit, switch to proxy on retry when configured, honor
rate ceilings and per-domain cooldowns, and stop (or fall back) according to the strategy. A
per-domain access rule, when one matches the target domain/URL pattern, overrides the
competitor/workspace default policy.

**Why this priority**: This is the behavioral core the acceptance criteria call out — "access
policy controls direct/proxy behavior" and "domain access rule overrides competitor/workspace
defaults." It turns stored configuration into runtime decisions and integrates with the
existing scrape spider and job orchestration.

**Independent Test**: For a given match/domain, resolve the effective policy and assert the
chosen access method sequence (e.g. DIRECT_THEN_PROXY → first attempt direct, retry via proxy);
add a matching domain access rule and assert it overrides the workspace default; assert rate
ceilings and cooldown are respected.

**Acceptance Scenarios**:

1. **Given** a policy with strategy DIRECT_THEN_PROXY and max_retries ≥ 1, **When** a scrape's
   first (direct) attempt fails, **Then** the retry is issued through the configured proxy.
2. **Given** a policy with strategy DIRECT_ONLY, **When** a scrape runs, **Then** no proxy is
   ever used regardless of failure.
3. **Given** a domain access rule matching the target domain (and URL pattern, if set), **When**
   the effective policy is resolved, **Then** the domain rule's policy/overrides win over the
   competitor/workspace default.
4. **Given** a policy with per-minute/hour/day ceilings, **When** those ceilings are exceeded for
   the domain, **Then** further attempts are deferred/blocked and reported as RATE_LIMITED.
5. **Given** a proxy provider whose monthly budget is exhausted, **When** the policy would use it,
   **Then** the system falls back per the policy strategy, or fails with LIMIT_REACHED when no
   fallback is available.

### User Story 3 - Record every request attempt for audit and tuning (Priority: P3)

Every attempt to fetch a target — direct or proxied, success or failure — must be logged as a
**request attempt** capturing the URL, access method, proxy provider/country used, HTTP status,
response time, success flag, and a structured error code/message. These rows accumulate quickly
(millions per month per workspace), so they live in a table that is partitioned monthly from
creation. The data feeds debugging, access-policy tuning, and later strategy optimization.

**Why this priority**: "Each scrape logs request attempts" is an explicit acceptance criterion,
and without attempt records there is no way to observe or tune access behavior. It depends on
the access engine (US2) producing attempts, so it follows it, but it is independently verifiable
by asserting rows are written with correct fields and partitioning.

**Independent Test**: Run a scrape that makes N attempts (mix of direct/proxy, success/failure)
and assert N request-attempt rows are persisted with correct access_method, proxy fields,
status, timing, success, and error_code; confirm the table is monthly-partitioned from birth and
that writes are batched and occur off the scraping reactor thread.

**Acceptance Scenarios**:

1. **Given** a scrape that makes a direct attempt and a proxied retry, **When** it completes,
   **Then** two request-attempt rows exist with matching attempt_number, access_method, and (for
   the retry) the proxy provider/country recorded.
2. **Given** an attempt that fails with a blocked/timeout/proxy error, **When** it is logged,
   **Then** the row carries the corresponding structured error code (e.g. BLOCKED, TIMEOUT,
   PROXY_FAILED) and status/response-time as available.
3. **Given** attempts spanning a month boundary, **When** they are written, **Then** each lands in
   the correct monthly partition and the primary key includes the partition key.
4. **Given** a workspace context, **When** request attempts are queried, **Then** only that
   workspace's rows are visible; with no context, zero rows.

### Edge Cases

- Proxy provider referenced by an access policy is later disabled or deleted → policy resolution
  must degrade gracefully (fall back per strategy or fail with a clear error), not crash a scrape.
- A domain access rule is disabled (`enabled = false`) → it is ignored and the default policy
  applies.
- Multiple domain access rules match the same target (e.g. one domain-only, one with a URL
  pattern) → the most specific match (URL-pattern rule) wins.
- `max_retries = 0` → exactly one attempt, no retry, regardless of failure.
- Rate ceilings and per-domain concurrency limits interact with the distributed rate limiter
  introduced later (spec 011) → this spec enforces the policy's own ceilings/cooldown; the
  cluster-wide limiter is out of scope here.
- Encryption key rotation → stored proxy passwords must remain decryptable across a defined key
  version; unreadable/missing key surfaces as an operational error, never plaintext leakage.
- A request attempt references a scrape_job/match whose partition was later dropped by retention
  → soft references (no FK) tolerate dangling; readers must not assume the referent still exists.

## Requirements *(mandatory)*

### Functional Requirements

#### Access configuration (US1)

- **FR-001**: System MUST let a workspace create, read, update, and delete **access policies**
  with: name, strategy (one of DIRECT_ONLY, DIRECT_THEN_PROXY, PROXY_FIRST, RESIDENTIAL_ONLY,
  BROWSER_FALLBACK), optional provider and country, the proxy-usage flags
  (use_proxy_on_first_attempt, use_proxy_on_retry, allow_browser_fallback), max_retries,
  rotate_per_request, sticky_session, optional session TTL, optional per-minute/hour/day request
  ceilings, and a timeout.
- **FR-002**: System MUST let a workspace register **proxy providers** with: name, type
  (DATACENTER, RESIDENTIAL, MOBILE), base_url, optional username, optional password, optional
  country, status, and optional monthly budget limit.
- **FR-003**: System MUST store proxy provider passwords **encrypted at rest** using Fernet
  symmetric encryption (key supplied via environment variable) with a `key_version` recorded per
  encrypted field, and MUST never return the plaintext password through any API response.
  Rotation MUST support decrypting prior key versions, re-encrypting records, then retiring the
  old key.
- **FR-004**: System MUST let a workspace define **domain access rules** binding a
  competitor + domain (and optional URL pattern) to an access policy, with per-domain
  max_concurrent_requests, max_requests_per_minute, cooldown_seconds, optional block-detection
  rules, an optional URL-pattern override, and an enabled flag.
- **FR-005**: System MUST validate every proxy `base_url` (and any URL the system will connect to)
  against SSRF rules: http/https only, public host only, rejecting localhost, private ranges,
  loopback, link-local, unique-local, cloud metadata endpoints, internal hostnames, and embedded
  userinfo — at save time, and re-validated at fetch time (re-resolve DNS, re-validate each
  redirect hop).
- **FR-006**: System MUST scope all three configuration tables and request attempts by workspace:
  a null workspace denotes a system-global default visible read-only to all workspaces; a
  non-null workspace denotes tenant-owned data. Cross-workspace read/write MUST be denied and a
  query with no workspace context MUST return zero tenant rows.

#### Access behavior resolution (US2)

- **FR-007**: System MUST resolve the **effective access policy** for a scrape target by
  precedence: a matching enabled domain access rule (most specific: URL-pattern match over
  domain-only) overrides the competitor/workspace default policy.
- **FR-008**: System MUST carry out the resolved strategy when accessing a target using the
  allowed access methods (DIRECT_HTTP, DIRECT_HTTP_RETRY, PROXY_HTTP, PLAYWRIGHT_PROXY): choose
  direct vs proxy for the first attempt and for retries per the policy flags and strategy, retry
  on failure up to max_retries, and stop or fall back per the strategy (including browser/
  Playwright-via-proxy fallback when allow_browser_fallback and the strategy permit). For an
  unknown domain the default sequence is DIRECT_HTTP → DIRECT_HTTP_RETRY → PROXY_HTTP →
  PLAYWRIGHT_PROXY → fail; a learned domain starts from its preferred access method.
- **FR-009**: System MUST assign a proxy to an outgoing scrape request when the resolved policy
  calls for it, selecting the provider/country from the policy (or domain rule), honoring
  rotation vs sticky-session behavior.
- **FR-010**: System MUST enforce the proxy provider's monthly budget from cheap Redis usage
  counters (incremented per proxied request, reset monthly) and MUST NOT enforce it by counting
  request_attempts rows; on budget exhaustion it MUST fall back per the policy strategy or fail
  with LIMIT_REACHED.
- **FR-011**: System MUST enforce the policy's per-minute/hour/day ceilings and the domain rule's
  cooldown/concurrency limits, deferring or blocking attempts that would exceed them and reporting
  RATE_LIMITED where appropriate.

#### Attempt logging (US3)

- **FR-012**: System MUST log a **request attempt** for every fetch attempt (direct or proxied,
  success or failure) capturing workspace, scrape_job, match, attempt_number, url, access_method,
  proxy_provider and proxy_country (when proxied), status_code, response_time_ms, success,
  error_code, error_message, and created_at. `access_method` MUST be one of DIRECT_HTTP,
  DIRECT_HTTP_RETRY, PROXY_HTTP, PLAYWRIGHT_PROXY.
- **FR-013**: System MUST classify attempt failures using the structured error codes
  (PROXY_FAILED, RATE_LIMITED, HTTP_429, HTTP_403, TIMEOUT, DNS_ERROR, BLOCKED, LIMIT_REACHED,
  UNKNOWN_ERROR).
- **FR-014**: The request-attempts store MUST be created **monthly-partitioned from birth** (by
  created_at), with a primary key that includes the partition key, and MUST reference other tables
  by soft reference (plain UUID, no foreign key).
- **FR-015**: Attempt-log writes MUST occur off the scraping reactor thread and be batched, so
  logging never blocks or serializes the fetch path.

### Key Entities *(include if feature involves data)*

- **Access Policy**: A named, workspace-scoped (or global) description of access strategy —
  direct/proxy sequencing, retry count, proxy provider/country preference, rotation/session
  behavior, and request-rate ceilings and timeout — consulted by every scrape.
- **Proxy Provider**: A workspace-scoped (or global) proxy endpoint with type, base URL, and
  encrypted credentials, an operational status, an optional country, and an optional monthly
  budget enforced via Redis counters.
- **Domain Access Rule**: A workspace-scoped override binding a competitor + domain (optionally a
  URL pattern) to an access policy, with per-domain concurrency/rate/cooldown limits, optional
  block-detection rules, and an enabled flag; it takes precedence over the default policy.
- **Request Attempt**: An append-only, monthly-partitioned record of a single fetch attempt —
  which URL, via what access method and proxy, the resulting status/timing, success, and
  structured error — used for debugging, access-policy tuning, and later strategy optimization.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can define an access policy that determines direct-vs-proxy behavior,
  and a scrape run for a target governed by that policy demonstrably follows the configured
  sequence (e.g. direct first, proxied retry) in 100% of runs.
- **SC-002**: Every fetch attempt produces exactly one request-attempt record — for a scrape that
  makes N attempts, exactly N records are written, each with correct access method, proxy usage,
  outcome, and error classification.
- **SC-003**: Proxy provider credentials are never observable in plaintext through any API
  response or at rest; a review of stored records and API outputs finds zero plaintext passwords.
- **SC-004**: A domain access rule overrides the competitor/workspace default for its
  domain/URL pattern in 100% of resolutions where the rule is enabled and matches.
- **SC-005**: Workspace isolation holds for all access-config and attempt data — no workspace can
  read or mutate another workspace's tenant-owned rows, and a no-context query returns zero rows.
- **SC-006**: The request-attempts store sustains millions of rows per workspace per month via
  monthly partitioning without a table rewrite, and attempt logging does not block the scrape
  fetch path (writes are batched and off the reactor thread).

## Assumptions

- The existing workspace/auth model, app-level scoping, and Postgres RLS pattern from specs
  002/003 are reused for all four entities; this spec adds tables and behavior, not a new
  isolation mechanism.
- Access-policy resolution and proxy assignment integrate with the existing Scrapyd HTTP spider
  (spec 007) and jobs orchestration (spec 008); browser fallback wiring beyond signaling intent
  is realized by the browser scraping service (spec 014) and is out of scope here except for the
  policy flag/strategy that expresses it.
- The cluster-wide distributed domain rate limiter and in-flight locks (spec 011) are out of
  scope; this spec enforces only the policy's own rate ceilings, cooldown, and per-domain
  concurrency intent.
- Proxy credential encryption uses an application-managed symmetric key (with a key version to
  allow rotation); key management/provisioning is an operational concern configured via the
  established settings mechanism.
- Monthly proxy-budget counters live in Redis, consistent with the master doc's hot-path
  guidance; approximate counting under contention is acceptable (budget is a soft ceiling with a
  defined fallback).
- "Direct retry" and "proxy HTTP" in the doc refer to attempt sequencing within a single scrape
  governed by the access policy, not a separate transport implementation.
