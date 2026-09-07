# Packet PB-3 — Phase B: Stage B — Durable work, reliable schedules
Model: opus   Parallel-safe: no   Depends on: PB-2, PB-4
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: B-w4   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### B3 — Atomic due-time claim, unique occurrences, per-rule isolation, fair mode on (F07)
Acceptance criteria:
- `fire_refresh_rule` adds `.where(RefreshRule.next_run_at <= now)` under `FOR UPDATE SKIP LOCKED`; `scheduled_for = rule.next_run_at` (second precision) is inserted into the new `refresh_rule_occurrences` table BEFORE `create_scope_job`; `IntegrityError` -> rollback, return `False`. Both plan unit tests pass.
- One Alembic revision creates `refresh_rule_occurrences(rule_id UUID, scheduled_for TIMESTAMPTZ, fired_at TIMESTAMPTZ, scrape_job_id UUID, PRIMARY KEY (rule_id, scheduled_for))`. Single head preserved.
- Per-rule isolation: an exception firing one rule is recorded through the existing `RetryLedger` (`fair_queue.py:553`), sets `consecutive_failures += 1` and `next_run_at = now + min(2^n x 60 s, 6 h)`, and the pass CONTINUES; the legacy loop in `refresh.py` gets the same per-iteration `try/except` instead of `break`.
- `SCHEDULER_FAIR_QUEUE_ENABLED` default flips to `True` at `libs/shared/app_shared/config.py:768` (closes the 2026-09-04 deferred item).
- Work-level fairness: `load_due_candidates` pages `DISTINCT ON (workspace_id)` first then the remainder; `redispatch_pending_jobs` orders pending jobs round-robin by workspace and dispatches at most `SCRAPE_DISPATCH_PER_WORKSPACE_BATCHES_PER_TICK = 4` batches per workspace per tick (new setting).
- `tests/integration/test_two_schedulers_one_occurrence.py`: two threads, two sessions, real Postgres, same due rule, 50 concurrent `fire_refresh_rule` calls -> exactly one `scrape_jobs` row per occurrence.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task B3: Atomic due-time claim, unique occurrences, per-rule isolation, fair mode on, work-level fairness (F07)
>
> **Files:**
> - Modify: `apps/scheduler/app/scheduler/scheduler_app.py:840-965` (`load_due_candidates`, `fire_refresh_rule`)
> - Modify: `apps/scheduler/app/scheduler/refresh.py:69-140` (legacy loop isolation, kept as fallback)
> - Modify: `libs/shared/app_shared/config.py:768` (`SCHEDULER_FAIR_QUEUE_ENABLED: bool = True`)
> - Modify: `apps/workers/app/workers/tasks_jobs.py:1040-1130` (`redispatch_pending_jobs` interleaves workspaces)
> - Create: `alembic/versions/<rev>_refresh_rule_occurrences.py` (`refresh_rule_occurrences(rule_id UUID, scheduled_for TIMESTAMPTZ, fired_at TIMESTAMPTZ, scrape_job_id UUID, PRIMARY KEY (rule_id, scheduled_for))`)
> - Test: `tests/unit/test_fire_refresh_rule_predicate.py`, `tests/integration/test_two_schedulers_one_occurrence.py`, `tests/unit/test_redispatch_interleave.py`
>
> - [ ] **Step 1: Failing tests**:
>
> ```python
> def test_fire_rechecks_due_time_at_claim(db_session, rule_due_then_advanced):
>     # candidate was loaded while due; another process advanced next_run_at before we lock
>     assert fire_refresh_rule(db_session, rule_id=rule_due_then_advanced.id, now=NOW) is False
>
> def test_occurrence_identity_is_unique(db_session, due_rule):
>     assert fire_refresh_rule(db_session, rule_id=due_rule.id, now=NOW) is True
>     due_rule.next_run_at = OCC; db_session.commit()      # stale candidate from a second scheduler
>     assert fire_refresh_rule(db_session, rule_id=due_rule.id, now=NOW) is False
> ```
>
> Integration: two threads, two sessions, real Postgres, same due rule, both call `fire_refresh_rule` concurrently 50 times → exactly one `scrape_jobs` row per occurrence.
>
> - [ ] **Step 2: Implement** — `fire_refresh_rule` adds `.where(RefreshRule.next_run_at <= now)` under `FOR UPDATE SKIP LOCKED`; `scheduled_for = rule.next_run_at` (second precision) is inserted into `refresh_rule_occurrences` **before** `create_scope_job`; `IntegrityError` → rollback, return `False`. Per-rule isolation: any exception firing one rule is recorded via the existing `RetryLedger` (`fair_queue.py:553`), `consecutive_failures += 1`, `next_run_at = now + min(2^n × 60 s, 6 h)`, and the pass continues; the legacy loop (`refresh.py`) gets the same `try/except` per iteration instead of `break`. Default `SCHEDULER_FAIR_QUEUE_ENABLED = True` (closes the 2026-09-04 deferred item).
> - [ ] **Step 3: Work-level fairness** — `load_due_candidates` pages with `DISTINCT ON (workspace_id)` first, then the remainder, so one workspace cannot fill the candidate window; `redispatch_pending_jobs` orders pending jobs round-robin by workspace and dispatches at most `SCRAPE_DISPATCH_PER_WORKSPACE_BATCHES_PER_TICK = 4` batches per workspace per tick (new setting).
> - [ ] **Step 4:** tests green; commit `feat(scheduler): atomic due-time claim, unique occurrences, per-rule backoff, fair mode default, workspace interleave (F07)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/B3.md

## Scope
Files/areas in scope:
- **B3** — Modify `apps/scheduler/app/scheduler/scheduler_app.py` (~`:840-965`), `apps/scheduler/app/scheduler/refresh.py` (~`:69-140`), `libs/shared/app_shared/config.py` (`:768`), `apps/workers/app/workers/tasks_jobs.py` (~`:1040-1130`). Create `alembic/versions/<rev>_refresh_rule_occurrences.py`, `tests/unit/test_fire_refresh_rule_predicate.py`, `tests/integration/test_two_schedulers_one_occurrence.py`, `tests/unit/test_redispatch_interleave.py`.

Conventions:
- Repo root `/srv/crawmatic/crawmatic`, branch `epa/plan-core-production-readiness-2026-09-07`. Run every engine command as `mahmoud`: `sudo -u mahmoud /srv/crawmatic/crawmatic/.venv/bin/pytest ...`.
- The interpreter in `.venv` is **Python 3.13.14** even though the plan text says 3.12. Do not "fix" that.
- Definition of done per the plan: failing test first, implementation, tests green.
- **Do NOT run `git commit`.** The plan's "Commit `...`" steps are for the orchestrator, which commits per phase after review. Leave your changes uncommitted in the main tree (worker contract rule 8) and put the intended commit message in your report. Never `git add -A`.
- **Core only.** Nothing under `/srv/crawmatic/saas` may be modified.
- One Alembic head at all times; every new revision chains from the current head. Partitioned tables never get a bulk `DELETE`; drops are whole partitions.
- Money is micro-USD (1 USD = 1,000,000). The proxy billing unit is a named setting, never an implicit constant.
- Every read/write is workspace-scoped unless it is a registered system sweep (`_system_session()`); new fleet tables without a `workspace_id` are never exposed to tenant roles.
- Secrets: variable NAMES and file locations only, never values. Never run `env`/`printenv`/`set`.

Context (from CONTEXT.md ## Areas):
- **B3** — `config.py:768` was directly confirmed to hold `SCHEDULER_FAIR_QUEUE_ENABLED: bool = False` with the master-switch comment block the plan describes. `refresh.py` is 150 lines; `scheduler_app.py` 1406 (`fire_refresh_rule` at ~`:915`). B2 (earlier wave) already moved `create_scope_job` onto the outbox - build on that.

Task-specific notes:
- **B3** — Flipping `SCHEDULER_FAIR_QUEUE_ENABLED` to `True` changes scheduling behaviour fleet-wide. It is an explicit plan decision, not a deviation - state it prominently in your report's Notes for reviewer.
- **B3** — Two-scheduler integration test needs the compose Postgres: serialized; skip if a parallel sibling is running and note it in the report.

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`
2. **Single Alembic head** (this packet adds a revision): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud bash scripts/check_single_head.sh`
3. Integration/docker checks: `docker compose up -d postgres redis` then `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/integration -q -m integration` (scoped to this packet's test files). **Serialized; skip if a parallel sibling is running, and note the skip in the report.** ENOSPC or an unavailable stack → BLOCKER + deferred, never a task failure.

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

