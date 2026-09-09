# Packet PA-6 — Phase A: Stage A — Contain risk, trustworthy baseline
Model: sonnet   Parallel-safe: yes (with PA-2)   Depends on: P0-2
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: A-w2   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### A7 — Ledger coverage and bytes at the transport boundary
Acceptance criteria:
- A new Scrapy downloader middleware at `libs/scrape-core/scrape_core/middlewares/wire_bytes.py` (create the `middlewares/` package with `__init__.py`) with priority `585` stores `len(response.body) + len(response.headers.to_string()) + len(status_line)` into `response.meta['wire_bytes']` while the body is still compressed (`HttpCompressionMiddleware` is 590 and `process_response` runs in descending order).
- `tests/unit/test_wire_bytes_middleware.py`: a gzip fixture of 1,000 wire bytes / 8,000 decoded yields `wire_bytes == 1000 + len(status line + headers)`.
- `netledger_middleware.py` writes `bytes_compressed` from `wire_bytes` for EVERY transport (PROXY and DIRECT) and `bytes_decompressed` from the final body; browser child `duration_ms` comes from Playwright/CDP timing (`{requestStart: 0, responseEnd: 240}` -> 240; no timing -> `None`).
- `opsmetrics/emit.py` gains `crawmatic_ledger_linked_attempt_fraction_24h` (fraction of 24 h `request_attempts` rows with a non-null `network_operation_id`) and `crawmatic_ledger_bytes_missing_fraction_24h` (fraction of proxied operations with null `bytes_compressed`). Alert thresholds < 95% / > 5% are documented for B9 to wire.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task A7: Ledger coverage and bytes at the transport boundary (deep dive §8.2) — *parallel-safe*
>
> **Files:**
> - Create: `libs/scrape-core/scrape_core/middlewares/wire_bytes.py` (Scrapy downloader middleware ordered before `HttpCompressionMiddleware` decompresses)
> - Modify: `libs/scrape-core/scrape_core/netledger_middleware.py:400-520` (`bytes_compressed` for PROXY and DIRECT; child `duration_ms` from CDP timing)
> - Modify: `libs/shared/app_shared/opsmetrics/emit.py` (`crawmatic_ledger_linked_attempt_fraction_24h`, `crawmatic_ledger_bytes_missing_fraction_24h`)
> - Test: `tests/unit/test_wire_bytes_middleware.py`, `tests/unit/test_netledger_child_duration.py`
>
> - [ ] **Step 1: Failing tests**: a gzip-compressed fixture response of 1,000 wire bytes / 8,000 decoded → `response.meta["wire_bytes"] == 1000 + len(status line + headers)`; a browser child operation with Playwright timing `{requestStart: 0, responseEnd: 240}` → `duration_ms == 240`; a child without timing → `None`.
> - [ ] **Step 2: Implement** — middleware priority `585` (`HttpCompressionMiddleware` is `590`; `process_response` runs in descending order so 585 sees the still-compressed body); it stores `len(response.body) + len(response.headers.to_string()) + len(status_line)` into `response.meta["wire_bytes"]`. `netledger_middleware` writes it to `bytes_compressed` for every transport and `bytes_decompressed` from the final body.
> - [ ] **Step 3: Coverage metrics** — fraction of `request_attempts` rows in 24 h with a non-null `network_operation_id`; fraction of proxied operations with null `bytes_compressed`. Alert thresholds < 95% / > 5% (B9 wires delivery).
> - [ ] **Step 4:** commit `feat(netledger): wire bytes for every transport, child durations, coverage metrics (deep dive §8.2)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/A7.md

## Scope
Files/areas in scope:
- **A7** — Create `libs/scrape-core/scrape_core/middlewares/__init__.py`, `libs/scrape-core/scrape_core/middlewares/wire_bytes.py`, `tests/unit/test_wire_bytes_middleware.py`, `tests/unit/test_netledger_child_duration.py`. Modify `libs/scrape-core/scrape_core/netledger_middleware.py` (~`:400-520`), `libs/shared/app_shared/opsmetrics/emit.py`, and the scrapers' Scrapy settings to register the middleware at the required priority.

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
- **A7** — CONTEXT.md unknown 2: there is NO `middlewares/` directory under `scrape_core/` today - create it (the plan already says 'Create' for this file). `netledger_middleware.py` sits at the package root. `opsmetrics/emit.py` is shared with A5, which runs in a LATER wave - keep your additions self-contained so A5 merges cleanly.

Task-specific notes:
- **A7** — Do not move or rename the existing `libs/scrape-core/scrape_core/limiter.py` (a reactor-safe seam over `app_shared.limiter`); B5 later works there. The new `middlewares/` package is for genuinely new downloader middlewares only.

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

