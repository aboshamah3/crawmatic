# Feature Specification: Jobs & Orchestration

**Feature Branch**: `008-jobs-orchestration`

**Created**: 2026-07-03

**Status**: Draft

**Input**: User description: "Jobs & Orchestration (SPEC-08). API triggers scraping through the worker and Scrapyd."

## Clarifications

### Session 2026-07-03

- Q: What `type` and `source` do the direct API run-match/run-variant endpoints record? → A: `type = MANUAL`, `source = API` — these are operator-initiated on-demand runs; `API_TRIGGERED` is reserved for programmatic/plugin triggers, `SCHEDULED` for the scheduler (later spec).
- Q: How does a variant (or other scoped) run with zero active matches resolve? → A: Create the job, set `total_targets = 0`, dispatch nothing, and immediately finalize it as `COMPLETED` (not rejected) so the run is observable and idempotent.
- Q: What is the stall timeout value for re-dispatch, and where does the idempotency guard live (Redis vs DB unique row)? → A: Deferred to planning — these are configuration / implementation choices; the binding requirements are "detect + re-dispatch past a configured timeout" and "duplicate dispatch never double-runs a batch."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Run a single match and observe its job (Priority: P1)

A workspace operator wants to scrape the current price for one competitor match. They call the "run match" endpoint with a match id. The system creates a durable job record, enqueues background dispatch work, and returns the job id immediately (the scrape itself runs asynchronously in the Scrapyd spider). The operator then polls the job status endpoint until the job reports a terminal state, and fetches the per-target results.

**Why this priority**: This is the minimal end-to-end orchestration slice — API → job record → background dispatch → Scrapyd schedule call. Without it, nothing downstream (variant runs, scheduler, alerting) can trigger scraping. It is the core of the MVP vertical slice and is independently demonstrable.

**Independent Test**: Call the run-match endpoint for a valid match; assert a job and exactly one target are created (scoped to the caller's workspace), that a dispatch task is enqueued, that the worker performs an authenticated Scrapyd `schedule.json` call carrying `workspace_id`, `scrape_job_id`, and `match_ids`, and that job status/results can be fetched and reflect target outcomes.

**Acceptance Scenarios**:

1. **Given** an active match in my workspace, **When** I POST to run that match, **Then** a ScrapeJob (type reflecting the trigger, scope MATCH) and exactly one ScrapeJobTarget are created with my workspace_id, the job is PENDING, and the response returns the job id.
2. **Given** a created run-match job, **When** the dispatch task runs, **Then** the worker groups the target into a batch and issues one authenticated Scrapyd schedule call carrying workspace_id, scrape_job_id, and the match_ids, and the job transitions toward RUNNING.
3. **Given** a running job, **When** I GET the job status, **Then** I receive the job's status, counts (total/success/failure/skipped), and timestamps; **When** I GET the job results, **Then** I receive each target with its status and error_code (if any).
4. **Given** a match id that does not exist in my workspace, **When** I POST to run it, **Then** the request is rejected (not found) and no job is created.
5. **Given** the dispatch task is delivered more than once (retry/at-least-once delivery), **When** it runs again for the same job, **Then** the idempotent dispatch guard prevents a second Scrapyd run for the same batch (no double scheduling).

---

### User Story 2 - Run all active matches for a variant (Priority: P2)

An operator wants to refresh all competitor prices for one product variant. They call the "run variant" endpoint. The system finds every active match for that variant, creates one job with a unique target per match, and dispatches the matches in domain/mode-grouped batches rather than one Scrapyd job per URL.

**Why this priority**: Variant-level runs are the first fan-out case and the unit the downstream price-analysis/alerting layer operates on. It proves batching and de-duplication of targets. It builds directly on P1's dispatch path.

**Independent Test**: Seed a variant with several active matches (and at least one inactive match); call run-variant; assert one job is created with exactly one target per *active* match (unique per (job, match)), that batching groups targets by domain/mode within Scrapyd batch-size bounds, and that status/results aggregate across all targets.

**Acceptance Scenarios**:

1. **Given** a variant with N active matches, **When** I POST to run that variant, **Then** one ScrapeJob (scope VARIANT) is created with exactly N unique targets, one per active match, and total_targets = N.
2. **Given** a variant with both active and inactive matches, **When** I run the variant, **Then** only active matches become targets.
3. **Given** a variant whose active matches span multiple domains and scrape modes, **When** dispatch runs, **Then** targets are grouped into batches by domain and mode (HTTP batch sizing in the 50–200 range), not one Scrapyd job per match.
4. **Given** a variant with zero active matches, **When** I run the variant, **Then** the job is created and immediately finalized as COMPLETED with total_targets = 0, without dispatching anything.

---

### User Story 3 - Accurate job lifecycle and counters under scale (Priority: P3)

An operator runs a large job (many targets across many domains). As spiders complete targets, the job's success/failure/skipped counters and overall status stay correct, and a job never gets stuck RUNNING forever if a Scrapyd node dies with the batch still queued.

**Why this priority**: Correctness and resilience of the orchestration layer. It is essential for production but not required to demonstrate the basic trigger→scrape→observe loop, so it follows the two functional slices.

**Independent Test**: Simulate targets transitioning to completed/failed and assert counters are derived by aggregation from targets (not incremented per target) and that job status resolves deterministically (COMPLETED / PARTIAL_FAILED / FAILED). Simulate a dispatched batch whose targets never progress past the timeout and assert it is detected and re-dispatched under the same idempotency and in-flight-lock guards without double-running progressed targets.

**Acceptance Scenarios**:

1. **Given** a job with many targets completing concurrently, **When** counters are refreshed, **Then** success_count/failure_count/skipped_count are computed by aggregating over scrape_job_targets (periodically and at finalization), never by one write-per-target on the job row.
2. **Given** all targets reached a terminal state, **When** the job is finalized, **Then** its status is COMPLETED (all succeeded), PARTIAL_FAILED (some succeeded, some failed), or FAILED (none succeeded), with completed_at set.
3. **Given** a batch was dispatched but its targets never left the pending/queued state past the stall timeout, **When** stalled-batch detection runs, **Then** the batch is re-dispatched to a deterministically selected node, guarded so already-progressed targets and in-flight-locked matches are not double-run.
4. **Given** dispatch retries for one batch, **When** node selection runs, **Then** the same batch always resolves to the same node (deterministic selection / persisted node), so two retries cannot send one batch to two nodes.

---

### Edge Cases

- **Cross-workspace access**: A caller must never create, view, or affect a job/target outside their workspace. Reads with no workspace context return zero rows; writes are rejected.
- **Match not found / not active**: Run-match on a missing or cross-workspace match is a not-found; run-variant on a missing variant is a not-found.
- **Duplicate dispatch delivery**: At-least-once task delivery must not cause duplicate Scrapyd runs (idempotent dispatch guard keyed on job/batch).
- **Node loss**: Scrapyd's pending queue is per-node and not durable; a batch queued on a node that dies must be recoverable via stall detection + timed re-dispatch.
- **Worker process fork**: A Celery prefork worker must dispose any database engine inherited from the parent process on `worker_process_init` before first use, to avoid sharing connections across the fork.
- **Empty fan-out**: Variant/other scoped runs with no active matches must not dispatch and must resolve to a defined terminal state.
- **Concurrent runs of the same match**: The unique(scrape_job_id, match_id) constraint plus in-flight match locks (owned by the spider, delivered in SPEC-07/later) prevent duplicate work within and across jobs; the orchestration layer must not itself schedule the same match twice in one batch.
- **Partial batch failure**: Some targets in a batch succeed while others fail; job status reflects PARTIAL_FAILED and per-target error codes are preserved.
- **Job with a single target that is skipped**: Skipped targets increment skipped_count and are reflected in the finalized status rule.

## Requirements *(mandatory)*

### Functional Requirements

**Data model**

- **FR-001**: System MUST persist a `scrape_jobs` record for every triggered run, carrying: id, workspace_id, type, scope, nullable scope references (product_id, product_variant_id, product_group_id, competitor_id, match_id), status, priority, total_targets, success_count, failure_count, skipped_count, nullable requested_by, source, nullable started_at, nullable completed_at, created_at.
- **FR-002**: System MUST persist a `scrape_job_targets` record per match in a job, carrying: id, workspace_id, scrape_job_id, match_id, status, nullable locked_at, nullable started_at, nullable completed_at, nullable error_code, created_at; with a unique constraint on (scrape_job_id, match_id).
- **FR-003**: Job `type` MUST be one of MANUAL, SCHEDULED, API_TRIGGERED, RETRY_FAILED, DISCOVERY. Job `status` MUST be one of PENDING, RUNNING, COMPLETED, PARTIAL_FAILED, FAILED, CANCELLED. Job `source` MUST be one of API, SCHEDULER, INTERNAL, PLUGIN.
- **FR-004**: Both new tables MUST enforce workspace isolation at the application scoping layer AND via Postgres row-level security, consistent with prior specs (SPEC-03/04/05/06). With no workspace context set, reads MUST return zero rows.
- **FR-005**: Schema changes MUST be delivered as a forward migration that composes with the existing single-head migration chain and continues to yield a single head.

**Run endpoints (API service)**

- **FR-006**: System MUST expose `POST /v1/jobs/run/match/{match_id}` that, for a match in the caller's workspace, creates a ScrapeJob (scope MATCH) and exactly one ScrapeJobTarget, enqueues a dispatch task, and returns the job id (HTTP 202/201-style acceptance). It MUST reject an unknown/cross-workspace match as not-found without creating a job.
- **FR-007**: System MUST expose `POST /v1/jobs/run/variant/{variant_id}` that finds all active matches for the variant, creates one ScrapeJob (scope VARIANT) with one unique target per active match, enqueues dispatch, and returns the job id. Inactive matches MUST be excluded.
- **FR-008**: System MUST expose `GET /v1/jobs/{job_id}` returning the job's status, type, scope, counts (total/success/failure/skipped), and lifecycle timestamps, scoped to the caller's workspace.
- **FR-009**: System MUST expose `GET /v1/jobs/{job_id}/results` returning the job's targets, each with match reference, status, and error_code, scoped to the caller's workspace.
- **FR-010**: All run endpoints MUST record who/what triggered the job: `requested_by` = the authenticated principal, `type` = MANUAL, and `source` = API for direct operator API calls. (`API_TRIGGERED`/`PLUGIN`/`SCHEDULED`/`INTERNAL` are used by later programmatic/scheduler triggers.)

**Dispatch & worker orchestration**

- **FR-011**: A `scrape_dispatch` Celery queue MUST carry a dispatch task that expands a job's targets into Scrapyd runs, grouping (batching) matches by workspace, competitor/domain, and scrape mode (HTTP/BROWSER). It MUST NOT create one Scrapyd job per URL at scale; HTTP batches SHOULD contain 50–200 matches.
- **FR-012**: The worker MUST call the Scrapyd `schedule.json` API authenticated, passing at minimum workspace_id, scrape_job_id, and the batch's match_ids, reusing the existing Scrapyd client.
- **FR-013**: Dispatch MUST be idempotent: an at-least-once or retried delivery of the dispatch task MUST NOT produce a second Scrapyd run for the same batch. The idempotency guard MUST key on job/batch identity so duplicates are safely neutralized.
- **FR-014**: Within a Scrapyd pool, node selection MUST be deterministic (e.g., hash by domain, or round-robin with the chosen node persisted on the batch) so two dispatch retries can never send one batch to two different nodes.
- **FR-015**: System MUST detect a dispatched batch whose targets never progressed past a configured stall timeout (e.g., the node died with the batch queued) and re-dispatch it, protected by the same idempotency guard and in-flight match locks so already-progressed or locked matches are not double-run.
- **FR-016**: Celery prefork workers MUST dispose any database engine inherited from the parent process on `worker_process_init` before first use (fork-safety), so pooled connections are never shared across a fork.

**Lifecycle & counters**

- **FR-017**: Targets MUST support status updates (pending → started → completed/failed/skipped) with the appropriate timestamps (started_at, completed_at) and error_code on failure. These updates are made by the dispatch path (RUNNING/started_at on the job) AND by the scrape result path: the SPEC-07 item pipeline MUST mark each target terminal (COMPLETED on a successful observation, FAILED with error_code otherwise) within the same reactor-safe persistence transaction, keyed on (scrape_job_id, match_id), and MUST enqueue a finalize for the affected job(s) so counters/status resolve without depending on a periodic scheduler.
- **FR-018**: Job counters (success_count, failure_count, skipped_count) MUST be derived by aggregating over scrape_job_targets — computed periodically and at finalization — and MUST NOT be incremented once per target on the job row (to avoid serializing thousands of writes on one hot row).
- **FR-019**: Job status MUST resolve deterministically at finalization by a failure-centric rule: COMPLETED when there are no failed targets (all-success, success+skipped, or skipped-only all resolve to COMPLETED — skips are non-fatal); PARTIAL_FAILED when there is at least one failure AND at least one success; FAILED when there is at least one failure AND no successes; a zero-target job is COMPLETED. started_at is set when work begins and completed_at when the job reaches a terminal state.
- **FR-020**: total_targets MUST equal the number of targets created for the job; a job with zero targets MUST be finalized immediately as COMPLETED (total_targets = 0) without dispatching.

### Key Entities *(include if feature involves data)*

- **ScrapeJob**: One triggered scraping run at a given scope (match, variant, product, group, competitor, workspace). Owns lifecycle status, priority, provenance (type/source/requested_by), aggregate counters, and lifecycle timestamps. Scoped to a workspace.
- **ScrapeJobTarget**: One match to be scraped within a job. Owns its own status, lifecycle timestamps, lock timestamp, and error code. Unique per (job, match). Scoped to a workspace. Aggregation over targets produces the parent job's counters and terminal status.
- **Dispatch batch**: A logical grouping of a job's targets by workspace/domain/mode used to schedule Scrapyd runs efficiently; carries the deterministically-selected node and the idempotency key that guards against duplicate scheduling and enables stalled-batch re-dispatch. (May be represented via target/job fields rather than a distinct table — an implementation choice for planning.)

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Triggering a single match run returns a job id and the run executes asynchronously — the API response does not block on the scrape completing.
- **SC-002**: A run-variant request produces exactly one target per active match with no duplicates, verifiable by the unique (job, match) guarantee, for variants with at least dozens of matches.
- **SC-003**: A dispatch task delivered twice for the same batch results in exactly one Scrapyd run (zero duplicate scrapes).
- **SC-004**: For a job of any size, the number of writes to the job counter row does not grow linearly with the number of targets — counters are aggregated, not per-target increments.
- **SC-005**: A batch queued on a node that becomes unavailable is re-dispatched within a bounded stall timeout without re-running targets that already progressed.
- **SC-006**: A caller can never observe or affect a job or target belonging to another workspace; requests with no workspace context yield zero rows.
- **SC-007**: Job status and results endpoints return counts and per-target outcomes that are consistent with the underlying targets at every terminal state (COMPLETED / PARTIAL_FAILED / FAILED).
- **SC-008**: One Scrapyd job is created per batch, not per URL — large jobs produce a batch count consistent with domain/mode grouping and the 50–200 HTTP batch-size guidance, not thousands of tiny jobs.

## Assumptions

- **Scope of this spec**: Only match- and variant-scoped run endpoints are required here (per the acceptance criteria). Product-, group-, competitor-, and workspace-scoped runs, plus the scheduler that triggers SCHEDULED jobs, are later specs; the job model carries their scope fields but this spec need not expose those endpoints.
- **Actual price analysis / alerting is out of scope**: This spec ends at persisting job/target outcomes. Emitting the `price_analysis` task, updating current prices, and alert logic are SPEC-09.
- **Spider/persistence already exist**: SPEC-07 delivered the generic HTTP spider, item pipeline, observation persistence, and the authenticated Scrapyd client (`libs/shared/app_shared/scrapyd/client.py`); this spec reuses them and does not re-implement scraping.
- **In-flight match locks / rate limiting**: Fencing-token match locks and distributed rate limiting are formally SPEC-11; this spec relies on the idempotent dispatch guard and the unique (job, match) constraint for safety, and integrates with locks where they exist. Where a full lock is not yet available, re-dispatch safety is provided by the idempotency guard plus target-state checks.
- **Deferred live verification**: The build environment has no running Docker daemon / live Postgres, Redis, or Scrapyd. DB/Redis/Scrapyd-dependent behaviors are verified via unit tests plus integration tests that skip cleanly when infrastructure is absent, consistent with SPEC-01…07.
- **Batching representation**: Whether a "batch" is a first-class row or a derived grouping (with node + idempotency key stored on targets/job) is an implementation decision left to planning; the deterministic-node and idempotency guarantees are the binding requirements.
- **ID/timestamp conventions**: uuidv7 primary keys and standard created/updated timestamp conventions follow the project-wide ID strategy already established.
- **Browser-mode routing**: Batching groups by scrape mode so HTTP and BROWSER matches never share a batch. Dispatch routes each batch to the node pool matching its mode (BROWSER → browser Scrapyd pool, HTTP → HTTP pool). The browser spider/service itself is SPEC-14; this spec routes correctly by mode but only HTTP scraping is proven end-to-end here.
- **US3 runtime activation**: `finalize_jobs`/counter refresh are triggered event-driven by the scrape result path (FR-017), so counters/finalization are operational within this spec. Periodic `recover_stalled_batches` needs the Celery beat schedule, which is delivered in SPEC-13; the recovery mechanism is delivered and unit-tested here and activates automatically once beat wiring exists.
- **`priority` field**: The job `priority` column reuses the existing project priority vocabulary (`MatchPriority`, default NORMAL); no requirement in this spec sets or acts on priority — it is carried forward for the scheduler (SPEC-13).
- **In-flight match lock**: The fencing-token match lock (Constitution VIII) is spider-owned and formally SPEC-11. In isolation this spec ships only the `unique(scrape_job_id, match_id)` constraint + idempotent dispatch guard + target-state checks for re-dispatch safety; the lock lands in SPEC-11 before real-data scale (documented cross-spec boundary, not a silent gap).
