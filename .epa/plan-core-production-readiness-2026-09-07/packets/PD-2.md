# Packet PD-2 — Phase D: Stage D — Certify and expand
Model: sonnet   Parallel-safe: yes (with PD-3)   Depends on: PC-10
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: D-w1   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### D2 — Fault-injection matrix in staging
Acceptance criteria:
- `tests/load/fault_injection/` contains `kill_worker_after_post.py`, `kill_scraper_after_fetch.py`, `pause_postgres_60s.py`, `pause_redis_60s.py`, `two_schedulers.py`, `one_broken_tenant.py` (a workspace whose fixture store returns 100% blocks) and `host_limit_hold.py` (three nodes; the fleet lease must hold).
- Each script is self-contained, refuses to run without an explicit staging target, and prints the measurement the plan requires: duplicate physical fetches (0 beyond the same `scrapyd_job_id`), lost observations (0), targets released by the reaper after a scraper kill, other tenants' due work starting while the broken tenant fails, concurrent schedulers firing one occurrence, fleet lease count never exceeding the cap.
- `docs/ops/FAULT_INJECTION_2026-09.md` states the matrix, the pass bar per row, and leaves the result cells empty.
- Step 1's RUNS are DEFERRED - they require the staging environment (ASSUMPTIONS.md answer 2). Record the exact command per script and mark the runs deferred.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task D2: Fault-injection matrix in staging (audit §13 Durability, Tenant fairness, Host protection)
>
> **Files:**
> - Create: `tests/load/fault_injection/` scripts: `kill_worker_after_post.py`, `kill_scraper_after_fetch.py`, `pause_postgres_60s.py`, `pause_redis_60s.py`, `two_schedulers.py`, `one_broken_tenant.py` (a workspace whose fixture store returns 100% blocks), `host_limit_hold.py` (three nodes; assert the fleet lease holds)
> - Create: `docs/ops/FAULT_INJECTION_2026-09.md`
>
> - [ ] **Step 1 (EPA, in staging):** run each; record: duplicate physical fetches (0 beyond the same `scrapyd_job_id`), lost observations (0), targets released by the reaper after a scraper kill (the 09-03 G2 proof, valid now that A5 made STARTED real), other tenants' due work starting while the broken tenant fails, concurrent schedulers firing one occurrence, fleet lease count never exceeding the cap.
> - [ ] **Step 2:** commit the scripts and the doc `test(fleet): fault-injection matrix results`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/D2.md

### D3 — Bounded real-domain canary with provider and container reconciliation — spend ≤ $3
Acceptance criteria:
- `scripts/reconcile_canary_costs.py --job-id <id> --provider-window <w> --max-usd <required>` compares ledger bytes to DataImpulse metered bytes for the window and modeled browser CPU to the Railway per-service CPU delta, with tolerances 10% bytes / 25% CPU, and REFUSES to run without `--max-usd`.
- `docs/ops/COST_MODEL_2026-09.md` re-states the deep dive §10 table with the input rows labelled by their source (bytes/page after C3, HTTP success after C2, CPU/page from the container, matches/product from the real catalogue) and the measured cells left empty.
- Unit coverage exists for the reconciliation math on fixture inputs.
- Step 1 is a SPEND step (<= $3, production scrapers) and is NOT run. Record the exact command and the pass bar (ledger within tolerance; `cost_model_drift_ratio` between 0.9 and 1.1) and mark it deferred.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task D3: Bounded real-domain canary with provider and container reconciliation (audit §13 Economics, deep dive §13) — spend ≤ $3
>
> **Files:**
> - Create: `scripts/reconcile_canary_costs.py` (`--job-id --provider-window --max-usd`; compares ledger bytes to DataImpulse metered bytes for the window and modeled browser CPU to the Railway per-service CPU delta; tolerance 10% bytes, 25% CPU; refuses to run without `--max-usd`)
> - Create: `docs/ops/COST_MODEL_2026-09.md` (the deep dive §10 table re-computed from measured inputs: bytes/page after C3, HTTP success after C2, CPU/page from the container, matches/product from the real catalogue)
>
> - [ ] **Step 1 (EPA, spend, production scrapers, refresh rule stays disabled):** one mixed Amazon + Noon + S-Tech job of 150 targets under the strategies chosen in C2–C4; run the reconciliation; pass: ledger within tolerance; `cost_model_drift_ratio` between 0.9 and 1.1.
> - [ ] **Step 2:** commit `feat(cost): canary cost reconciliation; measured cost model (audit §13 Economics)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/D3.md

## Scope
Files/areas in scope:
- **D2** — Create `tests/load/fault_injection/` (the seven scripts plus a README or `__init__.py` as the repo convention requires) and `docs/ops/FAULT_INJECTION_2026-09.md`.
- **D3** — Create `scripts/reconcile_canary_costs.py`, `docs/ops/COST_MODEL_2026-09.md`, and a unit test for the reconciliation math under `tests/unit/`.

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
- **D2** — The scraper-kill case is the 2026-09-03 G2 deploy-survival proof, which only became meaningful once A5 made the `STARTED` transition real - say so in the doc. B2's stable `scrapyd_job_id`, B3's `refresh_rule_occurrences` and B5's fleet lease are the mechanisms under test.
- **D3** — `cost_model_drift_ratio` is the A5 gauge `crawmatic_cost_model_drift_ratio` (`ledger_bytes_24h / provider_bytes_24h`, `NULL` when the provider figure is absent). A8 fixed the watchdog and the billing unit; A7 made ledger bytes trustworthy at the transport boundary.

Task-specific notes:
- **D2** — Destructive firewall: these scripts kill and pause containers. They must NEVER target production, and you must NOT run them here. Ship them with a hard guard on a staging-only environment marker.
- **D2** — `tests/load/` already carries the `load` marker registered by B7 - mark these consistently.
- **D3** — Destructive firewall: no production scrape, no proxy traffic, no Railway API write, no spending. Never print a provider or Railway token.

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

