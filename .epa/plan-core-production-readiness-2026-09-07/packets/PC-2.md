# Packet PC-2 — Phase C: Stage C — Efficient and correct results/cost
Model: sonnet   Parallel-safe: yes (with PC-3, PC-5)   Depends on: PC-1
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: C-w2   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### C2 — Amazon HTTP-leg investigation with the resolved production profile — spend ≤ $1
Acceptance criteria:
- `scripts/probe_amazon_http_leg.py` accepts `--targets N --runs 3 --max-usd <required> --report out.json`, REFUSES to run without `--max-usd`, and uses `scrape_core.targets` profile resolution with the production header/session policy over direct and proxied legs.
- Per attempt the report records: status, product title present, price via THE RESOLVED PROFILE, the C1 classification, and wire bytes (A7's `wire_bytes`).
- `tests/unit/test_probe_amazon_report.py` verifies the report math on fixture attempts.
- `docs/ops/AMAZON_STRATEGY_2026-09.md` is created with the decision table stated and the result rows left BLANK for the owner's run: >= 80% HTTP price success -> HTTP-first stays with browser fallback capped at 1 per refresh; 30-80% -> HTTP-first only for the classified 'HTTP-works' subset with sampled probes; < 30% -> browser-first with a 5% HTTP recovery probe.
- Step 2 is a SPEND step (<= $1) and is NOT run. Write the exact command into the doc and mark it deferred in the report.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task C2: Amazon HTTP-leg investigation with the resolved production profile (deep dive §12 item 3, audit F08) — spend ≤ $1
>
> **Files:**
> - Create: `scripts/probe_amazon_http_leg.py` (`--targets N --runs 3 --max-usd 1.00 --report out.json`; uses `scrape_core.targets` profile resolution, the production header/session policy, direct and proxied legs, and reports per attempt: status, product title present, price via **the resolved profile**, C1 classification, wire bytes)
> - Create: `docs/ops/AMAZON_STRATEGY_2026-09.md` (decision record filled by the run)
> - Test: `tests/unit/test_probe_amazon_report.py` (report math on fixture attempts)
>
> - [ ] **Step 1:** failing report test; implement the script; unit green.
> - [ ] **Step 2 (EPA, spend):** run `--targets 50 --runs 3 --max-usd 1.00` from a staging scrapyd node if D1's environment exists, else from this host with the recorded caveat that Railway IP reputation is not reproduced (deep dive §2). Decision table: HTTP price success ≥ 80% across runs → HTTP-first stays, browser fallback capped at 1 per refresh; 30–80% → HTTP-first only for the classified "HTTP-works" subset (response signature) with sampled probes; < 30% → browser-first for Amazon with a 5% HTTP recovery probe. Record which case applied and the exact `domain_playbooks` values (C4).
> - [ ] **Step 3:** commit `feat(scripts): Amazon HTTP-leg probe with resolved profile; strategy decision record (F08)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/C2.md

### C3 — Document-only browser canary with the fixed calculator — spend ≤ $3
Acceptance criteria:
- `libs/shared/app_shared/profiles/browser_resource_policy.py` (~`:100-220`): `BROWSER_DOCUMENT_ONLY_DOMAINS` applies to BOTH direct and proxied legs; the 2026-09-03 `BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS` stays as an alias; `BLOCKLIST_VERSION` becomes 3.
- `tests/unit/test_browser_resource_policy_both_legs.py`: a direct browser request for a listed domain is document-only; an unlisted domain is unchanged; the policy version is stamped into the report header.
- `scripts/canary_document_only_browser.py` gains `--max-usd` (required), `--targets 50`, randomized paired ordering and a dedicated proxy sub-account referenced by the env NAME `PROXY_CANARY_USERNAME`, on top of A6's corrected calculator.
- Step 2 is a SPEND step (<= $3) and is NOT run. Record the exact command and the acceptance criteria (price agreement >= 98% on shared successes; price success within 3 points; bytes/page <= 400,000; p95 wall not worse by > 20%; CPU/page reported) in the report and mark it deferred. `amazon.sa` is added to `BROWSER_DOCUMENT_ONLY_DOMAINS` by the OWNER in C11 only, never here.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task C3: Document-only browser canary with the fixed calculator; policy for both legs (deep dive §12 item 2, audit §11 item 4) — spend ≤ $3
>
> **Files:**
> - Modify: `libs/shared/app_shared/profiles/browser_resource_policy.py:100-220` (`BROWSER_DOCUMENT_ONLY_DOMAINS` applies to **direct and proxied** legs; the 09-03 `BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS` stays as an alias; `BLOCKLIST_VERSION = 3`)
> - Modify: `scripts/canary_document_only_browser.py` (A6 calculator; `--max-usd`, `--targets 50`, randomized paired order, dedicated proxy sub-account via the env name `PROXY_CANARY_USERNAME`)
> - Test: `tests/unit/test_browser_resource_policy_both_legs.py`
>
> - [ ] **Step 1:** failing test: a direct browser request for a listed domain is document-only; an unlisted domain is unchanged; the policy version is stamped into the report header.
> - [ ] **Step 2 (EPA, spend):** run on Railway (staging if available, else production scrapers-browser with the refresh rule disabled and the canary's own job): baseline vs document-only, ≥ 50 Amazon targets each, same target set and profile. Acceptance (all required): price agreement ≥ 98% on shared successes; price success within 3 points; bytes/page ≤ 400,000; p95 wall not worse by > 20%; CPU/page reported. Only then is `amazon.sa` added to `BROWSER_DOCUMENT_ONLY_DOMAINS` **by the owner** in C11.
> - [ ] **Step 3:** commit `feat(browser): document-only policy for both legs; canary with corrected calculator (deep dive §12.2)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/C3.md

## Scope
Files/areas in scope:
- **C2** — Create `scripts/probe_amazon_http_leg.py`, `docs/ops/AMAZON_STRATEGY_2026-09.md`, `tests/unit/test_probe_amazon_report.py`. Modify nothing else.
- **C3** — Modify `libs/shared/app_shared/profiles/browser_resource_policy.py` (~`:100-220`), `scripts/canary_document_only_browser.py`. Create `tests/unit/test_browser_resource_policy_both_legs.py`.

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
- **C2** — Total real-domain canary spend across the plan is capped at $10 with sub-caps per task; ASSUMPTIONS.md answer 3 defers ALL spend steps to the owner - total run spend is $0. C1 (same wave) defines the classification this report cites; import it rather than duplicating the logic.
- **C3** — `browser_resource_policy.py` is 378 lines; the cited range fits. A6 (Stage A) already fixed the calculator inside `canary_document_only_browser.py` - extend the CLI, do not rewrite the calculator.

Task-specific notes:
- **C2** — Destructive firewall: no production scrape, no proxy traffic, no spending. Ever.
- **C3** — Do NOT add `amazon.sa` (or any domain) to `BROWSER_DOCUMENT_ONLY_DOMAINS` - that is the owner's C11 decision, gated on canary results that do not exist yet.
- **C3** — Destructive firewall: no canary run, no proxy traffic, no spending.

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. Focused (C2): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit/test_probe_amazon_report.py -q`
2. Focused (C3): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit/test_browser_resource_policy_both_legs.py -q`
3. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

