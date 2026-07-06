# Feature Specification: Retention, Rollups & Partition Maintenance

**Feature Branch**: `015-retention-rollups-partition-maintenance`

**Created**: 2026-07-06

**Status**: Draft

**Input**: User description: "SPEC-15 — Retention, Rollups & Partition Maintenance. Deliver the scheduled jobs that keep the already-partitioned append-heavy tables (price_observations, request_attempts, price_alert_events, and later webhook_events) healthy over time: create next month's partitions in advance, generate daily rollups, drop expired partitions by DROP (never bulk DELETE) only after their rollups are verified, and guarantee readers tolerate soft references that dangle into dropped partitions."

## Clarifications

### Session 2026-07-06

All items below were resolved doc-first (PROJECT_SPEC §29/§22) or by direct derivation from the data model — no open human decision remained. They are recorded here because they materially sharpen data modeling, the verify-before-drop ordering guarantee, and scheduling for downstream task design.

- Q: By which calendar date is a raw price observation assigned to a daily rollup? → A: The UTC calendar date of its partition key (`price_observations.scraped_at`). This makes a partition's date range map 1:1 onto the set of rollup dates that must exist to consider it "covered," which is the basis for the verify-before-drop check (FR-016).
- Q: How far ahead must partitions be pre-created? → A: At least the next month (maintain current + next month at all times), run with enough schedule lead time that next month's partition always exists before that month begins (FR-004/FR-005).
- Q: Which day does each scheduled rollup run process, and how does it handle re-runs? → A: The most recent completed UTC day by default, idempotently — re-running or backfilling any past day upserts on `unique(workspace_id, product_variant_id, date)` without duplication (FR-010).
- Q: "Verified complete" rollups for a partition means what, concretely? → A: For every UTC date within the partition's range that had source pricing data, a rollup row exists; a date with no source data needs no rollup to count as covered (FR-016, Assumptions).

## User Scenarios & Testing *(mandatory)*

The consumers of this feature are the **platform operators** who run the price-monitoring backend and the **downstream readers** (analytics, comparison, alerting) that depend on the append-heavy data staying queryable, bounded, and correct. There is no end-user UI in v1; the "actors" are scheduled maintenance jobs and the code paths that read partitioned data.

### User Story 1 - Next month's partitions exist before writes need them (Priority: P1)

The platform continuously appends rows to monthly-partitioned tables (price observations, request attempts, alert events). A scheduled maintenance job creates each table's upcoming monthly partition(s) ahead of time, so that when the calendar rolls into a new month, inserts land in an existing partition instead of failing.

**Why this priority**: Without an advance partition, the first write of a new month raises a "no partition found for row" error and takes down every scrape-result and attempt write across all workspaces. This is the single most critical maintenance guarantee — it prevents a hard, recurring, calendar-driven outage. It delivers standalone value even if no other story ships.

**Independent Test**: Run the partition-creation job at any date; verify the current and next month's partitions now exist for every registered partitioned table, that re-running the job is a no-op (no error, no duplicate), and that a write dated into next month succeeds without error.

**Acceptance Scenarios**:

1. **Given** a registered partitioned table whose next-month partition does not yet exist, **When** the partition-creation job runs, **Then** the next-month partition is created with the correct monthly bounds and the job reports it as created.
2. **Given** all required partitions already exist, **When** the job runs again, **Then** no partition is created or altered, no error is raised, and the job reports zero changes (idempotent / self-healing).
3. **Given** a table is missing even its current-month partition (e.g. first deploy, or a gap), **When** the job runs, **Then** the missing current-month partition is also created so no in-range write can fail.
4. **Given** a table named in the partition registry does not yet exist in the database (e.g. webhook_events before its introducing spec ships), **When** the job runs, **Then** that table is skipped without error and the remaining tables are still maintained.

### User Story 2 - Daily rollups summarize each day's pricing (Priority: P2)

A scheduled job aggregates the raw price data for a completed day into one compact daily rollup row per client variant (per currency), capturing the client price and the min / average / max comparable competitor prices, the alert type, and the comparable-competitor count. These rollups are the durable, long-lived summary that outlives the 90-day raw retention window.

**Why this priority**: Rollups are the mechanism that lets the platform drop raw partitions cheaply while still retaining two years of pricing history for reporting. They are also the precondition for safe retention (Story 3). Valuable on their own as the historical analytics surface.

**Independent Test**: Seed a day of raw observations/current-price comparisons for several variants; run the rollup job for that day; verify exactly one rollup row per (workspace, variant, day) with correct client price, min/avg/max competitor prices, alert type, comparable count, and that re-running the job for the same day does not duplicate or corrupt rows.

**Acceptance Scenarios**:

1. **Given** a completed day with priced comparisons for a variant, **When** the daily rollup job runs for that day, **Then** exactly one rollup row exists for that (workspace, variant, day) with the correct aggregated values.
2. **Given** a rollup already exists for a (workspace, variant, day), **When** the rollup job runs again for that day, **Then** the existing row is updated in place (upsert on the unique key), never duplicated.
3. **Given** a variant with no comparable competitor prices that day, **When** the rollup runs, **Then** the rollup row is still produced with the client price recorded and the competitor min/avg/max left empty and the comparable count zero.
4. **Given** competitor prices in a currency that differs from the client price, **When** the rollup runs, **Then** non-comparable-currency prices are excluded from the min/avg/max aggregation (only same-currency comparable prices are aggregated), preserving monetary correctness.

### User Story 3 - Expired partitions are dropped, but only after their rollups are verified (Priority: P2)

A scheduled retention job removes data older than each table's retention window by **dropping the whole expired monthly partition** (never issuing a bulk row delete). For any table whose raw rows feed the daily rollups, the retention job must first confirm that the rollups covering that partition's date range are complete; only then may it drop the partition.

**Why this priority**: Retention is what keeps the database bounded and cheap at millions of rows per month. Doing it as partition-drop (not DELETE) is what avoids bloat and vacuum storms. The verify-before-drop ordering is a hard data-safety guarantee: dropping a raw partition before its rollups are computed would permanently lose summarizable history.

**Independent Test**: Create partitions older than the retention window with known rollup coverage; run retention; verify partitions whose rollups are complete are dropped, partitions whose rollups are missing/incomplete are retained and flagged, no bulk DELETE is issued, and partitions inside the retention window are untouched.

**Acceptance Scenarios**:

1. **Given** a raw partition older than its table's retention window whose covering daily rollups are all present, **When** the retention job runs, **Then** the partition is dropped (via partition drop, not row delete) and reported as reclaimed.
2. **Given** a raw partition older than the retention window whose covering rollups are missing or incomplete, **When** the retention job runs, **Then** the partition is **not** dropped, and the job records that it was skipped pending rollups.
3. **Given** a partition whose newest possible row is still within the retention window, **When** the retention job runs, **Then** the partition is left in place.
4. **Given** a table that has no rollup dependency (e.g. request attempts, alert events, webhook events), **When** its partition ages past its own retention window, **Then** it is dropped by age alone without a rollup check, using that table's specific retention window.

### User Story 4 - Readers tolerate references into dropped partitions (Priority: P3)

Current-state tables (e.g. a match's current price, a competitor match's current-price pointer) hold a plain soft-reference id into a partitioned raw table, with no foreign key. After retention drops the referenced partition, that id dangles. Every reader that follows such a soft reference must tolerate the referenced row being gone and rely on the denormalized fields the current-state row already carries.

**Why this priority**: This is a correctness guard rather than a new capability. Retention makes dangling references an expected steady state, so readers must never assume the pointed-to raw row still exists. Lower priority because it is a robustness property verified across existing read paths rather than a new job.

**Independent Test**: Take a current-state row whose soft-reference id points at a raw row in an already-dropped partition; exercise every read path that consumes that current-state row; verify each returns correct denormalized data and never errors, 500s, or filters the row out due to the missing raw row.

**Acceptance Scenarios**:

1. **Given** a current-state row whose soft-reference id points into a dropped partition, **When** a reader loads that current-state row, **Then** it returns successfully using the denormalized fields and does not attempt a hard join that would drop or error on the row.
2. **Given** the same dangling soft reference, **When** code explicitly tries to fetch the referenced raw row, **Then** the absence is handled as an expected "not found" (no exception propagated to the caller), and any dangling-reference check the maintenance job runs reports it as tolerated, not as data corruption.

### Edge Cases

- **Month/year boundary & leap conditions**: The partition-creation job must compute correct monthly bounds across December→January and February lengths; "next month" of December is January of the next year.
- **Double run / concurrency**: Two maintenance runs overlapping (or a retried scheduled job) must not create duplicate partitions, drop a partition twice, or double-count rollups. Operations are idempotent and safe under concurrent execution.
- **Registry lists a not-yet-created table**: Skipped cleanly (Story 1 scenario 4) rather than erroring the whole pass.
- **Rollup for a day with zero data**: Produces no spurious rows for variants that had no activity; a day with no data at all yields no rollups and does not block retention for unrelated tables.
- **Retention window boundary**: A partition exactly at the retention cutoff is treated deterministically (documented rule: dropped only when the entire partition range is strictly older than the cutoff).
- **Partition drop of a table with dependent soft references**: Dropping proceeds regardless of dangling soft references (there are no FKs); readers tolerate the result (Story 4).
- **Rollups incomplete indefinitely**: If a raw partition can never have its rollups verified, retention keeps skipping it and surfaces it for operator visibility rather than silently dropping or silently accumulating without signal.
- **Vacuum/analyze**: Optional housekeeping on partitions must never block or fail the core create/rollup/drop guarantees.

## Requirements *(mandatory)*

### Functional Requirements

**Partition registry & scope**

- **FR-001**: The system MUST maintain a registry of monthly-partitioned tables and, for each, its partition key column and its retention window. The initial set is `price_observations` (90 days), `request_attempts` (90 days), `price_alert_events` (1 year), and `webhook_events` (90 days). Daily rollups (`variant_price_daily_rollups`) retain for 2 years.
- **FR-002**: The maintenance jobs MUST operate only over registered tables that actually exist in the database, skipping any registered table not yet present (e.g. `webhook_events` before its introducing spec ships) without failing the run.
- **FR-003**: The maintenance work MUST run as scheduled background jobs (not on any request/API path) and MUST NOT perform any scraping.

**Partition creation (US1)**

- **FR-004**: The partition-creation job MUST ensure the next month's partition exists for every registered, existing partitioned table, created with correct monthly bounds for the table's partition key.
- **FR-005**: The partition-creation job MUST also create the current month's partition if it is missing (self-healing), so no in-range write can fail for lack of a partition.
- **FR-006**: The partition-creation job MUST be idempotent: re-running when partitions already exist creates nothing, alters nothing, and raises no error.
- **FR-007**: The partition-creation job MUST compute monthly bounds correctly across month/year boundaries (including December→January).
- **FR-008**: Every partition the job creates MUST satisfy the project's partitioned-table rules (monthly range partition on the declared partition key; the table's unique/primary keys already include the partition key — this job creates partitions only, it does not create or alter the parent tables).

**Daily rollups (US2)**

- **FR-009a**: The system MUST create the `variant_price_daily_rollups` table (deferred from SPEC-09) via a single Alembic migration that keeps the migration history at a single head, as a workspace-owned table with row-level security and `unique(workspace_id, product_variant_id, date)`. It is a plain (non-partitioned) table retained by row age (2 years); it is not a monthly-partitioned table.
- **FR-009**: The system MUST generate daily rollups into `variant_price_daily_rollups`, one row per (workspace, product variant, day), carrying the client price, the min/avg/max comparable competitor prices, the alert type, the comparable-competitor count, the product/variant identity, currency, and the date.
- **FR-010**: Rollup generation MUST be an upsert on the unique key (workspace, product variant, date): re-running for a day updates the existing row rather than duplicating it.
- **FR-011**: Competitor price aggregation (min/avg/max) MUST include only comparable, same-currency prices; non-comparable or currency-mismatched prices MUST be excluded from the aggregates, and the comparable-competitor count MUST reflect only the included prices.
- **FR-012**: Monetary aggregation MUST use exact decimal arithmetic on finite values only; no floating-point money and no NaN/Infinity may enter a rollup.
- **FR-013**: A rollup row MUST be produced for a variant that had a client price that day even when it had zero comparable competitor prices (competitor min/avg/max empty, comparable count zero).
- **FR-014**: Rollup rows MUST be workspace-owned and subject to workspace isolation (scoped writes + row-level security), consistent with every other workspace-owned table; the maintenance job writes across workspaces using a system/elevated execution context rather than a single tenant's context.

**Retention & ordering guarantee (US3)**

- **FR-015**: The retention job MUST reclaim expired data by dropping whole expired monthly partitions and MUST NOT issue bulk row deletes on these tables.
- **FR-016**: For any table whose raw rows feed the daily rollups, the retention job MUST verify that the rollups covering a partition's date range are complete before dropping that partition; if coverage is missing or incomplete, the partition MUST be retained and the skip recorded.
- **FR-017**: The retention job MUST apply each table's own retention window (observations 90 days, attempts 90 days, alert events 1 year, webhook events 90 days, daily rollups 2 years) and MUST leave any partition still within its window in place.
- **FR-018**: The retention cutoff MUST be applied deterministically at partition granularity: a partition is eligible for drop only when its entire date range is older than the table's retention cutoff.
- **FR-019**: For tables with no rollup dependency, the retention job MUST drop expired partitions by age alone (no rollup check), using that table's retention window.
- **FR-020**: Retention MUST be idempotent and safe to re-run and to run concurrently: an already-dropped partition is not an error, and a partition is never dropped twice.

**Soft-reference tolerance (US4)**

- **FR-021**: Readers that follow a soft-reference id into a partitioned raw table MUST tolerate the referenced row being absent (because its partition was dropped) and MUST rely on the denormalized fields carried on the current-state row; such reads MUST NOT error, 500, or silently drop the current-state row.
- **FR-022**: The maintenance process MUST provide a dangling-soft-reference tolerance check that treats a soft reference pointing into a dropped/absent partition as an expected, tolerated condition (not data corruption), for operator visibility.

**Operability**

- **FR-023**: Each maintenance job MUST record what it did (partitions created, rollups written, partitions dropped or skipped-pending-rollups, tables skipped as absent) for operator observability.
- **FR-024**: Optional vacuum/analyze housekeeping on partitions MUST NOT be able to block or fail the core create / rollup / drop guarantees.
- **FR-025**: All timestamps used for partition bounds, retention cutoffs, and rollup dates MUST be timezone-aware (TIMESTAMPTZ semantics), computed consistently in UTC.

### Key Entities *(include if feature involves data)*

- **Partitioned table registry entry**: A description of one append-heavy table under maintenance — its name, partition key column, partition granularity (monthly), whether its raw rows feed rollups, and its retention window. Drives all three jobs.
- **Monthly partition**: One physical monthly slice of an append-heavy table, bounded by a start/end instant on the partition key. Created in advance, dropped when expired.
- **Daily rollup row (`variant_price_daily_rollups`)**: A durable per-(workspace, variant, day) pricing summary — client price, min/avg/max comparable competitor price, alert type, comparable-competitor count, currency, product/variant identity, date. Outlives raw retention; retained two years.
- **Soft reference**: A plain id column on a current-state row (e.g. a match's current-price/observation pointer) pointing into a partitioned raw table with no foreign key. May dangle after retention; readers tolerate it.
- **Maintenance run report**: The per-run record of actions taken (created / rolled-up / dropped / skipped / absent) for operator visibility.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Across any month boundary, zero writes to the append-heavy tables fail for lack of a partition — the next month's partition always exists before the month begins.
- **SC-002**: Running any maintenance job twice in a row produces the same end state as running it once (no duplicate partitions, no duplicate or corrupted rollup rows, no double drop, no error).
- **SC-003**: 100% of raw partitions are reclaimed by partition drop and 0% by bulk row delete.
- **SC-004**: No raw partition is ever dropped before the daily rollups covering its date range are verified complete — a partition with missing rollups is always retained.
- **SC-005**: Each append-heavy table retains no data older than its configured retention window (90 days for observations/attempts/webhooks, 1 year for alert events, 2 years for rollups), keeping table size bounded and predictable at production volume.
- **SC-006**: Every daily rollup row reflects exactly one (workspace, variant, day) and correctly excludes currency-mismatched competitor prices from its aggregates.
- **SC-007**: 100% of reader paths that follow a soft reference into a dropped partition return correct denormalized results without error.

## Assumptions

- The append-heavy raw tables (`price_observations` from 07, `price_alert_events` from 09, `request_attempts` from 10) already exist, already partitioned by their introducing specs; this feature adds only the maintenance/rollup/retention jobs and does not create or alter those raw tables.
- The rollup target table `variant_price_daily_rollups` does NOT yet exist: SPEC-09 defined its shape but explicitly deferred its creation to this spec. This feature therefore creates that table (a single Alembic migration on the current head), as a plain workspace-owned RLS table (not partitioned; retained 2 years by row age, since it is small and long-lived), then populates it. See FR-009a.
- `webhook_events` is introduced by a later spec (SPEC-16) and does not yet exist; the registry includes it but the jobs skip it until it is present (FR-002).
- The daily rollup source data (client price, comparable competitor prices, alert type, comparable count, currency) is available from the current-price / price-comparison surfaces produced by SPEC-09; this feature reads and aggregates that data rather than recomputing comparison logic.
- Maintenance jobs run in the existing background/worker (or scheduler) service on a periodic schedule; scheduling cadence and orchestration reuse the platform's existing periodic-job mechanism. Exact cadence is an operational configuration detail, provided the P1 guarantee (next-month partition exists before the month starts) holds with comfortable lead time.
- The maintenance jobs run under a system/elevated execution context that can act across all workspaces (analogous to the scheduler's system session), while still writing workspace-owned rollup rows with correct workspace ownership.
- Retention windows and the set of partitioned tables are configuration derived from the project spec's Section 29 defaults and may be adjusted operationally without changing the job logic.
- "Verified complete" rollups for a partition means the daily rollups exist for every date the partition can contain that had source data; a day with no source data requires no rollup to be considered covered.
