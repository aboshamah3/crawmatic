# Packet PB-5 — Phase B: Stage B — Durable work, reliable schedules
Model: opus   Parallel-safe: yes (with PB-6)   Depends on: PB-3
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: B-w5   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### B5 — Fleet-wide host admission at the physical request boundary (F10)
Acceptance criteria:
- `libs/shared/app_shared/limiter/keys.py` gains `fleet_rate_key(domain, transport)` and `fleet_semaphore_key(domain, transport)`.
- `libs/shared/app_shared/limiter/fleet.py` provides `admit_fleet(redis, *, domain, transport, rate_per_minute, concurrency, lease_ttl) -> FleetLease | None` and `release_fleet(redis, lease)`, reusing the existing Lua `acquire_token`/`acquire_slot`/`release_slot` in `limiter/bucket.py`.
- Plan tests pass with fakeredis: with concurrency 2 across THREE different workspaces the third simultaneous acquire for `amazon.sa` returns `None`; a lease whose holder never releases frees after `lease_ttl`; the rate bucket refuses the 91st request in a minute regardless of workspace.
- HTTP path: the fleet lease is acquired at the physical request boundary alongside the tenant limits and released on BOTH success and error; a refused lease defers the request through the existing `_mark_target_deferred_rate_limited` path. Browser path: acquire before `page.goto`, release after the page closes; child resources ride the page's lease.
- Settings `FLEET_HOST_CONCURRENCY_DEFAULT: int = 6`, `FLEET_HOST_RATE_PER_MINUTE_DEFAULT: int = 90`, `FLEET_LEASE_TTL_SECONDS: int = 120`, with per-domain overrides read from `domain_rules`. Browser navigations and HTTP requests count one lease each; retries re-acquire.
- `ScrapeErrorCode.FLEET_LIMITED` is added and recorded on the attempt so C1's classification and D5's scorecard see admission pressure separately from host blocking.
- One Alembic revision creates the `domain_rules` table with `fleet_concurrency INT NULL` and `fleet_rate_per_minute INT NULL`. Single head preserved.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task B5: Fleet-wide host admission at the physical request boundary (F10)
>
> **Files:**
> - Modify: `libs/shared/app_shared/limiter/keys.py` (`fleet_rate_key(domain, transport)`, `fleet_semaphore_key(domain, transport)`)
> - Create: `libs/shared/app_shared/limiter/fleet.py` (`admit_fleet(redis, *, domain, transport, rate_per_minute, concurrency, lease_ttl) -> FleetLease | None`, `release_fleet(redis, lease)`; reuses the Lua `acquire_token`/`acquire_slot`/`release_slot` in `bucket.py`)
> - Modify: `libs/scrape-core/scrape_core/middlewares/limiter.py` (HTTP: acquire tenant limits **and** the fleet lease before the request; release on response/error)
> - Modify: `apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py` (browser: acquire before `page.goto`, release after the page closes; child resources ride the page's lease)
> - Modify: `libs/shared/app_shared/models/domain_rules.py` + migration `<rev>_domain_rules_fleet_limits.py` (`fleet_concurrency INT NULL`, `fleet_rate_per_minute INT NULL`)
> - Modify: `libs/shared/app_shared/costauth/service.py:1253` (`_check_concurrency` stays tenant-scoped; a fleet snapshot is read for denial *reasons* only — admission itself is the Redis lease)
> - Test: `tests/unit/test_fleet_admission.py` (fakeredis Lua), `tests/unit/test_limiter_middleware_fleet.py`
>
> **Interfaces:**
> - Settings: `FLEET_HOST_CONCURRENCY_DEFAULT: int = 6`, `FLEET_HOST_RATE_PER_MINUTE_DEFAULT: int = 90`, `FLEET_LEASE_TTL_SECONDS: int = 120` (crash recovery = lease expiry). Per-domain overrides from `domain_rules`. Browser navigations and HTTP requests count 1 lease each; retries re-acquire.
>
> - [ ] **Step 1: Failing tests**: with concurrency 2 across **three different workspaces**, the third simultaneous acquire for `amazon.sa` returns `None`; a lease whose holder never releases frees after `lease_ttl`; the rate bucket refuses the 91st request in a minute regardless of workspace; the HTTP middleware defers the request through the existing `_mark_target_deferred_rate_limited` path when the fleet lease is refused, and releases the lease on both success and error paths.
> - [ ] **Step 2: Implement**; the denial is recorded on the attempt as `FLEET_LIMITED` (new `ScrapeErrorCode`) so C1's classification and D5's scorecard see admission pressure separately from host blocking.
> - [ ] **Step 3:** commit `feat(limiter): atomic fleet host concurrency and rate leases at the physical request boundary (F10)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/B5.md

## Scope
Files/areas in scope:
- **B5** — Modify `libs/shared/app_shared/limiter/keys.py`, `libs/scrape-core/scrape_core/limiter.py` and its call site `libs/scrape-core/scrape_core/targets.py:1484` (`acquire_permission`), `apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py`, `libs/shared/app_shared/enums.py`, `libs/shared/app_shared/config.py`, `libs/shared/app_shared/costauth/service.py` (~`:1253`). Create `libs/shared/app_shared/limiter/fleet.py`, `libs/shared/app_shared/models/domain_rules.py`, `alembic/versions/<rev>_domain_rules_fleet_limits.py`, `tests/unit/test_fleet_admission.py`, `tests/unit/test_limiter_middleware_fleet.py`.

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
- **B5** — CONTEXT.md unknowns 1 and 2 apply and the plan's paths are stale:
  - `libs/scrape-core/scrape_core/middlewares/limiter.py` does NOT exist. The real limiter is the reactor-safe seam `libs/scrape-core/scrape_core/limiter.py` (`acquire_permission`/`release_slot` over `app_shared.limiter`), and its only production call site is `libs/scrape-core/scrape_core/targets.py:1484`. Put the fleet lease THERE - that is the physical request boundary the plan means. Do not create a `middlewares/limiter.py`.
  - `libs/shared/app_shared/models/domain_rules.py` does NOT exist and no `domain_rules` table exists. CREATE both. This is a fleet-wide (workspace-independent) host-limits table and is deliberately distinct from the tenant access-policy `DomainAccessRule` (`apps/api/app/routers/domain_access_rules.py`) and from strategy `domain_playbooks`. New fleet tables without a `workspace_id` are NEVER exposed to tenant roles (Global Constraints, tenant rules).
`limiter/keys.py` is 33 lines; `costauth/service.py:1253` contains `_check_concurrency` exactly as cited.

Task-specific notes:
- **B5** — `_check_concurrency` stays tenant-scoped: a fleet snapshot is read for denial REASONS only - admission itself is the Redis lease. Do not move admission into costauth.
- **B5** — C1 later adds `request_timeout_seconds` to this same `domain_rules` table. Design the model so a column can be added without a table rewrite.
- **B5** — If you conclude the fleet limits belong on an existing table instead of a new one, that is a MATERIAL decision (data model) - report ESCALATE rather than choosing.

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

