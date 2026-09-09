# Packet PB-2 — Phase B: Stage B — Durable work, reliable schedules
Model: opus   Parallel-safe: yes (with PB-7)   Depends on: PB-1
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: B-w2   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### B2 — Dispatch intent committed before the POST; stable remote job id; outbox (F06)
Acceptance criteria:
- Intent states are `PLANNED -> POSTED -> CONFIRMED | FAILED` plus `RECONCILED_MISSING`. One Alembic revision adds `node_url TEXT NOT NULL`, `scrapyd_job_id UUID NOT NULL` (deterministic `uuid5(identity)`), `state TEXT`, `posted_at`, `confirmed_at`. Single head preserved.
- `DispatchIntentStore.plan/record_post/confirm/fail/mark_missing` each commit in their own short transaction via an injected `session_factory`.
- The five-step protocol is implemented exactly: plan+commit (targets get `claimed_at`) -> record_post+commit -> `client.schedule(..., jobid=str(scrapyd_job_id), node_url=node_url)` with NO DB transaction open -> confirm + `stamp_targets_dispatched` + commit (`remote_accepted_at`) -> on any exception after record_post leave `POSTED` for `reconcile_inflight_intents`, which calls the new `client.list_jobs(node_url, project)` and moves the row to `CONFIRMED` or `RECONCILED_MISSING`; only `RECONCILED_MISSING` re-POSTs, with the SAME id.
- The plan's two unit tests pass: worker death after an accepted POST does not re-POST (`post_count == 1`, ends `CONFIRMED`, targets have `dispatched_at`); a node that forgot the job re-POSTs the same `jobid`.
- Cost authorization: the grant is reserved in the plan transaction and released in `fail`. The Redis dispatch guard stays as a fast path but is no longer load-bearing.
- Outbox: `create_scope_job` writes `outbox_messages(kind='job_created', payload={job_id})` inside the job's transaction; `outbox/dispatcher.py` handles the new kind by enqueuing `SCRAPE_DISPATCH_JOB`. Every direct `.delay()`-before-commit call site is removed; a rolled-back job creation produces no Celery message (tested).

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task B2: Dispatch intent committed before the POST; stable remote job id; outbox for job-created (F06)
>
> **Files:**
> - Modify: `apps/workers/app/workers/tasks_jobs.py:587-840` (dispatch loop transaction boundaries), `:1040-1200` (`redispatch_pending_jobs` uses the same path)
> - Modify: `libs/shared/app_shared/jobs/dispatch_intents.py:96-300` (states, `node_url`, `scrapyd_job_id` chosen at plan time)
> - Modify: `libs/shared/app_shared/scrapyd/client.py:239-420` (`schedule(..., jobid=)` passes Scrapyd's `jobid` parameter; new `list_jobs(node_url, project) -> set[str]`)
> - Modify: `create_scope_job` call sites (`apps/scheduler/app/scheduler/scheduler_app.py:940-962`, API job creation router) → write an outbox row instead of `.delay()` before commit
> - Modify: `libs/shared/app_shared/outbox/dispatcher.py` (new kind `job_created` → enqueue `SCRAPE_DISPATCH_JOB`)
> - Create: `alembic/versions/<rev>_dispatch_intent_node_and_state.py`
> - Test: `tests/unit/test_dispatch_intent_state_machine.py`, `tests/unit/test_dispatch_kill_after_post.py`, `tests/integration/test_dispatch_reconcile_fake_scrapyd.py`
>
> **Interfaces:**
> - Intent states: `PLANNED → POSTED → CONFIRMED | FAILED`, plus `RECONCILED_MISSING` (POSTED but the node has no such job → safe to re-POST **with the same** `scrapyd_job_id`). Columns added: `node_url TEXT NOT NULL`, `scrapyd_job_id UUID NOT NULL` (deterministic `uuid5(identity)`), `state TEXT`, `posted_at`, `confirmed_at`.
> - `DispatchIntentStore.plan(...)`, `record_post`, `confirm`, `fail`, `mark_missing` each commit in their own short transaction (the store takes a `session_factory`).
>
> - [ ] **Step 1: Failing tests** — the kill-after-POST case the audit demands:
>
> ```python
> def test_worker_death_after_accepted_post_does_not_repost(session_factory, fake_scrapyd):
>     with pytest.raises(ProcessDied):   # POST accepted, then the process "dies" before confirm
>         dispatch_job(job_id, session_factory=session_factory, client=fake_scrapyd.dying_after_post())
>     intent = load_intents(job_id)[0]
>     assert intent.state == "POSTED" and intent.node_url and intent.scrapyd_job_id
>     reconcile_inflight_intents(session_factory=session_factory, client=fake_scrapyd)
>     assert fake_scrapyd.post_count == 1
>     assert load_intents(job_id)[0].state == "CONFIRMED"
>     assert all(t.dispatched_at for t in load_targets(job_id))
>
> def test_missing_on_node_reposts_with_same_jobid(session_factory, fake_scrapyd):
>     fake_scrapyd.forget_all()
>     reconcile_inflight_intents(session_factory=session_factory, client=fake_scrapyd)
>     assert fake_scrapyd.posted_jobids == [fake_scrapyd.posted_jobids[0]] * 2   # same id; Scrapyd dedups
> ```
>
> - [ ] **Step 2: Run** → FAIL. **Step 3: Implement the five-step protocol** from F06: (1) `plan` + commit (targets get `claimed_at`); (2) `record_post` + commit (state `POSTED`, `node_url`, `scrapyd_job_id`); (3) `client.schedule(..., jobid=str(scrapyd_job_id), node_url=node_url)` with **no DB transaction open**; (4) `confirm` + `stamp_targets_dispatched` + commit (`remote_accepted_at`); (5) on any exception after (2): leave `POSTED`; the maintenance reconciler (`reconcile_inflight`) asks `list_jobs(node_url)` and moves to `CONFIRMED` or `RECONCILED_MISSING`; only `RECONCILED_MISSING` re-enters step 3 with the same id. Cost authorization: the grant is reserved in step (1)'s transaction and released in `fail`. The Redis guard stays as a fast path but is no longer load-bearing.
> - [ ] **Step 4: Outbox** — `create_scope_job` writes `outbox_messages(kind='job_created', payload={job_id})` inside the job's transaction; the outbox dispatcher (already scheduled by `_enqueue_outbox_drain`) enqueues `SCRAPE_DISPATCH_JOB`. Remove every direct `.delay()`-before-commit at the call sites. Test: a rolled-back job creation never produces a Celery message.
> - [ ] **Step 5:** migration; tests green; commit `feat(dispatch): commit-before-send intents with stable Scrapyd job ids; outbox for job-created (F06)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/B2.md

## Scope
Files/areas in scope:
- **B2** — Modify `apps/workers/app/workers/tasks_jobs.py` (~`:587-840`, `:1040-1200`), `libs/shared/app_shared/jobs/dispatch_intents.py` (~`:96-300`), `libs/shared/app_shared/scrapyd/client.py` (~`:239-420`), `apps/scheduler/app/scheduler/scheduler_app.py` (~`:940-962`) and the API job-creation router, `libs/shared/app_shared/outbox/dispatcher.py`. Create `alembic/versions/<rev>_dispatch_intent_node_and_state.py`, `tests/unit/test_dispatch_intent_state_machine.py`, `tests/unit/test_dispatch_kill_after_post.py`, `tests/integration/test_dispatch_reconcile_fake_scrapyd.py`.

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
- **B2** — All cited files exist at the cited sizes: `tasks_jobs.py` 1438 lines, `dispatch_intents.py` 319, `scrapyd/client.py` 692 (`.schedule()` near line 239, `daemon_status` near 585), `scheduler_app.py` 1406, `outbox/dispatcher.py` 195. The plan's corrections table confirms `fire_refresh_rule` currently relies on the Redis-only dispatch guard - that is exactly the gap this task closes, and B3 completes it.

Task-specific notes:
- **B2** — `tasks_jobs.py` and `scheduler_app.py` are also edited by B3 and B6 in later waves - keep your edits inside the cited ranges so their merges stay clean.
- **B2** — The fake-Scrapyd integration test must never POST to a real Scrapyd node. `SCRAPYD_*_URLS` point at `http://127.0.0.1:1` in unit tests after Task 0.4.

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

