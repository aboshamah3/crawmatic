# Contract: Scheduler Refresh Pass (US2 / US3)

The custom DB-driven enqueuer. Lives in `apps/scheduler`, invoked from the existing
`scheduler_app.py` loop on a poll interval. Scraping-free (Principle I / FR-019).

## Modules

- `apps/scheduler/app/scheduler/refresh.py` — `run_refresh_pass(session, *, now, batch_limit) -> int`
  (returns number of rules fired). Owns the claim + per-rule processing + single commit.
- `apps/scheduler/app/scheduler/scheduler_app.py` — extended: a second interval accumulator
  (`SCHEDULER_POLL_INTERVAL_SECONDS`) that, each tick, opens a `get_system_session()` and calls
  `run_refresh_pass`. Existing SIGTERM/SIGINT clean shutdown and best-effort posture preserved:
  a pass that raises is logged and swallowed (never crash-loops the process).

## Session (research R2)

`run_refresh_pass` runs on the **BYPASSRLS** `get_system_session()` (new helper in
`app_shared/database.py`, bound to `SYSTEM_DATABASE_URL` → falls back to `AUTH_DATABASE_URL`).
This is required because the claim is cross-tenant. Workspace isolation is preserved by app-level
scoping: job creation uses `scoped_select(CompetitorProductMatch, rule.workspace_id)` and sets
`workspace_id` explicitly on inserted job/target rows.

## Claim (FR-007/008/009 — one transaction per pass)

```python
now = datetime.now(timezone.utc)
stmt = (
    select(RefreshRule)
    .where(RefreshRule.enabled, RefreshRule.next_run_at <= now)
    .order_by(RefreshRule.next_run_at)
    .with_for_update(skip_locked=True)
    .limit(batch_limit)               # SCHEDULER_CLAIM_BATCH_LIMIT (default 100)
)
rules = session.execute(stmt).scalars().all()
```
- `FOR UPDATE SKIP LOCKED` ⇒ sibling instances skip already-claimed rows → each due rule fired by
  exactly one instance (US3 AS-1, SC-003).
- **No** global/advisory pass-lock (FR-009). Per-rule `pg_advisory_xact_lock` is permitted but not
  used (SKIP LOCKED already isolates).
- Priority is **not** in ORDER BY (advisory only, §28 / autospec-decisions).

## Per-rule processing (inside the same transaction, before commit)

For each claimed `rule` (research R3/R4):
```python
run_time = now
target_id = <the non-null scope target id for rule.scope, or None for WORKSPACE>
job_id, status = create_scope_job(
    session,
    workspace_id=rule.workspace_id,
    scope=rule.scope,
    target_id=target_id,
    requested_by=None,
    job_type=ScrapeJobType.SCHEDULED,
    source=ScrapeJobSource.SCHEDULER,
)   # resolves active matches; creates ScrapeJob+targets; ENQUEUES dispatch (before commit).
    # zero active matches -> (None, None), no job, no dispatch (FR-015).
rule.last_run_at = run_time
rule.locked_at   = run_time
rule.next_run_at = compute_next_run_at(rule, run_time)   # strictly future (FR-013/016)
# ... continue loop ...
session.commit()   # single commit for the whole batch; enqueues already happened before it
```

## Ordering & crash safety (FR-012/014, US3)

- **Enqueue-before-commit is structural**: `create_scope_job` enqueues the Celery
  `scrape_dispatch.dispatch_job` task inside the open transaction; the batch `commit()` is the only
  commit and comes after all enqueues. Never commit-then-enqueue.
- **Crash before commit** → transaction rolls back → SKIP-LOCKED row locks release →
  `next_run_at`/`last_run_at`/`locked_at` unchanged → a later pass re-claims (US3 AS-2). Any dispatch
  that leaked to the broker is neutralized by the SPEC-08 idempotent dispatch guard
  (`dispatch_key` Redis `SET NX`) + SPEC-11 in-flight match lock — a possible duplicate, never a
  missed run (US3 AS-3).
- **No global lock** ⇒ horizontal scaling intact (US3 AS-4).

## Backlog & zero-match (FR-015/016)

- `next_run_at` far in the past → fired **once** this pass; `compute_next_run_at` bases on
  `run_time = now`, so the new value is the next future occurrence (cron) or `now + interval` — no
  per-missed-interval catch-up (SC-005).
- Zero active matches → schedule still advanced, no dispatch (SC-006); the rule cannot get stuck
  perpetually due.

## Cadence computation (`app_shared/scheduling/cadence.py`, research R1)

`compute_next_run_at(rule, run_time) -> datetime`:
- cron: `croniter(rule.cron_expression, run_time_utc).get_next(datetime)` (tz-aware UTC).
- interval: `run_time + timedelta(minutes=rule.interval_minutes)`.
Also used by the API to compute the first `next_run_at` on create/update (base = now).

## Acceptance mapping

US2 AS-1..6, US3 AS-1..4, SC-002/003/005/006 → covered by the claim, enqueue-before-commit,
zero-match advance, and backlog rules above.
