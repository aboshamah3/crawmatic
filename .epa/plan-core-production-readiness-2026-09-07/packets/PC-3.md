# Packet PC-3 — Phase C: Stage C — Efficient and correct results/cost
Model: opus   Parallel-safe: yes (with PC-2, PC-5)   Depends on: PC-1
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: C-w2   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### C4 — Noon and S-Tech labeled canaries; versioned domain strategy; safe coalescing key — spend ≤ $2
Acceptance criteria:
- One Alembic revision adds `domain_playbooks.strategy_version INT`, `cheap_path TEXT`, `fallback_path TEXT`, `fallback_cap_per_refresh INT`, `recovery_probe_fraction NUMERIC`. Single head preserved.
- The escalation ladder reads the playbook and stamps `strategy_version` on every attempt.
- `libs/shared/app_shared/jobs/coalescing.py`: the equivalence key becomes `(workspace_id, canonical_url_hash, region, currency, proxy_country, transport, variant_selector_hash)`. Cross-workspace sharing stays OFF - `CrossWorkspaceCoalescingUnsupported` remains the contract.
- Plan tests pass: two matches with the same URL but different `proxy_country` are not coalesced; the same key yields one physical fetch and two logical results; a playbook with `fallback_cap_per_refresh=1` allows exactly one browser fallback per target per refresh.
- `tests/fixtures/labeled_offers/{noon,stech,amazon}.jsonl` each hold >= 30 labeled expected offers (price, currency, seller, variant, availability).
- `scripts/run_domain_canary.py --domain <d> --max-usd <required> --labels <jsonl>` exists and refuses to run without `--max-usd`.
- Step 2 is a SPEND step (<= $2) and is NOT run. Create `docs/ops/DOMAIN_STRATEGIES_2026-09.md` with the acceptance bar (>= 95% label agreement on price + currency + availability) and empty result rows, record the exact commands, and mark the gate deferred. `scripts/seed_domain_playbooks.sql` is updated with the SHAPE of the rows to seed, values left for the owner.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task C4: Noon and S-Tech labeled canaries; versioned domain strategy; safe coalescing key (audit F08, §11 items 2 and 5) — spend ≤ $2
>
> **Files:**
> - Modify: `libs/shared/app_shared/models/domain_playbooks.py` + migration `<rev>_domain_playbook_strategy_version.py` (`strategy_version INT`, `cheap_path TEXT`, `fallback_path TEXT`, `fallback_cap_per_refresh INT`, `recovery_probe_fraction NUMERIC`)
> - Modify: `libs/scrape-core/scrape_core/strategy/escalation.py` (reads the playbook; stamps `strategy_version` on every attempt)
> - Modify: `libs/shared/app_shared/jobs/coalescing.py` (equivalence key = `(workspace_id, canonical_url_hash, region, currency, proxy_country, transport, variant_selector_hash)`; cross-workspace sharing stays **off** — `CrossWorkspaceCoalescingUnsupported` remains the contract; the audit's 50% upper bound is a duplicate-heavy pilot artefact)
> - Create: `tests/fixtures/labeled_offers/{noon,stech,amazon}.jsonl` (≥ 30 labeled expected offers each: price, currency, seller, variant, availability)
> - Create: `scripts/run_domain_canary.py` (`--domain noon.com --max-usd 1.00 --labels tests/fixtures/labeled_offers/noon.jsonl`)
> - Test: `tests/unit/test_playbook_strategy.py`, `tests/unit/test_coalescing_key.py`
>
> - [ ] **Step 1:** failing tests: two matches with the same URL but different `proxy_country` are not coalesced; same key → one physical fetch, two logical results; a playbook with `fallback_cap_per_refresh=1` allows exactly one browser fallback per target per refresh.
> - [ ] **Step 2 (EPA, spend):** run the Noon and S-Tech canaries against the labeled fixtures (≤ $1 each); acceptance: ≥ 95% label agreement (price + currency + availability); write results to `docs/ops/DOMAIN_STRATEGIES_2026-09.md` with the playbook rows to seed (`scripts/seed_domain_playbooks.sql` updated).
> - [ ] **Step 3:** commit `feat(strategy): versioned domain playbooks; labeled canaries; coalescing equivalence key (F08)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/C4.md

## Scope
Files/areas in scope:
- **C4** — Modify `libs/shared/app_shared/models/domain_playbooks.py`, `libs/shared/app_shared/strategy/methods.py` / `resolution.py` (the escalation ladder), `libs/shared/app_shared/jobs/coalescing.py`, `scripts/seed_domain_playbooks.sql`. Create `alembic/versions/<rev>_domain_playbook_strategy_version.py`, `tests/fixtures/labeled_offers/{noon,stech,amazon}.jsonl`, `scripts/run_domain_canary.py`, `docs/ops/DOMAIN_STRATEGIES_2026-09.md`, `tests/unit/test_playbook_strategy.py`, `tests/unit/test_coalescing_key.py`.

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
- **C4** — CONTEXT.md unknown 3 again: the plan's `libs/scrape-core/scrape_core/strategy/escalation.py` does not exist; the ladder is `libs/shared/app_shared/strategy/methods.py` + `resolution.py`, which C1 already modified in an earlier wave - build on C1's budget hook, do not revert it. `domain_playbooks.py` is 281 lines, `jobs/coalescing.py` 198.

Task-specific notes:
- **C4** — The audit's 50% coalescing upper bound is a duplicate-heavy pilot artefact - do not use it to justify enabling cross-workspace sharing.
- **C4** — Destructive firewall: no canary run, no proxy traffic, no spending.

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

