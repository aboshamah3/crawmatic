# Packet PA-5 — Phase A: Stage A — Contain risk, trustworthy baseline
Model: sonnet   Parallel-safe: yes (with PA-3)   Depends on: PA-2
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: A-w3   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### A6 — Canary calculator counts pages, prices and provider dimension
Acceptance criteria:
- `summarize()` in `scripts/canary_document_only_browser.py` counts PARENT pages (`parent_operation_id IS NULL`) as the denominator; children join by `parent_operation_id` regardless of hostname. The plan's `test_calculator_counts_parent_pages_and_attaches_children` passes.
- The SQL adds `provider` and `proxy_provider_id`, and joins `request_attempts.success AND price IS NOT NULL` as `price_ok` via `network_request_id`.
- Missing bytes/durations stay `None` and are reported as `unknown_*` counts - never coerced to 0.
- The comparison refuses to run when either arm has < 50 parents or > 5% unknowns, or when the target set / `strategy_profile_id` differ between arms.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task A6: Canary calculator counts pages, prices and provider dimension (deep dive §8.1) — *parallel-safe*
>
> **Files:**
> - Modify: `scripts/canary_document_only_browser.py:438-470` and its pure functions
> - Test: `tests/unit/test_canary_document_only_calculator.py`
>
> - [ ] **Step 1: Failing test** — reuse the deep dive's reproduction: one parent page (10 s, 2.18 MB across 99 children) must yield `pages=1, price_success=0, p50=10 s, bytes_per_page≈2.18 MB`, not `100 pages, 0 latency, 21.8 KB`:
>
> ```python
> def test_calculator_counts_parent_pages_and_attaches_children():
>     rows = [op(parent=None, id="p1", duration_ms=10_000, bytes=200_000, price_ok=False, provider="dataimpulse")]
>     rows += [op(parent="p1", id=f"c{i}", duration_ms=None, bytes=20_000, price_ok=None, provider="dataimpulse") for i in range(99)]
>     s = summarize(rows)
>     assert s.pages == 1 and s.price_success == 0 and s.p50_ms == 10_000
>     assert s.bytes_per_page == pytest.approx(2_180_000) and s.unknown_durations == 0
> ```
>
> - [ ] **Step 2: Fix the SQL**: pages are `parent_operation_id IS NULL`; children join by `parent_operation_id` **regardless of hostname**; add `provider` and `proxy_provider_id`; join `request_attempts.success AND price IS NOT NULL` as `price_ok` via `network_request_id`; missing bytes/durations stay `None` and are reported as `unknown_*` counts; refuse comparisons where either arm has < 50 parents or > 5% unknowns; require identical target set and `strategy_profile_id`.
> - [ ] **Step 3:** commit `fix(canary): parent-page denominators, price outcomes, provider dimension, unknowns preserved (deep dive §8.1)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/A6.md

### A8 — Cost artifacts and watchdog corrected
Acceptance criteria:
- `scripts/railway_cost_watchdog.py` takes the project id from `RAILWAY_PROJECT_ID` and the service map from the API; the hard-coded map is deleted; the script refuses to run without `RAILWAY_PROJECT_ID` (unit-tested).
- The daily log includes `hourly_sum_vs_window_delta_pct` for CPU/RAM/egress and warns above 2%.
- `PROXY_BILLING_UNIT_BYTES: int = 1073741824` is added to `libs/shared/app_shared/config.py` and used by `libs/shared/app_shared/costauth/pricing.py`. Test: 1 GiB at $1/unit -> 1,000,000 micro-USD by default; with `PROXY_BILLING_UNIT_BYTES=1000000000` the same bytes price to 1,073,741 micro-USD.
- `/srv/crawmatic/COST_MEASURED_AND_PRICING_2026-09-03.md` gains an **Erratum 2026-09-07** section: $0.093 -> $0.000093, and a note that the 3.7x GraphQL under-report was not reproduced on 2026-09-06.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task A8: Cost artifacts and watchdog corrected (deep dive §8.3) — *parallel-safe*
>
> **Files:**
> - Modify: `scripts/railway_cost_watchdog.py:55-75` (project id from env `RAILWAY_PROJECT_ID`, service map from the API; delete the hard-coded map), add hourly-vs-window reconciliation
> - Modify: `libs/shared/app_shared/config.py` (`PROXY_BILLING_UNIT_BYTES: int = 1073741824`), `libs/shared/app_shared/costauth/pricing.py` (use it)
> - Modify: `/srv/crawmatic/COST_MEASURED_AND_PRICING_2026-09-03.md` (append **Erratum 2026-09-07**: $0.093 → $0.000093; the 3.7× GraphQL under-report was not reproduced on 2026-09-06)
> - Test: `tests/unit/test_cost_watchdog_config.py`, `tests/unit/test_pricing_unit_setting.py`
>
> - [ ] **Step 1: Failing tests**: the watchdog refuses to run without `RAILWAY_PROJECT_ID`; pricing 1 GiB at $1/unit → 1,000,000 micro-USD with the default unit, and with `PROXY_BILLING_UNIT_BYTES=1000000000` the same bytes price to 1,073,741 micro-USD (the unit is visibly a choice).
> - [ ] **Step 2: Implement**; the watchdog's daily log includes `hourly_sum_vs_window_delta_pct` for CPU/RAM/egress and warns above 2%.
> - [ ] **Step 3:** commit `fix(cost): watchdog targets the core project by env; billing unit is a named setting; erratum (deep dive §8.3)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/A8.md

## Scope
Files/areas in scope:
- **A6** — Modify `scripts/canary_document_only_browser.py` (~`:438-470` plus its pure functions). Create `tests/unit/test_canary_document_only_calculator.py`.
- **A8** — Modify `scripts/railway_cost_watchdog.py` (~`:55-75`), `libs/shared/app_shared/config.py`, `libs/shared/app_shared/costauth/pricing.py`, `/srv/crawmatic/COST_MEASURED_AND_PRICING_2026-09-03.md` (outside the repo). Create `tests/unit/test_cost_watchdog_config.py`, `tests/unit/test_pricing_unit_setting.py`.

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
- **A6** — `scripts/canary_document_only_browser.py` is 647 lines; the cited range fits. C3 modifies this same script later (different concern: `--max-usd`, target count, paired ordering) - leave the CLI surface easy to extend.
- **A8** — `railway_cost_watchdog.py` is 279 lines, `costauth/pricing.py` 225. Ledger amounts are micro-USD (1 USD = 1,000,000); the proxy billing unit is bytes per GiB unless the provider contract says GB, and must be a named setting.

Task-specific notes:
- **A6** — This task is calculator-only. Do NOT run the canary - that is C3, and it is a deferred spend step (ASSUMPTIONS.md answer 3).
- **A8** — ASSUMPTIONS.md: `/srv/crawmatic/COST_MEASURED_AND_PRICING_2026-09-03.md` is at the workspace root, which is NOT a git repo - edit it in place and say so in the report; it cannot be committed with the engine work.
- **A8** — Never call the Railway API for real and never print or store a Railway token. Unit tests use fixtures.

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. Focused (A6): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit/test_canary_document_only_calculator.py -q`
2. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

