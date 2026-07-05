# Contract: Scheduler Refresh Pass (US2 / US3)

The custom DB-driven enqueuer. Lives in `apps/scheduler`, invoked from the existing
`scheduler_app.py` loop on a poll interval. Scraping-free (Principle I / FR-019).

## Modules

- `apps/scheduler/app/scheduler/refresh.py` — `run_refresh_pass(session_factory, *, now, batch_limit) -> int`
  (returns number of rules fired). Owns the per-rule claim → process → commit loop with per-rule
  error isolation (FR-021).
- `apps/scheduler/app/scheduler/scheduler_app.py` — extended: a second interval accumulator
  (`SCHEDULER_POLL_INTERVAL_SECONDS`) that, each tick, calls `run_refresh_pass`. Existing
  SIGTERM/SIGINT clean shutdown and best-effort posture preserved: a pass that raises is logged
  and swallowed (never crash-loops the process).

## Session (research R2)

The refresh pass runs on the **BYPASSRLS** `get_system_session()` (new helper in
`app_shared/database.py`, bound to `SYSTEM_DATABASE_URL` → falls back to `AUTH_DATABASE_URL`).
This is required because the claim is cross-tenant. Workspace isolation is preserved by app-level
scoping: job creation uses `scoped_select(CompetitorProductMatch, rule.workspace_id)` and sets
`workspace_id` explicitly on inserted job/target rows. The API CRUD path never uses this
session — it uses the ordinary RLS-enforced request session (FR-005).

## Claim + process: one transaction PER RULE (FR-007/008/009/012/021)

Each rule is claimed, processed, and committed in **its own transaction** — not a single
batch transaction. This is deliberate: it is what makes enqueue-before-commit (FR-012) and
per-rule error isolation (FR-021) coexist. A single batch transaction with per-rule SAVEPOINTs
is **rejected** — a savepoint rollback cannot un-send an already-enqueued Celery task, which
would orphan a dispatch against a rolled-back `scrape_job_id`.

```python
fired = 0
while fired < batch_limit:
    with session_factory() as session:            # fresh transaction per rule
        now = datetime.now(timezone.utc)
        rule = session.execute(
            select(RefreshRule)
            .where(RefreshRule.enabled, RefreshRule.next_run_at <= now)
            .order_by(RefreshRule.next_run_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalars().first()
        if rule is None:
            break                                   # no more due rules
        try:
            run_time = now
            target_id = <non-null scope target id for rule.scope, or None for WORKSPACE>
            create_scope_job(                        # resolves active matches; creates
                session,                             # ScrapeJob+targets; ENQUEUES dispatch.
                workspace_id=rule.workspace_id,      # zero matches -> no job/dispatch (FR-015)
                scope=rule.scope, target_id=target_id, requested_by=None,
                job_type=ScrapeJobType.SCHEDULED, source=ScrapeJobSource.SCHEDULER,
            )
            rule.last_run_at = run_time
            rule.locked_at   = run_time
            rule.next_run_at = compute_next_run_at(rule, run_time)   # strictly future (FR-013/016)
            session.commit()                         # enqueue already happened; commit last
            fired += 1
        except Exception:
            session.rollback()                       # FR-021: only THIS rule is undone;
            logger.exception("refresh rule %s failed", rule.id)   # lock released, next_run_at
            # a poison rule releases its lock on rollback; because next_run_at is unchanged it is
            # still due, so bound total attempts per pass to avoid re-selecting it forever:
            break                                    # end pass; next tick retries remaining rules
```

- `FOR UPDATE SKIP LOCKED` ⇒ sibling instances (and successive per-rule txns) skip already-held
  rows → each due rule fired by exactly one instance (US3 AS-1, SC-003).
- **No** global/advisory pass-lock (FR-009). Per-rule `pg_advisory_xact_lock` is permitted but not
  used (SKIP LOCKED already isolates).
- Priority is **not** in ORDER BY (advisory only, §28 / autospec-decisions).
- Note: because a failed rule keeps its `next_run_at` (FR-021, retry-safe) it would be re-selected
  by the same `next_run_at <= now` predicate within one pass; the pass therefore stops on first
  failure (or, equivalently, tracks already-attempted rule ids) so a poison rule cannot spin the
  loop. Successfully fired rules have advanced `next_run_at` and are naturally not re-selected.

## Ordering & crash safety (FR-012/014/021, US3)

- **Enqueue-before-commit is structural, per rule**: `create_scope_job` enqueues the Celery
  `scrape_dispatch.dispatch_job` task inside the rule's open transaction; that rule's `commit()`
  comes after the enqueue. Never commit-then-enqueue.
- **Per-rule isolation** (FR-021): one rule's failure rolls back only its own transaction; other
  rules claimed earlier in the pass are already committed, and later due rules are retried on the
  next tick. No poison rule blocks the batch.
- **Crash before commit** → that rule's transaction rolls back → its SKIP-LOCKED row lock releases
  → `next_run_at`/`last_run_at`/`locked_at` unchanged → a later pass re-claims (US3 AS-2). Any
  dispatch that leaked to the broker is neutralized by the SPEC-08 idempotent dispatch guard
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
