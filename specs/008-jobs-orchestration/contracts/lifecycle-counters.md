# Contract: lifecycle, counters, target state (`app_shared.jobs.lifecycle`, `app_shared.jobs.targets`)

## `resolve_finalized_status(success, failure, skipped, total) -> ScrapeJobStatus` (pure, `lifecycle`)

Single ordered, deterministic **failure-centric** rule (FR-019, D6; skips are non-fatal — analyze A1 remediation):

1. `total == 0` → **COMPLETED**.
2. `failure == 0` → **COMPLETED** (covers all-success, success+skipped, and skipped-only — nothing failed).
3. `failure > 0` and `success > 0` → **PARTIAL_FAILED**.
4. `failure > 0` and `success == 0` → **FAILED**.

Skipped targets increment `skipped_count` but are non-fatal: a job with no failures finalizes COMPLETED regardless of skips (skipped-only → COMPLETED); PARTIAL_FAILED requires at least one real failure alongside at least one success; FAILED requires failures with zero successes. This is the ordered decision rule whose boundary values are unit-tested for determinism (constitution testing gate).

## `stall_window(now, timeout_seconds) -> int` (pure, `lifecycle`)

`floor(now.timestamp() / timeout_seconds)` — the bucket used to make stall re-dispatch itself idempotent (see stall-recovery.md).

## `aggregate_counts(session, scrape_job_id, workspace_id) -> Counts` (`targets`)

- One scoped `SELECT status, COUNT(*) FROM scrape_job_targets WHERE scrape_job_id = :id GROUP BY status` (RLS + `scoped_select`-style predicate).
- Maps to `Counts(success, failure, skipped, total)`.
- Callers (`refresh_job_counters`, `finalize_jobs`) write the totals to the job row in **one** `UPDATE` — never a per-target increment (FR-018, SC-004, Principle VIII).

## `mark_target(session, *, workspace_id, scrape_job_id, match_id, status, error_code=None) -> None` (`targets`)

- The single writer of a target's `status` transition (`PENDING→STARTED→COMPLETED/FAILED/SKIPPED`) + timestamps (`started_at` on STARTED, `completed_at` on a terminal status) + `error_code` on FAILED (§34).
- Touches **only** the target row — never the parent job counters (D5). Workspace-scoped.
- This is the FR-017 seam the scrape result path calls as targets progress. In this spec it IS wired into the SPEC-07 item pipeline `_flush_batch` (T052): each persisted item terminalizes its target (COMPLETED on success, FAILED with `error_code` otherwise) in the same reactor-safe transaction, then the batch enqueues `SCRAPE_FINALIZE_JOBS` once per affected job so finalization is event-driven (no dependence on the SPEC-13 beat).

## Finalization (`finalize_jobs`, maintenance task)

- For each non-terminal job whose targets are **all** terminal: `counts = aggregate_counts(...)`; write counts; `status = resolve_finalized_status(...)`; `completed_at = now`. Idempotent (re-running on an already-finalized job is a no-op).

## Tests

- `test_jobs_lifecycle.py`: the four rule branches + zero-target COMPLETED + skipped-only COMPLETED + success+skipped COMPLETED + failure+success PARTIAL_FAILED + failure-only/failure+skipped FAILED boundary values.
- `test_pipeline_target_terminalization.py` (T053): the pipeline result-path caller marks COMPLETED/FAILED correctly, skips null-`scrape_job_id` items, shares the batch transaction, and enqueues one finalize per distinct job.
- `test_jobs_counters.py`: `aggregate_counts` over a fake session returns correct `Counts`; finalize writes one UPDATE; `mark_target` never mutates job counters.
- Live (`test_jobs_counters_finalize_live.py`): simulate real target transitions → counters aggregate, status finalizes COMPLETED/PARTIAL_FAILED/FAILED, `completed_at` set.
