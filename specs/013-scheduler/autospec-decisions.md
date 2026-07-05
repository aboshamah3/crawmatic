# Autospec Decisions — SPEC-13 Scheduler

Auto-answered questions during the pipeline (doc-first per the autospec skill). Format:
`- [step] Q: <question> → A: <answer> (source)`

## specify

- [specify] Q: Cron expression format/timezone? → A: standard 5-field cron evaluated in UTC
  (source: default — doc §28 names `cron_expression` but not a dialect; UTC matches the
  project-wide TIMESTAMPTZ/UTC convention). Recorded in spec Assumptions.
- [specify] Q: interval_minutes semantics? → A: next run = run_time + interval_minutes
  (source: default — doc lists `interval_minutes` without formula; standard fixed-interval
  meaning). Recorded in spec Assumptions.
- [specify] Q: Behavior when next_run_at is far in the past after downtime? → A: fire once on
  recovery, advance to next FUTURE occurrence (no per-missed-interval catch-up)
  (source: default — doc silent; avoids thundering-herd, matches "duplicates cheap, missed runs
  costly" posture of §28). Captured as FR-016 / SC-005.
- [specify] Q: Behavior when a due rule's scope resolves to zero active matches? → A: still
  advance next_run_at/last_run_at, skip dispatch (source: default — doc silent; prevents a rule
  being perpetually re-selected). Captured as FR-015 / SC-006 / US2 AS-4.
- [specify] Q: Is `priority` load-bearing for claim ordering? → A: advisory only; due rules
  claimed in `next_run_at` order per §28 SQL (source: doc §28 `ORDER BY next_run_at`).
- [specify] Q: Enqueue vs commit ordering? → A: enqueue dispatch INSIDE claiming txn before
  commit; never commit-then-enqueue (source: doc §28 explicit). Captured as FR-012/FR-014.
- [specify] Q: Global pass lock allowed? → A: no global `lock:scheduler:refresh-rules`/advisory
  pass lock (negates SKIP LOCKED); per-rule `pg_advisory_xact_lock` permissible belt-and-braces
  (source: doc §28 explicit). Captured as FR-009.

## clarify

- [clarify] Outcome: no critical ambiguities worth a formal (blocking) clarification. Spec is
  derived directly from the authoritative master doc (§22/§25/§28/§32), which resolves every
  material decision. No user questions asked.
- [clarify] Q: Scheduler poll/tick interval? → A: deferred to plan (source: doc silent;
  low-impact operational knob with an obvious default, does not change architecture or acceptance
  tests). Plan will pick a default (reuse a Settings cadence knob rather than hardcode).
- [clarify] Q: Uniqueness of a refresh rule per (scope, target)? → A: not constrained; multiple
  rules for the same scope/target allowed (source: default — doc §22 lists no unique constraint;
  operators may legitimately want two cadences on one scope). No spec change needed.
- [plan] R: BYPASSRLS system session for cross-tenant refresh-rule claim (scheduler must scan all workspaces). Justified: mirrors existing auth get_system_session seam; app-level scoped_select scoping retained for every job/target read/write; RLS still enforced on refresh_rules for the CRUD (api) path. (source: doc §28 cross-tenant enqueuer + §32 isolation)
- [plan] R: new dependency croniter (pure-Python, scraping-free) for 5-field UTC cron next_run_at; interval_minutes = run_time + minutes. (source: plan research R-cadence; no cron lib previously present)
- [plan] R: scope target FK columns ON DELETE CASCADE so deleting a product/variant/group/competitor/match removes referencing refresh rules cleanly (FR-020).
- [plan] R: SCHEDULER_POLL_INTERVAL_SECONDS default 30s + SCHEDULER_CLAIM_BATCH_LIMIT as new Settings knobs (interval deferred from clarify).

## checklist

- [checklist] Generated checklists/scheduler.md (33 requirements-quality items across concurrency,
  cadence, isolation, scope-resolution, cross-cutting). Completed 33/33; requirements.md remains
  16/16.
- [checklist] Gap CHK008 → added FR-021: per-rule error isolation. One failing rule must not roll
  back/block others in the pass; failing rule keeps next_run_at and retries later. Reconciled the
  plan away from a single batch commit to a per-rule claim→process→commit loop (single batch txn
  rejected: a SAVEPOINT rollback can't un-send an already-enqueued Celery dispatch → orphaned
  dispatch). Updated scheduler-loop.md + research.md R5 + plan.md accordingly.
- [checklist] Gap CHK015 → strengthened FR-003 to reject invalid cron / non-positive interval at
  write time.
- [checklist] Gap CHK021/CHK020 → strengthened FR-005: refresh_rules registered in the
  workspace-owned model registry (unscoped-query CI guard covers it); API CRUD path uses an
  RLS-enforced (non-bypass) session; only the scheduler claim uses BYPASSRLS.
