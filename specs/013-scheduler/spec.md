# Feature Specification: Scheduler

**Feature Branch**: `013-scheduler`

**Created**: 2026-07-05

**Status**: Draft

**Input**: User description: "SPEC-13 — Scheduler. Purpose: dynamic recurring jobs. A custom DB-driven scheduler enqueuer service that claims due refresh rules and enqueues scrape jobs, with no duplicate runs across multiple scheduler instances."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Configure recurring refresh schedules (Priority: P1)

An operator (via the API, plugin, or an internal script) wants their catalog re-scraped on a
recurring cadence without pressing "run" every time. They define a **refresh rule** that names
*what* to refresh (a whole workspace, a competitor, a product, a variant, a product group, or a
single match) and *how often* (a cron expression such as "daily at 06:00", or a fixed interval
such as "every 60 minutes"). They can list their rules, change the cadence or scope, and enable
or disable a rule without deleting it.

**Why this priority**: Nothing recurring can happen until rules can be expressed and stored.
This is the configuration surface the whole feature exists to serve, and it delivers standalone
value: an operator can capture their refresh policy even before the loop that acts on it exists.

**Independent Test**: Create a "daily workspace refresh" rule and an "hourly product group
refresh" rule through the API, read them back, disable one, and confirm rules created in one
workspace are invisible to another workspace.

**Acceptance Scenarios**:

1. **Given** an authenticated workspace, **When** the operator creates a rule with
   scope=WORKSPACE and cron_expression="0 6 * * *", **Then** the rule is persisted with
   enabled=true and a first `next_run_at` computed from the cadence.
2. **Given** an authenticated workspace, **When** the operator creates a rule with
   scope=PRODUCT_GROUP, product_group_id set, and interval_minutes=60, **Then** the rule is
   persisted and its `next_run_at` is one interval ahead.
3. **Given** an existing rule, **When** the operator disables it (enabled=false), **Then** it
   remains stored but is never claimed by the scheduler.
4. **Given** a rule owned by workspace A, **When** workspace B lists or fetches rules, **Then**
   workspace A's rule is not returned and is not addressable by B.
5. **Given** a create request that specifies neither a cron expression nor an interval (or
   specifies both), **When** it is submitted, **Then** it is rejected with a validation error.
6. **Given** a create request whose scope requires a target id (e.g. scope=MATCH without
   match_id, or an id belonging to another workspace), **When** it is submitted, **Then** it is
   rejected with a validation error.

---

### User Story 2 - Scheduler enqueues due jobs automatically (Priority: P1)

The scheduler service runs continuously. On each pass it finds the rules that are due
(enabled and `next_run_at <= now`), and for each one it resolves the rule's scope to the set of
active matches, creates a scheduled scrape job for them, hands that job off to be dispatched,
records that the rule just ran, and computes when it should next run. From the operator's point
of view: they configured a daily workspace refresh, and every day a scrape job appears and runs
without anyone touching the API.

**Why this priority**: This is the core behavior — turning stored rules into real scrape jobs
on time. Together with US1 it forms the MVP (configure a rule → it runs on schedule).

**Independent Test**: Seed a due rule (next_run_at in the past) whose scope resolves to at least
one active match, run one scheduler pass, and confirm exactly one scheduled scrape job with its
targets is created and dispatched, `last_run_at` advances, and `next_run_at` moves into the
future by one cadence.

**Acceptance Scenarios**:

1. **Given** an enabled WORKSPACE-scoped rule that is due and a workspace with active matches,
   **When** the scheduler runs a pass, **Then** one scrape job of type SCHEDULED / source
   SCHEDULER is created covering those matches and its dispatch is enqueued.
2. **Given** a due PRODUCT_GROUP rule, **When** the scheduler runs, **Then** the job's targets
   are exactly the active matches reachable from that group's members.
3. **Given** a rule that just fired, **When** the pass completes, **Then** `last_run_at` is set
   to the run time and `next_run_at` is advanced to the next occurrence per its cron/interval.
4. **Given** a due rule whose scope currently resolves to zero active matches, **When** the
   scheduler runs, **Then** no dispatch is wasted on empty work, yet the rule is still advanced
   so it does not stay perpetually "due" and re-selected every pass.
5. **Given** an enabled rule that is not yet due (`next_run_at` in the future), **When** the
   scheduler runs, **Then** the rule is not claimed and no job is created for it.
6. **Given** a disabled rule that would otherwise be due, **When** the scheduler runs, **Then**
   it is skipped.

---

### User Story 3 - No duplicate runs under concurrency or failure (Priority: P2)

Several scheduler instances may run at once for availability. The same due rule must produce
exactly one scheduled job per due moment — never two because two instances grabbed it, and never
zero because a process died mid-pass. A scheduled job and a manual job may still target the same
match; that overlap is tolerated downstream, but two identical *scheduled* runs of one rule at
one moment must not occur.

**Why this priority**: Correctness and safety at scale. The MVP works with a single instance
(US1+US2); this story hardens it for real multi-instance operation and crash recovery.

**Independent Test**: Point two scheduler passes at the same set of due rules concurrently and
confirm each rule is claimed and fired by exactly one of them; then simulate a process dying
after selecting a rule but before committing, and confirm the rule is neither lost nor
double-fired on the next pass.

**Acceptance Scenarios**:

1. **Given** two scheduler instances and a set of due rules, **When** they run overlapping
   passes, **Then** each rule is claimed by exactly one instance (no rule fires twice, none is
   skipped) — achieved by row-level claiming that lets each instance skip rows another instance
   already holds.
2. **Given** a rule is selected and its dispatch task enqueued, **When** the claiming
   transaction fails to commit (e.g. the process dies), **Then** the rule's `next_run_at` is
   left unchanged so a later pass re-runs it; any dispatch that did leak out is neutralized
   downstream (idempotent dispatch + in-flight match locks) rather than double-scraping.
3. **Given** normal operation, **When** the system schedules a run, **Then** the design favors a
   possible duplicate over a missed run (dispatch is committed together with the rule update, not
   after it).
4. **Given** many due rules in one pass, **When** the scheduler processes them, **Then** it does
   not serialize all instances behind a single global lock (which would defeat horizontal
   scaling); isolation is per-rule.

---

### Edge Cases

- **Neither/both cadence fields**: a rule with neither `cron_expression` nor `interval_minutes`,
  or with both, is rejected at write time (US1 AS-5). The scheduler never has to guess a cadence.
- **Missed window / downtime backlog**: if the scheduler was down and `next_run_at` is far in the
  past, the rule fires once on recovery and `next_run_at` is advanced to the next *future*
  occurrence — it does not fire once per missed interval (no thundering catch-up).
- **Scope target removed**: if a rule references a product/variant/group/competitor/match that
  was later deleted, the scope simply resolves to zero active matches (handled like US2 AS-4);
  the rule does not crash the pass.
- **Deleting the scope target row**: refresh rules referencing a deleted scope target must not
  block deletion of catalog/competitor rows nor leave the scheduler dereferencing a missing row.
- **Crash after claim, before commit**: the in-progress transaction rolls back, the row lock is
  released, `next_run_at` is unchanged, and another instance (or the next pass) re-claims it
  (US3 AS-2).
- **`locked_at` observability**: `locked_at` records the last claim time for operator visibility;
  correctness does not depend on a separate stale-lock reaper because an uncommitted claim
  releases its lock automatically on rollback.
- **Enabled toggled mid-pass**: a rule disabled between selection and the next pass is simply not
  selected next time; a rule disabled after being claimed in the current transaction still
  completes that already-committed run.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST persist **refresh rules** as workspace-owned records capturing:
  name, scope, the scope's target id (product/variant/product-group/competitor/match, nullable
  per scope), exactly one cadence (`cron_expression` OR `interval_minutes`), priority, an
  enabled flag, and the scheduling timestamps `next_run_at`, `last_run_at`, and `locked_at`.
- **FR-002**: The system MUST support all six refresh scopes — WORKSPACE, COMPETITOR, PRODUCT,
  VARIANT, PRODUCT_GROUP, MATCH — and MUST require the target id appropriate to the chosen scope
  (WORKSPACE requires none; the others require their corresponding id) and reject mismatches.
- **FR-003**: The system MUST reject a rule that specifies neither cadence nor both cadences; each
  rule has exactly one of `cron_expression` or `interval_minutes`.
- **FR-004**: Operators MUST be able to create, read, list, update, and delete refresh rules, and
  to enable/disable a rule without deleting it, all scoped to their workspace.
- **FR-005**: All refresh-rule reads and writes MUST be confined to the caller's workspace via
  both application-level scoping and database Row-Level Security, and refresh_rules MUST be
  created with RLS enabled from its first migration. A missing application filter MUST NOT expose
  another workspace's rules.
- **FR-006**: On rule creation/update the system MUST compute `next_run_at` from the rule's
  cadence; on each successful run it MUST recompute `next_run_at` to the next occurrence that is
  in the future relative to the run time.
- **FR-007**: The scheduler service MUST periodically select **due** rules — those with
  enabled=true and `next_run_at <= now()` — ordered by `next_run_at`, and process them.
- **FR-008**: The scheduler MUST claim due rules using row-level locking that skips rows already
  claimed by another instance, so that multiple scheduler instances can run concurrently and each
  due rule is processed by exactly one of them.
- **FR-009**: The scheduler MUST NOT wrap an entire pass in a single global/advisory pass-lock
  that would force a singleton and negate row-level skip-locked claiming. (A transaction-scoped
  per-rule advisory lock is permissible as belt-and-suspenders; a global pass lock is not.)
- **FR-010**: For each claimed rule the scheduler MUST resolve the rule's scope to the set of
  **active matches** it targets, using the same scope→match resolution already used by the
  corresponding manual run flows (run workspace / competitor / product / variant / product group
  / match).
- **FR-011**: For each claimed rule the scheduler MUST create a scrape job of type SCHEDULED and
  source SCHEDULER (with its job targets) for the resolved matches, reusing the existing job
  creation service rather than duplicating job/target/dispatch logic.
- **FR-012**: The scheduler MUST enqueue the Celery dispatch task **inside the same transaction
  that claims the rule and updates its scheduling fields, before that transaction commits**. It
  MUST NOT commit the rule update first and enqueue afterward.
- **FR-013**: Within the claiming transaction, for each fired rule the scheduler MUST set
  `last_run_at` to the run time, set `locked_at` to the claim time, and advance `next_run_at`
  to the next future occurrence before commit.
- **FR-014**: If the claiming transaction fails to commit, the rule's scheduling fields
  (including `next_run_at`) MUST remain unchanged so the run is retried, and the system MUST rely
  on the downstream idempotent dispatch guard and in-flight match locks to neutralize any dispatch
  that already escaped — favoring a possible duplicate over a missed run.
- **FR-015**: A due rule whose scope resolves to zero active matches MUST still have its
  `next_run_at`/`last_run_at` advanced (so it is not perpetually re-selected) without wasting a
  dispatch on empty work.
- **FR-016**: The scheduler MUST tolerate a large backlog: a rule whose `next_run_at` is far in
  the past fires once on the next pass and is advanced to the next future occurrence, not once per
  missed interval.
- **FR-017**: The scheduler MUST NOT perform per-request hot-row writes; scheduling bookkeeping is
  confined to the refresh-rule row updates and the standard job/target inserts.
- **FR-018**: All timestamp fields (`next_run_at`, `last_run_at`, `locked_at`, `created_at`,
  `updated_at`) MUST be timezone-aware (TIMESTAMPTZ) and compared against a timezone-aware "now".
- **FR-019**: The scheduler service, running from the existing scheduler app, MUST NOT introduce
  Scrapy/Twisted/Playwright dependencies into the shared library or its own image (scraping-free
  scheduling path).
- **FR-020**: Deleting a catalog, competitor, or match row referenced by a refresh rule MUST be
  handled cleanly (e.g. the reference is cleared or the rule is removed) so it neither blocks the
  delete nor leaves the scheduler dereferencing a missing target.

### Key Entities *(include if feature involves data)*

- **Refresh Rule**: A workspace-owned statement of "re-scrape *this scope* on *this cadence*."
  Holds the scope and its target id, exactly one cadence (cron or interval), a priority, an
  enabled flag, and the scheduling clock (`next_run_at`, `last_run_at`, `locked_at`). It is the
  only new persistent entity in this feature.
- **Scrape Job / Scrape Job Target** *(existing)*: Produced by a fired rule via the existing job
  service; the scheduler creates them with type SCHEDULED and source SCHEDULER — it does not
  redefine them.
- **Match** *(existing)*: The unit a rule's scope resolves down to; only active matches are
  targeted.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can configure a daily whole-workspace refresh and an hourly
  product-group refresh entirely through the API, and see both stored with a correct first
  next-run time.
- **SC-002**: A rule configured to run on a cadence produces a scheduled scrape job at each due
  moment with no manual action, and its next-run time always advances into the future after
  firing.
- **SC-003**: Across concurrent scheduler instances, each due rule produces exactly one scheduled
  job per due moment — 0% duplicate scheduled runs and 0% missed runs in a concurrency test over
  a batch of due rules.
- **SC-004**: No refresh rule is ever readable or writable across workspace boundaries, including
  when an application-level filter is omitted (verified by a cross-workspace denial test).
- **SC-005**: After scheduler downtime, each rule that came due during the outage fires exactly
  once on recovery (not once per missed interval) and resumes its normal cadence.
- **SC-006**: A due rule whose scope has no active matches advances its schedule without creating
  wasted dispatches, and never becomes stuck as permanently "due."

## Assumptions

- **Reuse of existing job path**: Job creation, target generation, node selection, and idempotent
  Scrapyd dispatch already exist (SPEC-08) and are reused as-is; this feature only decides *when*
  and *for which scope* to invoke them. The scope→match resolution mirrors the existing manual run
  flows.
- **Cadence semantics**: `cron_expression` uses standard 5-field cron semantics evaluated in UTC;
  `interval_minutes` schedules the next run that many minutes after the current run time. Exactly
  one is set per rule.
- **Duplicate tolerance downstream**: The idempotent dispatch guard and in-flight match locks from
  SPEC-08/SPEC-11 are the safety net that makes "enqueue-before-commit" safe; a scheduled and a
  manual job may target the same match without harm.
- **Priority is advisory**: `priority` orders/labels work but does not change claiming
  correctness; due rules are claimed in `next_run_at` order.
- **Multiple scheduler instances allowed**: The service is horizontally scalable; correctness
  comes from row-level skip-locked claiming, not from running a single instance.
- **Backend only**: No frontend is delivered in v1; the configuration surface is the API.
