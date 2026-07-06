# Feature Specification: Webhook Events

**Feature Branch**: `016-webhook-events`

**Created**: 2026-07-06

**Status**: Draft

**Input**: User description: "SPEC-16 — Webhook Events. Integration readiness: webhook endpoint registration with URL-safety validation, workspace-scoped event records created on domain state changes (alerts, jobs, strategy), and a poll-based event API with pagination. No automatic delivery in v1."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Poll workspace events for integration (Priority: P1)

An external integration operator holds an API key with read access and wants to keep a downstream
system in sync with what is happening inside a workspace. They periodically call the event-polling
API, receive a page of events newest-or-oldest first with a stable cursor, and advance through the
backlog without missing or double-reading events. Each event carries a type, a JSON payload
describing what changed, a status, and a creation timestamp. They can also fetch a single event by
its id to inspect its full payload.

**Why this priority**: Polling is the whole point of "integration readiness" in v1 — the acceptance
criteria are "events are created" and "external systems can poll events". Without the poll API the
feature delivers no external value, so this is the MVP slice. It is independently valuable even if
endpoint registration (P2) does not exist yet, because events are created workspace-wide regardless
of any registered endpoint.

**Independent Test**: Seed a handful of events in a workspace (directly or by triggering domain
changes), call the list endpoint with a page size, walk the pages using the returned cursor to the
end, and confirm every seeded event is returned exactly once, ordered deterministically, and that a
single-event fetch returns the matching payload. Confirm a caller from another workspace sees none
of them.

**Acceptance Scenarios**:

1. **Given** a workspace with N events, **When** the operator lists events with page size P (P < N),
   **Then** they receive P events plus a cursor, and following the cursor to exhaustion yields all N
   events exactly once with no duplicates or gaps, in a deterministic order.
2. **Given** an event id that exists in the caller's workspace, **When** the operator fetches that
   event by id, **Then** the full event (type, payload, status, created_at, delivered_at) is returned.
3. **Given** an event that belongs to a different workspace, **When** the operator fetches or lists,
   **Then** that event is never returned (404 on direct fetch by id; absent from the list).
4. **Given** a read-only API key (webhooks:read), **When** the operator calls the list or get event
   endpoints, **Then** the request is authorized; a key lacking webhooks:read is rejected.
5. **Given** the list endpoint supports filtering, **When** the operator filters by event_type,
   **Then** only events of that type in their workspace are returned, still paginated deterministically.

---

### User Story 2 - Register and manage webhook endpoints safely (Priority: P2)

A workspace administrator registers the URLs where webhooks will eventually be delivered, names each
endpoint, marks which event types it is interested in, and enables or disables it. Because an internal
service will one day connect to these URLs, every URL is validated against SSRF-safe rules at save
time: only http/https, only public hosts, no private/loopback/link-local/metadata targets, and no
embedded credentials. The administrator can list, update, and delete their endpoints. In v1 no
delivery occurs — endpoints record subscription intent only.

**Why this priority**: Endpoint registration is required for the eventual delivery feature and is part
of the stated scope, but it is not needed to satisfy the v1 acceptance criteria (events are created
and can be polled regardless of any registered endpoint). It is a fully independent CRUD slice.

**Independent Test**: Create an endpoint with a public https URL and verify it round-trips; attempt to
create endpoints with a private/loopback/metadata/userinfo URL and verify each is rejected with a
validation error; update the name/enabled/event_types and verify persistence; delete and verify it is
gone. Verify another workspace cannot see or mutate these endpoints.

**Acceptance Scenarios**:

1. **Given** a valid public https URL, **When** the admin creates an endpoint, **Then** it is stored
   and returned with its id, name, url, enabled flag, and event_types.
2. **Given** a URL whose host is private, loopback, link-local, a cloud metadata address, or an
   internal hostname, or a URL containing userinfo (user:pass@host) or a non-http(s) scheme, **When**
   the admin tries to create or update an endpoint with it, **Then** the request is rejected with a
   validation error and nothing is persisted.
3. **Given** an existing endpoint, **When** the admin updates its name, enabled flag, or event_types,
   **Then** the changes persist and updated_at advances.
4. **Given** an existing endpoint, **When** the admin deletes it, **Then** it no longer appears in the
   list and a direct fetch returns 404.
5. **Given** an endpoint owned by another workspace, **When** an admin lists/updates/deletes, **Then**
   it is never visible or mutable (404 / absent).
6. **Given** write operations, **When** the caller lacks the webhooks:write scope, **Then** create,
   update, and delete are rejected; webhooks:read alone permits only listing/reading endpoints.

---

### User Story 3 - Events are automatically created on domain changes (Priority: P2)

When a meaningful domain change happens inside a workspace — a price alert transitions state, a scrape
job changes status, or a domain strategy changes — the system records a corresponding webhook event in
that workspace so that pollers observe it. Event creation is decoupled from the originating operation
(it happens on a dedicated background queue) so it never blocks or fails the source action, and it is
resilient to duplication.

**Why this priority**: "Events are created" is an explicit acceptance criterion, and without automatic
creation the poll API only ever returns manually-seeded rows. It is grouped as P2 alongside endpoint
management because the poll API (P1) is demonstrable with seeded events, but this story is what makes
the feature live in production.

**Independent Test**: Trigger each source change (alert transition, job status change, strategy change)
in a workspace and confirm exactly one event of the expected type with a descriptive payload appears
in that workspace's event list, attributed to the correct workspace, and that re-triggering an
identical source signal does not create contradictory or malformed duplicates.

**Acceptance Scenarios**:

1. **Given** a price alert transitions (e.g. created/updated/resolved/reopened) for a variant, **When**
   the transition is committed, **Then** a webhook event of an alert-related type is created in that
   workspace with a payload identifying the variant and the transition.
2. **Given** a scrape job reaches a terminal or notable status (e.g. completed / partial / failed),
   **When** that status is set, **Then** a webhook event of a job-related type is created in that
   workspace with a payload identifying the job and status.
3. **Given** a domain strategy change occurs, **When** it is committed, **Then** a webhook event of a
   strategy-related type is created in that workspace with a payload identifying the strategy change.
4. **Given** event creation runs on a background queue, **When** the source operation commits, **Then**
   the source operation's success does not depend on event creation succeeding, and event creation
   failure is retried/handled without corrupting the source domain state.

---

### Edge Cases

- **Empty workspace / first poll**: Listing events in a workspace with no events returns an empty page
  and a cursor that signals end-of-data; it is not an error.
- **Pagination past the end**: Following a cursor beyond the last event returns an empty page and a
  terminal cursor; a stale/invalid cursor is rejected with a validation error rather than silently
  returning wrong data.
- **Events spanning a month boundary**: Because events are stored in monthly partitions, a page and its
  ordering must remain correct and stable when it spans two adjacent months.
- **Polling during retention drop**: When old monthly partitions are dropped by the maintenance job,
  polling continues to work for the remaining (in-retention) events; already-advanced pollers are not
  broken by the disappearance of expired events.
- **Endpoint URL targeting an internal destination**: At save time the URL is rejected when its scheme
  is not http/https, when its host is an IP *literal* in a private/loopback/link-local/reserved/
  unique-local/metadata range, when its host is a known internal hostname or internal-suffix
  (localhost, *.internal, *.local, cloud-metadata host, docker-compose service names), or when it
  embeds userinfo. Save-time validation does NOT perform DNS resolution (consistent with the reused
  validator and the master doc's two-phase model): a *public-looking hostname whose DNS resolves into a
  private range* is caught later at delivery time by the future dispatcher's re-resolution, which is out
  of v1 scope. This matches how competitor match URLs are handled (save-time string/literal check;
  fetch-time DNS re-validation).
- **event_types on an endpoint referencing an unknown type**: Recording subscription intent for a type
  string that no source currently emits is permitted (forward-compatible) but does not cause events to
  be created for it.
- **No-context / system session**: A request with no workspace context returns zero rows for both
  endpoints and events (fail-closed), never another workspace's data.
- **Very large payload**: An event payload is bounded to a reasonable size; the source producing it
  must not be able to store an unbounded blob.

## Requirements *(mandatory)*

### Functional Requirements

**Webhook endpoints**

- **FR-001**: System MUST let an authorized user register a webhook endpoint carrying a name, a URL, an
  enabled flag, an optional list of subscribed event types, and an optional stored secret placeholder.
- **FR-002**: System MUST validate a webhook endpoint URL at save time (create and update) against
  SSRF-safe rules: scheme MUST be http or https; the system MUST reject a host that is an IP *literal*
  in a non-public range — private (10/8, 172.16/12, 192.168/16), loopback, link-local (169.254/16,
  fe80::/10), unique-local (fc00::/7), reserved/multicast/unspecified, and cloud-metadata addresses —
  MUST reject known internal service hostnames and internal-suffix hosts (localhost, cloud-metadata
  host, docker-compose service names, `*.internal`/`*.local`/`*.localhost`), and MUST reject URLs
  containing userinfo (user:pass@host). Save-time validation is a string/literal check and does NOT
  perform DNS resolution; authoritative DNS re-resolution of a hostname against the same deny rules is
  a delivery-time control performed by the future dispatcher (out of v1 scope), mirroring the master
  doc's two-phase SSRF model (§11) and the existing competitor-URL save-time validator.
- **FR-003**: System MUST reuse the existing SSRF/URL-safety validation used for competitor match URLs
  rather than introducing a second, divergent validator.
- **FR-004**: Users MUST be able to list, retrieve, update, and delete webhook endpoints within their
  workspace; update MUST support changing name, url (re-validated), enabled, event_types, and secret;
  updated_at MUST advance on update.
- **FR-005**: System MUST store the endpoint secret in an encrypted/non-plaintext-exposed form and MUST
  NOT return the raw secret in API responses. In v1 the secret is stored but unused.

**Webhook events**

- **FR-006**: System MUST persist webhook events with: id, workspace_id, event_type, JSON payload,
  status, created_at, and a nullable delivered_at.
- **FR-007**: System MUST store webhook events in a table that is monthly-partitioned by created_at
  from creation ("born partitioned"); the table's primary key MUST include the partition key
  (created_at). The table MUST NOT be created plain and converted later.
- **FR-008**: System MUST create a webhook event on notable domain state changes: price alert-state
  transitions, scrape job status changes, and domain strategy changes. Each event's payload MUST
  identify the affected entity and the nature of the change, and the event MUST be attributed to the
  originating workspace.
- **FR-009**: Event creation MUST be decoupled from the originating operation via a dedicated
  background queue so that creating an event never blocks or fails the source operation; a failure to
  create an event MUST NOT roll back or corrupt the source domain state, and creation work MUST be
  retriable.
- **FR-010**: In v1 the system MUST NOT perform any automatic delivery, dispatch, retry-of-delivery, or
  signing of webhook events. delivered_at remains null and status reflects a not-yet-delivered state.
- **FR-011**: Newly created events MUST default to a status indicating they have been recorded but not
  delivered.

**Polling API**

- **FR-012**: Users MUST be able to poll (list) webhook events in their workspace through a paginated
  endpoint, and retrieve a single event by id.
- **FR-013**: The list endpoint MUST return results in a deterministic, stable order and provide a
  pagination mechanism (cursor or equivalent) that lets a caller walk the entire backlog exactly once
  with no duplicates and no gaps, including when results span monthly partition boundaries.
- **FR-014**: The list endpoint MUST support at least filtering by event_type, and MUST bound page size
  to a sane maximum with a documented default.
- **FR-015**: Fetching an event id that does not exist in the caller's workspace MUST return not-found;
  an invalid/stale pagination cursor MUST return a validation error rather than incorrect data.

**Isolation & authorization**

- **FR-016**: All webhook endpoint and webhook event access MUST be workspace-scoped with row-level
  security AND application-level scoping; cross-workspace reads and writes MUST be denied and a session
  with no workspace context MUST return zero rows (fail-closed).
- **FR-017**: Read operations (list/get endpoints, list/get events) MUST require the webhooks:read
  scope; create/update/delete of endpoints MUST require the webhooks:write scope. These scopes MUST be
  registered in the platform's scope catalog so they are grantable to API keys.

**Retention & maintenance**

- **FR-018**: Webhook events MUST have a default retention of 90 days, enforced by monthly partition
  drop (never bulk DELETE), performed by the existing maintenance job — this feature MUST register the
  webhook_events table into the existing partition-create and retention registry rather than adding a
  new maintenance job or scheduler.
- **FR-019**: Other tables MUST reference webhook events only by soft reference (no hard foreign key
  into the partitioned events table), consistent with the platform's partitioned-table rules; readers
  MUST tolerate references into dropped (expired) partitions.

### Key Entities *(include if feature involves data)*

- **Webhook Endpoint**: A workspace-owned registration of where webhooks will eventually be delivered.
  Attributes: id, workspace, name, url (SSRF-validated), encrypted secret (optional, unused in v1),
  enabled flag, subscribed event_types (list), created/updated timestamps. Records subscription intent
  only in v1; no delivery occurs.
- **Webhook Event**: A workspace-owned, immutable-once-created record of a domain change available for
  polling. Attributes: id, workspace, event_type, JSON payload (bounded), status (recorded /
  not-delivered in v1), created_at (partition key), nullable delivered_at. Stored in a monthly
  partitioned table with 90-day retention. Referenced by soft reference only.
- **Event Type**: A stable string categorizing an event by its source domain change (alert-related,
  job-related, strategy-related). Used both to categorize created events and to record an endpoint's
  subscription interest.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An external integrator can retrieve every event in a workspace exactly once by walking
  the paginated poll API from start to finish, with zero duplicates and zero omissions, across a
  dataset that spans at least two monthly partitions.
- **SC-002**: 100% of webhook endpoint URLs whose host is a non-public IP *literal* (private, loopback,
  link-local, unique-local, reserved, cloud-metadata), a known internal hostname/suffix, or that carry
  userinfo or a non-http(s) scheme, are rejected at save time; 100% of valid public http/https URLs are
  accepted. (DNS-resolution-based rejection of public-looking hostnames is a delivery-time control,
  out of v1 scope.)
- **SC-003**: Each of the three source domain changes (alert transition, job status change, strategy
  change) results in exactly one corresponding webhook event in the correct workspace, with a payload
  that identifies the affected entity.
- **SC-004**: A caller in one workspace can retrieve zero events or endpoints belonging to any other
  workspace, and a request without workspace context returns zero rows — verified for both tables.
- **SC-005**: Creating a webhook event never blocks the source operation and never leaves the source
  domain state inconsistent when event creation fails; the source operation succeeds independently.
- **SC-006**: Webhook events older than 90 days are removed by monthly partition drop through the
  existing maintenance job, with no new scheduler or maintenance job introduced, and polling of
  in-retention events is unaffected by the drop.
- **SC-007**: No automatic delivery occurs in v1: for every created event, delivered_at is null and no
  outbound HTTP request is made by the system.

## Assumptions

- **Reuse of existing SSRF validator**: The URL-safety validation from competitor match URLs
  (SPEC-05/07) already exists and is reused verbatim for webhook endpoint URLs; no new validator is
  written. Delivery-time re-validation is deferred with the delivery feature (out of v1 scope).
- **Reuse of SPEC-15 maintenance machinery**: Partition creation and 90-day retention for
  webhook_events are handled by the existing maintenance registry/jobs from SPEC-15; this feature only
  registers the table. No new Celery beat/scheduler entry is added.
- **Reuse of partitioning conventions**: webhook_events follows the same born-partitioned monthly
  pattern (PK includes created_at, soft references only) already used by price_observations,
  request_attempts, and price_alert_events.
- **Dedicated event-creation queue exists**: A Celery `webhook_events` queue is defined in the platform
  (Section 26) and is used for event creation; in v1 it only creates/persists events (no delivery).
- **Event creation hooks into existing domain transitions**: Alert-state transitions (SPEC-09 price
  analysis), scrape job status changes (SPEC-08), and strategy changes (SPEC-12) already have commit
  points where an event-creation task can be enqueued; this feature adds those enqueue calls.
- **Events are workspace-wide, not endpoint-filtered, in v1**: Because there is no delivery, events are
  created for the workspace regardless of which endpoints (if any) subscribe; an endpoint's event_types
  list records intent for the future delivery feature and does not gate event creation.
- **Pagination model**: Cursor/keyset pagination over (created_at, id) is assumed for stable ordering
  across partitions; the exact query-parameter shape follows existing list endpoints in the platform.
- **Secret handling**: The secret column is stored encrypted-at-rest and never returned raw, following
  the same convention as other encrypted secrets in the platform (e.g. proxy credentials); it is unused
  in v1.
- **uuidv7 ids**: Event and endpoint ids use uuidv7 as elsewhere in the platform; event id alone is not
  the sole PK for the partitioned events table (PK includes created_at).
