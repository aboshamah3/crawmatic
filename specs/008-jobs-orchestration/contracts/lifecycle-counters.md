# Contract: lifecycle, counters, target state (`app_shared.jobs.lifecycle`, `app_shared.jobs.targets`)

## `resolve_finalized_status(success, failure, skipped, total) -> ScrapeJobStatus` (pure, `lifecycle`)

Single ordered, deterministic rule (FR-019, D6):

1. `total == 0` → **COMPLETED**.
2. `success == total` → **COMPLETED** (all succeeded).
3. `success > 0` and (`failure > 0` or `skipped > 0`) → **PARTIAL_FAILED**.
4. `success == 0` → **FAILED** (none succeeded, with failures/skips present).

Skipped targets increment `skipped_count` and participate in the rule (a job of only skipped/failed targets → FAILED; some-success-some-skipped → PARTIAL_FAILED). This is the ordered decision rule whose boundary values are unit-tested for determinism (constitution testing gate).

## `stall_window(now, timeout_seconds) -> int` (pure, `lifecycle`)

`floor(now.timestamp() / timeout_seconds)` — the bucket used to make stall re-dispatch itself idempotent (see stall-recovery.md).

## `aggregate_counts(session, scrape_job_id, workspace_id) -> Counts` (`targets`)

- One scoped `SELECT status, COUNT(*) FROM scrape_job_targets WHERE scrape_job_id = :id GROUP BY status` (RLS + `scoped_select`-style predicate).
- Maps to `Counts(success, failure, skipped, total)`.
- Callers (`refresh_job_counters`, `finalize_jobs`) write the totals to the job row in **one** `UPDATE` — never a per-target increment (FR-018, SC-004, Principle VIII).

## `mark_target(session, *, workspace_id, scrape_job_id, match_id, status, error_code=None) -> None` (`targets`)

- The single writer of a target's `status` transition (`PENDING→STARTED→COMPLETED/FAILED/SKIPPED`) + timestamps (`started_at` on STARTED, `completed_at` on a terminal status) + `error_code` on FAILED (§34).
- Touches **only** the target row — never the parent job counters (D5). Workspace-scoped.
- This is the FR-017 seam the spider/finalizer calls as targets progress; in this spec its behavior is exercised by tests simulating transitions (US3 Independent Test), the spider wiring lands with SPEC-09's post-persistence step.

## Finalization (`finalize_jobs`, maintenance task)

- For each non-terminal job whose targets are **all** terminal: `counts = aggregate_counts(...)`; write counts; `status = resolve_finalized_status(...)`; `completed_at = now`. Idempotent (re-running on an already-finalized job is a no-op).

## Tests

- `test_jobs_lifecycle.py`: the four rule branches + zero-target + skipped-only + mixed boundary values.
- `test_jobs_counters.py`: `aggregate_counts` over a fake session returns correct `Counts`; finalize writes one UPDATE; `mark_target` never mutates job counters.
- Live (`test_jobs_counters_finalize_live.py`): simulate real target transitions → counters aggregate, status finalizes COMPLETED/PARTIAL_FAILED/FAILED, `completed_at` set.
