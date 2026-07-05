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

## analyze

- [analyze] Result: 0 CRITICAL, 0 HIGH, 5 LOW. 100% FR/SC coverage; external refs verified
  (single head f30c60cfa2f7; enums ScrapeScope/ScrapeJobType.SCHEDULED/ScrapeJobSource.SCHEDULER/
  MatchStatus.ACTIVE present; jobs/service.py seams present). One documented constitution
  deviation (BYPASSRLS system session) — justified, not a violation. No re-run needed (nothing
  CRITICAL/HIGH).
- [analyze] I1 (LOW) remediated: added test_refresh_pass_isolation.py + test_scheduler_concurrency_live.py
  to plan.md file tree (were in tasks T025/T026 but missing from tree).
- [analyze] U1 (LOW) remediated: FR-006 + T010 now state update-time next_run_at semantics
  explicitly (recompute only on cadence change; enable-toggle leaves next_run_at → fire-once by
  design, not a bug).
- [analyze] C1 (LOW) remediated: T019 now asserts bookkeeping confined to rule row + standard
  job/target inserts (FR-017), and cites FR-017.
- [analyze] A1 (LOW) remediated: T024 now mandates a single shared rollback path so FR-014
  (crash) and FR-021 (per-rule exception) cannot diverge.
- [analyze] I2 (LOW) remediated: spec edge cases now distinguish deleting the rule's NAMED scope
  target (CASCADE removes the rule) vs an underlying match going inactive (rule survives, resolves
  to fewer/zero matches).

## implement

- [implement] All 30 tasks (T001–T030) complete across 6 phases, one sonnet subagent per phase.
  Final suite: 1541 unit passed / 0 failed; integration 3 passed / 240 skipped / 0 errors
  (all *_live.py skip cleanly — no Postgres/Redis/Docker in build env); single alembic head
  93511d5f7885 (refresh_rules, off f30c60cfa2f7); scripts/check_workspace_scoping.py OK; import
  boundaries green (croniter added no forbidden import; scheduler stays scraping-free).
- [implement] T007 also updated the frozen-set equality test test_repository_scoping.py to include
  RefreshRule in WORKSPACE_OWNED_MODELS — the same maintenance every prior spec applies.
- [implement] Cross-tenant RefreshRule claim in refresh.py is a sanctioned unscoped read annotated
  `# noqa: workspace-scope` (the exact marker scripts/check_workspace_scoping.py requires; same
  idiom as status_cache.py User lookup + strategy/flush.py). NOT a violation — the BYPASSRLS design
  requires scanning all workspaces; app-level scoped_select is used for all job/target reads.
- [implement] ruff "invalid-noqa" on the workspace-scope pragma is a false positive vs. the repo's
  actual tooling (custom AST guard, not ruff; ruff is not a repo dependency). Pattern is repo-wide
  and required; left unchanged intentionally.
- [implement] Deferred live verifications (require live Postgres/Redis/Scrapyd; authored + skip
  cleanly): T013 CRUD+first-next_run_at+cross-workspace RLS denial; T014 alembic upgrade/downgrade
  round-trip; T023 due/zero-match/backlog pass e2e; T026 SKIP-LOCKED concurrency + crash-before-
  commit re-fire + cascade-delete. quickstart scenarios 1–5,7 deferred to live; scenarios 6 (cadence
  backlog) + 8 (import boundary) executed green here.
- [implement] Pre-existing monorepo note (NOT introduced by SPEC-13, verified via git stash):
  apps/api / apps/scheduler / apps/workers each ship a top-level package named `app`; editable .pth
  ordering makes a bare `import app...` from repo root resolve apps/api. Tests use the established
  `sys.path.insert(0, "apps/scheduler")` subprocess idiom (as test_jobs_dispatch_task.py does).
