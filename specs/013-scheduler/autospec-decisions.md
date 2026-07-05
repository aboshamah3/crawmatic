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
