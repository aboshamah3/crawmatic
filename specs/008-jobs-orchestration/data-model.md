# Phase 1 Data Model: Jobs & Orchestration

Two new tables in `libs/shared/app_shared/models/jobs.py`, exact §22 shapes. Both are **non-partitioned, workspace-owned** (`WorkspaceScopedBase` — `workspace_id NOT NULL`, indexed), added to `WORKSPACE_OWNED_MODELS`, and given `emit_rls_policy` (ENABLE + FORCE + fail-closed) in the creating Alembic migration. Enum-like columns use `enum_column` (app-validated `VARCHAR`, never a DB enum). Timestamps are `TZDateTime` (`TIMESTAMPTZ`, naive rejected). Per §22 both tables carry **`created_at` only** (no `updated_at`) — so they use explicit `created_at` columns, **not** `TimestampMixin`.

New enums added to `app_shared.enums` (all `StrEnum` → `VARCHAR`):
- `ScrapeScope`: `WORKSPACE`, `COMPETITOR`, `PRODUCT`, `VARIANT`, `PRODUCT_GROUP`, `MATCH` (§22 "Refresh scopes"; shared with `refresh_rules` in a later spec). This spec's endpoints produce only `MATCH` and `VARIANT`.
- `ScrapeJobType`: `MANUAL`, `SCHEDULED`, `API_TRIGGERED`, `RETRY_FAILED`, `DISCOVERY` (§22). Direct API runs record `MANUAL`.
- `ScrapeJobStatus`: `PENDING`, `RUNNING`, `COMPLETED`, `PARTIAL_FAILED`, `FAILED`, `CANCELLED` (§22).
- `ScrapeJobSource`: `API`, `SCHEDULER`, `INTERNAL`, `PLUGIN` (§22). Direct API runs record `API`.
- `ScrapeTargetStatus`: `PENDING`, `STARTED`, `COMPLETED`, `FAILED`, `SKIPPED`.
- Target `error_code` reuses the existing `ScrapeErrorCode` (§34) vocabulary — no new enum.

---

## Entity: ScrapeJob (`scrape_jobs`) — job header

One triggered scraping run at a given scope. Owns lifecycle status, priority, provenance, aggregate counters, lifecycle timestamps. Not partitioned; single-column PK (`id`) **plus** `unique(workspace_id, id)` so a target can composite-FK its parent job workspace-locally (same pattern as `competitors`).

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | UUID (uuidv7) | no | PK (default `new_uuid7`) |
| `workspace_id` | UUID | no | indexed; FK → `workspaces.id`; RLS column; part of `unique(workspace_id, id)` |
| `type` | `ScrapeJobType` VARCHAR | no | `MANUAL` for direct API runs |
| `scope` | `ScrapeScope` VARCHAR | no | `MATCH` / `VARIANT` in this spec |
| `product_id` | UUID | yes | soft scope ref (no FK) |
| `product_variant_id` | UUID | yes | soft scope ref (set for scope VARIANT) |
| `product_group_id` | UUID | yes | soft scope ref |
| `competitor_id` | UUID | yes | soft scope ref |
| `match_id` | UUID | yes | soft scope ref (set for scope MATCH) |
| `status` | `ScrapeJobStatus` VARCHAR | no | `PENDING` at creation → `RUNNING` → terminal |
| `priority` | `MatchPriority` VARCHAR | no | reuses the existing priority enum; default `NORMAL` |
| `total_targets` | INT | no | count of targets created (0 for an empty fan-out) |
| `success_count` | INT | no | **aggregated** from targets; default 0 |
| `failure_count` | INT | no | **aggregated** from targets; default 0 |
| `skipped_count` | INT | no | **aggregated** from targets; default 0 |
| `requested_by` | UUID | yes | the authenticated principal id (API runs) |
| `source` | `ScrapeJobSource` VARCHAR | no | `API` for direct API runs |
| `started_at` | TIMESTAMPTZ | yes | set when `dispatch_job` begins |
| `completed_at` | TIMESTAMPTZ | yes | set at deterministic finalization |
| `created_at` | TIMESTAMPTZ | no | default `now(utc)` (no `updated_at`, §22) |

**Constraints**: `UniqueConstraint("workspace_id", "id")` (the composite-FK target); `ForeignKeyConstraint(["workspace_id"], ["workspaces.id"])`. Indexes: `(workspace_id)` (from the mixin). Counter columns are **never** incremented per-target (FR-018) — only overwritten by `aggregate_counts`.

**State machine** (`status`): `PENDING` → `RUNNING` (dispatch begins) → `COMPLETED` | `PARTIAL_FAILED` | `FAILED` (deterministic finalization, D6). A zero-target job goes `PENDING` → `COMPLETED` immediately at creation, skipping `RUNNING`/dispatch. `CANCELLED` is a member of the enum (§22) but not produced by this spec's endpoints.

---

## Entity: ScrapeJobTarget (`scrape_job_targets`) — one match in a job

One match to be scraped within a job. Owns its own status, lifecycle timestamps, lock timestamp, error code. Unique per `(scrape_job_id, match_id)`. Not partitioned.

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | UUID (uuidv7) | no | PK |
| `workspace_id` | UUID | no | indexed; RLS column; part of the composite FK to the job |
| `scrape_job_id` | UUID | no | part of `unique(scrape_job_id, match_id)`; composite FK → `scrape_jobs(workspace_id, id)` |
| `match_id` | UUID | no | soft ref to `competitor_product_matches` (no FK — §22 soft-ref, SPEC-07 precedent); part of the unique |
| `status` | `ScrapeTargetStatus` VARCHAR | no | `PENDING` at creation |
| `locked_at` | TIMESTAMPTZ | yes | set by the in-flight lock (SPEC-11); read by stall recovery to skip locked matches |
| `started_at` | TIMESTAMPTZ | yes | set on `PENDING→STARTED` |
| `completed_at` | TIMESTAMPTZ | yes | set on transition to a terminal status |
| `error_code` | `ScrapeErrorCode` VARCHAR | yes | set on `FAILED` (§34 vocabulary) |
| `created_at` | TIMESTAMPTZ | no | default `now(utc)` (no `updated_at`, §22) |

**Constraints**:
- `UniqueConstraint("scrape_job_id", "match_id", name="uq_scrape_job_targets_scrape_job_id_match_id")` — the §22 unique; guarantees one target per match per job (prevents duplicate work within a job and is the arbiter for the set-based target insert).
- `ForeignKeyConstraint(["workspace_id", "scrape_job_id"], ["scrape_jobs.workspace_id", "scrape_jobs.id"], name="fk_scrape_job_targets_workspace_scrape_job_scrape_jobs")` — a target can only reference a job **in its own workspace** (structural isolation, the `competitor_product_matches`→parents pattern).
- `ForeignKeyConstraint(["workspace_id"], ["workspaces.id"])` — the RLS anchor.
- `match_id` carries **no** FK (soft ref, §22 / SPEC-07 precedent — a match may be archived/deleted without cascading job history; workspace consistency of the match is enforced at creation time in the service, which only inserts targets for matches resolved within the workspace).

**State machine** (`status`): `PENDING` → `STARTED` → `COMPLETED` | `FAILED` | `SKIPPED`. `PENDING` (never started) past the stall timeout is what `recover_stalled_batches` re-dispatches (D4). `mark_target` (`app_shared.jobs.targets`) is the single writer of these transitions and their timestamps/`error_code`; it never touches the parent job's counters (D5).

---

## Relationships & isolation

- Both tables carry `workspace_id` (RLS + app scoping) with a real FK to `workspaces.id` (the RLS anchor).
- `scrape_job_targets` → `scrape_jobs` is a **workspace-local composite FK** (`(workspace_id, scrape_job_id) → scrape_jobs(workspace_id, id)`), so a cross-workspace target→job reference is structurally impossible, not just app-filtered.
- Scope refs on the job (`product_id`/`product_variant_id`/`product_group_id`/`competitor_id`/`match_id`) and `scrape_job_targets.match_id` are **soft** (plain indexed/nullable UUID columns, no FK) — matching §22's soft-reference philosophy and the SPEC-07 observations precedent.
- Cross-workspace isolation is enforced by (1) `scoped_select`/`scoped_get` (app layer, CI-guarded via `WORKSPACE_OWNED_MODELS`) and (2) DB RLS on both tables (`emit_rls_policy`, fail-closed) — the two-layer model.

## Table summary

| Table | Partitioned | PK | Unique | Composite FK | RLS |
|-------|-------------|----|--------|--------------|-----|
| `scrape_jobs` | no | `(id)` | `(workspace_id, id)` | — | `emit_rls_policy` |
| `scrape_job_targets` | no | `(id)` | `(scrape_job_id, match_id)` | `(workspace_id, scrape_job_id) → scrape_jobs(workspace_id, id)` | `emit_rls_policy` |

## Transport / logic shapes (not tables)

- `app_shared.jobs.batching.Batch` — `batch_index: int`, `mode: ScrapeProfileMode`, `domain: str`, `match_ids: list[uuid.UUID]`. Produced by `plan_batches(targets, *, http_min=50, http_max=200)`; each `(domain, mode)` group chunked to ≤ `http_max` (and merged toward ≥ `http_min` where the group allows).
- `app_shared.jobs.targets.Counts` — `success: int`, `failure: int`, `skipped: int`, `total: int` (the `GROUP BY status` result consumed by `resolve_finalized_status`).
- `app_shared.jobs.service` return — `(job_id: uuid.UUID, status: ScrapeJobStatus)` so the router can 202-return the job id + whether it already finalized (zero-target COMPLETED) without a re-read.
