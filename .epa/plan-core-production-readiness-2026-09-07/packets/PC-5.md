# Packet PC-5 — Phase C: Stage C — Efficient and correct results/cost
Model: opus   Parallel-safe: yes (with PC-2, PC-3)   Depends on: PB-9
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: C-w2   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### C6 — Usage aggregation by provider dimension; reservation by known first rung (F17, F18)
Acceptance criteria:
- `apps/api/app/services/admin_usage.py`: proxied means `provider <> 'direct'` AND `proxy_provider_id IS NOT NULL` - transport is no longer the discriminator. Aggregation uses `COUNT(DISTINCT network_request_id)`, includes children via `parent_operation_id`, and counts one physical op shared by N logical attempts ONCE, allocating through `cost_allocations` rather than summing N times.
- `tasks_jobs.py` (~`:235-300`) `_batch_authorization_request` uses `batch.initial_transport` from the playbook's `cheap_path`: DIRECT -> `estimated_bytes=0`, `transport='DIRECT'`, still authorized for breaker/entitlement/concurrency; PROXY/BROWSER -> bytes. `estimated_requests` is the coalesced count of unique physical requests.
- `libs/shared/app_shared/costauth/service.py` gains `AuthorizationPurpose.PROXY_ESCALATION`; escalation reserves separately at escalation time; `costauth_denials_by_reason` is exported to opsmetrics.
- `tests/integration/test_admin_usage_fixtures.py` (DB-backed): direct browser, proxied browser, retries, child resources and one fetch shared by three matches - allocated cost <= physical cost, and direct NEVER appears as proxied. `tests/unit/test_reservation_first_rung.py` covers the reservation change.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task C6: Usage aggregation by provider dimension; reservation by known first rung (F17, F18) — *parallel-safe*
>
> **Files:**
> - Modify: `apps/api/app/services/admin_usage.py:85-100`, `:255-400` (proxied = `provider <> 'direct'` **and** `proxy_provider_id IS NOT NULL`; `COUNT(DISTINCT network_request_id)`; include children via `parent_operation_id`; one physical op shared by N logical attempts is counted once and allocated through `cost_allocations`, never summed N times)
> - Modify: `apps/workers/app/workers/tasks_jobs.py:235-300` (`_batch_authorization_request` uses `batch.initial_transport` from the playbook's `cheap_path`: DIRECT → `estimated_bytes=0`, `transport="DIRECT"`, still authorized for breaker/entitlement/concurrency; PROXY/BROWSER → bytes; `estimated_requests` = coalesced unique physical requests)
> - Modify: `libs/shared/app_shared/costauth/service.py` (`AuthorizationPurpose.PROXY_ESCALATION`; escalation reserves separately at escalation time; `costauth_denials_by_reason` exported to opsmetrics)
> - Test: `tests/integration/test_admin_usage_fixtures.py` (DB-backed fixtures: direct browser, proxied browser, retries, child resources, one fetch shared by three matches — assert allocated cost ≤ physical cost and direct never appears as proxied), `tests/unit/test_reservation_first_rung.py`
>
> - [ ] **Step 1:** failing tests as listed. **Step 2:** implement. **Step 3:** commit `fix(usage): provider-dimension aggregation over unique physical operations; reservation by first rung with separate escalation (F17, F18)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/C6.md

## Scope
Files/areas in scope:
- **C6** — Modify `apps/api/app/services/admin_usage.py` (~`:85-100`, `:255-400`), `apps/workers/app/workers/tasks_jobs.py` (~`:235-300`), `libs/shared/app_shared/costauth/service.py`. Create `tests/integration/test_admin_usage_fixtures.py`, `tests/unit/test_reservation_first_rung.py`.

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
- **C6** — `admin_usage.py` is 408 lines; `PROXIED_TRANSPORTS = (PROXY, BROWSER)` at `:96` was confirmed exactly as the plan's corrections table describes, and `tasks_jobs.py:235`'s own docstring confirms every HTTP batch is currently authorized as `PROXY`, fail-closed. `costauth/service.py` is 1887 lines and was also edited by B5 (Stage B, fleet denial reasons) - different line ranges; do not revert B5's work.

Task-specific notes:
- **C6** — No Alembic revision in this task - if you believe one is needed, that is a MATERIAL decision; report ESCALATE.
- **C6** — The integration test needs the compose Postgres: serialized; skip if a parallel sibling is running and note it in the report.

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`
2. Integration/docker checks: `docker compose up -d postgres redis` then `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/integration -q -m integration` (scoped to this packet's test files). **Serialized; skip if a parallel sibling is running, and note the skip in the report.** ENOSPC or an unavailable stack → BLOCKER + deferred, never a task failure.

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

