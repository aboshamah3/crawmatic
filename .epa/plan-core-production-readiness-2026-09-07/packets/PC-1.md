# Packet PC-1 — Phase C: Stage C — Efficient and correct results/cost
Model: opus   Parallel-safe: yes (with PC-9)   Depends on: PB-9
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: C-w1   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### C1 — Per-target deadline, physical attempt budget, failure classes, method suppression (F08)
Acceptance criteria:
- `ScrapeErrorCode` gains `CONNECT_TIMEOUT`, `TTFB_TIMEOUT`, `READ_TIMEOUT`, `EXTRACTION_FAILED` (a 200 with no product identity - distinct from `PRICE_NOT_FOUND` on a genuine product page), `ATTEMPT_BUDGET_EXHAUSTED`, `TARGET_DEADLINE_EXCEEDED`.
- `libs/scrape-core/scrape_core/attempt_budget.py` provides `AttemptBudget(redis, *, job_id, match_id, max_physical, deadline_at)` with `try_consume(method) -> Verdict`, `suppress(method)`, `is_suppressed(method)`, keyed `budget:{job_id}:{match_id}` with the job's TTL.
- Plan tests pass: a method that failed with `EXTRACTION_FAILED` on this target in this refresh is not retried with the same method; the 5th physical attempt is refused with `ATTEMPT_BUDGET_EXHAUSTED`; a target past its deadline is finalized `FAILED/TARGET_DEADLINE_EXCEEDED` without a fetch; a `PRICE_NOT_FOUND` on a page whose title matched the product stays `PRICE_NOT_FOUND` while a 200 with no product title becomes `EXTRACTION_FAILED`; 5% of targets whose cheap method is suppressed by a DOMAIN-level rule still get a sampled recovery probe.
- The escalation ladder consults the budget before EVERY physical attempt.
- `libs/shared/app_shared/maintenance/domain_timeouts.py` adds task `MAINTENANCE_DOMAIN_TIMEOUT_TUNE`: per domain `timeout = clamp(1.5 x p95(successful attempt duration, 7 d), 10 s, 60 s)` written to `domain_rules.request_timeout_seconds`.
- New settings `SCRAPE_TARGET_DEADLINE_SECONDS: int = 900`, `SCRAPE_TARGET_MAX_PHYSICAL_ATTEMPTS: int = 4`, `SCRAPE_RECOVERY_PROBE_FRACTION: float = 0.05`. A new gauge `crawmatic_attempt_budget_exhausted_24h` joins A5's `crawmatic_attempts_per_valid_fresh_24h` as this task's KPI.
- One Alembic revision adds `domain_rules.request_timeout_seconds` (the table B5 created). Single head preserved.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task C1: Per-target deadline, physical attempt budget, failure classes, method suppression (F08, deep dive §5)
>
> **Files:**
> - Modify: `libs/shared/app_shared/enums.py` (`ScrapeErrorCode` adds `CONNECT_TIMEOUT`, `TTFB_TIMEOUT`, `READ_TIMEOUT`, `EXTRACTION_FAILED` (a 200 with no product identity — distinct from `PRICE_NOT_FOUND` on a genuine product page), `ATTEMPT_BUDGET_EXHAUSTED`, `TARGET_DEADLINE_EXCEEDED`)
> - Create: `libs/scrape-core/scrape_core/attempt_budget.py` (`AttemptBudget(redis, *, job_id, match_id, max_physical: int, deadline_at: datetime)` with `try_consume(method) -> Verdict`, `suppress(method)`, `is_suppressed(method)`)
> - Modify: `libs/scrape-core/scrape_core/targets.py` and the escalation ladder (`libs/scrape-core/scrape_core/strategy/escalation.py`) to consult the budget before every physical attempt
> - Create: `libs/shared/app_shared/maintenance/domain_timeouts.py` (task `MAINTENANCE_DOMAIN_TIMEOUT_TUNE`: per domain, `timeout = clamp(1.5 × p95(successful attempt duration, 7 d), 10 s, 60 s)` written to `domain_rules.request_timeout_seconds`; bounds the 46.9 s average proxied-HTTP timeout the deep dive found)
> - Modify: `libs/shared/app_shared/config.py` (`SCRAPE_TARGET_DEADLINE_SECONDS: int = 900`, `SCRAPE_TARGET_MAX_PHYSICAL_ATTEMPTS: int = 4`, `SCRAPE_RECOVERY_PROBE_FRACTION: float = 0.05`)
> - Test: `tests/unit/test_attempt_budget.py`, `tests/unit/test_error_classification.py`, `tests/unit/test_domain_timeout_tune.py`
>
> - [ ] **Step 1: Failing tests**: a method that failed with `EXTRACTION_FAILED` on this target in this refresh is not retried with the same method (`is_suppressed`); the 5th physical attempt is refused with `ATTEMPT_BUDGET_EXHAUSTED`; a target past its deadline is finalized `FAILED/TARGET_DEADLINE_EXCEEDED` without a fetch; a `PRICE_NOT_FOUND` on a page whose title matched the product stays `PRICE_NOT_FOUND`, while a 200 with no product title becomes `EXTRACTION_FAILED` (deep dive §6.1: "200 and fast is not success"); 5% of targets whose cheap method is suppressed by a *domain-level* rule still get a sampled recovery probe.
> - [ ] **Step 2: Implement**; the budget lives in Redis keyed `budget:{job_id}:{match_id}` with the job's TTL; `crawmatic_attempts_per_valid_fresh_24h` (A5) and a new `crawmatic_attempt_budget_exhausted_24h` are the KPI for this task, not raw success rows.
> - [ ] **Step 3:** commit `feat(scrape): per-target deadline and physical attempt budget; failure classes; method suppression; domain timeouts from success latency (F08)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/C1.md

## Scope
Files/areas in scope:
- **C1** — Modify `libs/shared/app_shared/enums.py`, `libs/shared/app_shared/config.py`, `libs/scrape-core/scrape_core/targets.py`, `libs/shared/app_shared/strategy/methods.py` and `libs/shared/app_shared/strategy/resolution.py` (the escalation ladder). Create `libs/scrape-core/scrape_core/attempt_budget.py`, `libs/shared/app_shared/maintenance/domain_timeouts.py`, `alembic/versions/<rev>_domain_rules_request_timeout.py`, `tests/unit/test_attempt_budget.py`, `tests/unit/test_error_classification.py`, `tests/unit/test_domain_timeout_tune.py`.

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
- **C1** — CONTEXT.md unknown 3: `libs/scrape-core/scrape_core/strategy/escalation.py` DOES NOT EXIST and there is no `strategy/` directory under `scrape_core/`. The real escalation-ladder logic lives in `libs/shared/app_shared/strategy/methods.py` (`resolve_method_candidate`) and `libs/shared/app_shared/strategy/resolution.py` (`resolve_strategy_start`). Hook the budget check THERE - follow the plan's intent, not its stale path. `domain_rules` was created by B5 in Stage B.

Task-specific notes:
- **C1** — C4 also edits `strategy/methods.py`/`resolution.py` (playbook `strategy_version` stamping) in a later wave - keep your changes narrow so C4 merges cleanly.
- **C1** — Register the new Celery task's time limit consistent with B4's annotations (maintenance tasks 300 s unless the plan says otherwise).

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`
2. **Single Alembic head** (this packet adds a revision): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud bash scripts/check_single_head.sh`

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

