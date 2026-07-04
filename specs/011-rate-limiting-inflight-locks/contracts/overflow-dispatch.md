# Contract: Requeue-Cap Overflow & Re-Dispatch

**Files**: `libs/shared/app_shared/enums.py`, `libs/shared/app_shared/jobs/targets.py`,
`apps/workers/app/workers/tasks_jobs.py`, plus the spider overflow call. Reuses the existing
`app_shared.messaging.enqueue` producer (SPEC-08). Covers FR-018, FR-019; US3; SC-003.

---

## 1. Enum — `ScrapeTargetStatus.DEFERRED` (NEW; VARCHAR, no migration)
Add `DEFERRED = "DEFERRED"`. Non-terminal (NOT added to any `_TERMINAL_TARGET_STATUSES` set in
`app_shared/jobs/targets.py` or `apps/workers/.../tasks_jobs.py`). Renders as VARCHAR via
`enum_column` ⇒ **no Alembic migration**.

## 2. `mark_target` — accept the DEFERRED transition
`app_shared/jobs/targets.py::mark_target` must allow `status=ScrapeTargetStatus.DEFERRED`:
- stamp **no** `completed_at` (not terminal) and **no** `started_at`;
- leave `error_code` as passed (the overflow path passes `RATE_LIMITED` — FR-020, so the
  deferred target still carries the reason it was throttled).
No other `mark_target` behavior changes.

## 3. Spider overflow (on requeue-cap exceed — `spider-integration.md` step 2)
When `requeue_count > REQUEUE_MAX_ATTEMPTS` **or** `cumulative_wait > REQUEUE_MAX_TOTAL_WAIT_SECONDS`:
1. `await run_in_thread(mark_target_deferred, workspace_id, scrape_job_id, match_id)` — one
   off-reactor `workspace_txn` that calls `mark_target(status=DEFERRED, error_code=RATE_LIMITED)`.
2. `await run_in_thread(enqueue, SCRAPE_DISPATCH_JOB, queue="scrape_dispatch",
   kwargs={"scrape_job_id": str(scrape_job_id)})` — fire-and-forget re-dispatch via the
   existing producer seam (the spider imports `app_shared.messaging` + `app_shared.task_names`
   only; it never imports `apps/workers` — Principle I).
3. **Do not** yield a request for this target; the Scrapyd slot is freed immediately (SC-003).
Any semaphore slot held for this attempt is released first; no match lock was held (overflow
happens before the lock is acquired — see step ordering in `spider-integration.md`).

## 4. Re-dispatch expansion (`apps/workers/app/workers/tasks_jobs.py`)
`dispatch_job`'s target-expansion query (`_expand_job`) today selects
`ScrapeJobTarget.status == PENDING`. Change to include DEFERRED:
```python
ScrapeJobTarget.status.in_((ScrapeTargetStatus.PENDING, ScrapeTargetStatus.DEFERRED))
```
- On pickup the target transitions `DEFERRED → STARTED` (via the existing dispatch marking),
  re-entering the normal path — re-subject to the SPEC-11 lock + limiter gate, so no double-run
  (FR-019, US3 AS3).
- The idempotent-dispatch guard (SPEC-08 `SET NX` / persisted `jobid`) is unchanged; a repeated
  overflow `enqueue` for the same job coalesces at the batch level (a job already re-dispatching
  is not double-run).
- **Overflow-loop ceiling** (edge case): a job in a **terminal** status is skipped by
  `dispatch_job` (existing `_TERMINAL_JOB_STATUSES` guard), so a target cannot overflow forever
  beyond its parent job's lifecycle. A DEFERRED target keeps the job non-terminal (DEFERRED is
  not in the "all terminal" set) until it finally resolves.

## 5. Observability
- The DEFERRED overflow carries `RATE_LIMITED` on the target (FR-020, US4 AS1).
- Emit a structured log/counter `rate_limit.overflow` at overflow and `rate_limit.requeue` at
  each backoff (§31; see `observability.md`).

## Invariants (tested — SC-003)
- A request forced to deny past the cap is DEFERRED + enqueued exactly once per cap-hit and is
  **not** re-parked in the spider (slot freed).
- A DEFERRED target is picked up by the next `dispatch_job` for its job and re-runs through the
  full lock+limiter gate.
