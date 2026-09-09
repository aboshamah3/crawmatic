# Packet PA-3 — Phase A: Stage A — Contain risk, trustworthy baseline
Model: sonnet   Parallel-safe: yes (with PA-5)   Depends on: PA-1, PA-2
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: A-w3   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### A3 — Component-scoped credentials and grants (F03)
Acceptance criteria:
- `scripts/sql/grants_expected.yaml` maps component -> role -> table -> explicit privilege set; the unit test asserts every table in `scripts/rls_table_manifest.txt` appears exactly once per role with an explicit privilege list (never an implicit `ALL`).
- Manifest content per the plan: `crawmatic_app` has no `UPDATE` on fleet tables; `crawmatic_auth` (BYPASSRLS) has `SELECT` on identity tables only - no `UPDATE products`, no `SELECT network_operations`; new `crawmatic_scraper` gets `INSERT` on `request_attempts`/`price_observations`/`network_operations`, `UPDATE` on `scrape_job_targets` status+timestamp columns, `SELECT` on profiles/matches, nothing on budgets/entitlements/users.
- One Alembic revision creates the `crawmatic_scraper` role and the tenant-safe view `workspace_usage_v` over `network_operations` filtered by the RLS workspace key; tenant roles read the view, never the table. Single head preserved.
- `scripts/verify_grants.py` queries `information_schema.role_table_grants`, diffs against the manifest and exits 2 on drift.
- `docs/ops/SECRETS_BY_COMPONENT.md` lists which variable NAMES each Railway service may hold (scrapers keep only the scraper-role `DATABASE_URL`, `REDIS_URL`, `SCRAPYD_*`, proxy credentials, `NETLEDGER_BUFFER_PATH`). Values are never written.
- `tests/integration/test_grants_manifest.py` runs `provision_db_roles.sql`, then `verify_grants.py` and the existing `scripts/rls_verify.py` against the compose DB.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task A3: Component-scoped credentials and grants (F03, F01 blast radius) — *parallel-safe*
>
> **Files:**
> - Modify: `scripts/provision_db_roles.sql`, `scripts/provision_db_roles.py`
> - Create: `scripts/sql/grants_expected.yaml` (component → role → table → privileges)
> - Create: `scripts/verify_grants.py`
> - Create: `alembic/versions/<rev>_tenant_usage_view_and_scraper_role.py`
> - Create: `docs/ops/SECRETS_BY_COMPONENT.md`
> - Test: `tests/integration/test_grants_manifest.py`, `tests/unit/test_grants_manifest_schema.py`
>
> - [ ] **Step 1: Failing unit test**: the manifest parses and every table in `scripts/rls_table_manifest.txt` appears exactly once per role with an explicit privilege set (no implicit `ALL`).
> - [ ] **Step 2: Write the manifest** from the audit's evidence: `crawmatic_app` (API): no `UPDATE` on fleet tables; `crawmatic_auth` (BYPASSRLS): `SELECT` on identity tables only, **no** `UPDATE products`, **no** `SELECT network_operations`; `crawmatic_scraper` (new role, used by the scrapyd services): `INSERT` on `request_attempts`, `price_observations`, `network_operations`, `UPDATE` on `scrape_job_targets` status/timestamp columns, `SELECT` on profiles/matches, nothing on budgets/entitlements/users. The migration creates the role and a tenant-safe view `workspace_usage_v` over `network_operations` filtered by the RLS workspace key; tenant roles read the view, never the table. The existing partition RLS guard (`scripts/rls_verify.py`) stays and is run by the integration test.
> - [ ] **Step 3: `verify_grants.py`** queries `information_schema.role_table_grants` and diffs against the manifest; exit 2 on drift. Integration test runs it against the compose DB after `provision_db_roles.sql`.
> - [ ] **Step 4: Secrets split** — `SECRETS_BY_COMPONENT.md` names which variable **names** each Railway service may hold. Scraper services keep only the scraper-role `DATABASE_URL`, `REDIS_URL`, `SCRAPYD_*`, proxy credentials, `NETLEDGER_BUFFER_PATH`; they lose every owner/admin/service-token variable. Applied by the owner in A10.
> - [ ] **Step 5:** commit `feat(security): per-component DB roles, grants manifest + verifier, tenant usage view (F03)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/A3.md

### A4 — Advisory triage gate and pinned build tools (F04)
Acceptance criteria:
- `scripts/security/advisory_triage.yaml` replaces `scripts/security/known_critical_advisories.txt`; entries are `{id, decision: accept|fix, owner, expires: YYYY-MM-DD, reason}`.
- `scripts/security/pip_audit_gate.py` runs `pip-audit --format json` and fails when a finding is untriaged, and when a triaged entry is past `expires`; a valid entry passes. `tests/unit/test_pip_audit_gate.py` covers all three cases.
- `scripts/security/image_inventory.py` writes `packages.json` (pip freeze, `dpkg -l`, Chromium/Playwright versions) into the image; the release manifest (0.3) links it.
- Both `uvx --from scrapyd-client` call sites are pinned: `apps/scrapers/Dockerfile:35` and `apps/scrapers-browser/Dockerfile:50` become a pinned `scrapyd-client==<current>`, declared in the dev dependency group.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task A4: Advisory triage gate and pinned build tools (F04) — *parallel-safe*
>
> **Files:**
> - Modify: `scripts/security/pip_audit_gate.py`; replace `scripts/security/known_critical_advisories.txt` with `scripts/security/advisory_triage.yaml`
> - Modify: every `uvx --from scrapyd-client` call site (grep `uvx --from`) → pinned `scrapyd-client==<current>` in the dev dependency group
> - Create: `scripts/security/image_inventory.py`
> - Test: `tests/unit/test_pip_audit_gate.py`
>
> - [ ] **Step 1: Failing test**: an advisory absent from `advisory_triage.yaml` fails the gate; an entry `{id, decision: accept|fix, owner, expires: YYYY-MM-DD, reason}` past `expires` fails; a valid entry passes.
> - [ ] **Step 2: Implement**; the gate runs `pip-audit --format json` and requires every finding to be triaged. `image_inventory.py` writes `packages.json` (pip freeze, `dpkg -l`, Chromium/Playwright versions) into the image; the release manifest (0.3) links it.
> - [ ] **Step 3:** commit `feat(security): every advisory triaged with owner and expiry; pinned build tools; image inventory (F04)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/A4.md

## Scope
Files/areas in scope:
- **A3** — Modify `scripts/provision_db_roles.sql`, `scripts/provision_db_roles.py`. Create `scripts/sql/grants_expected.yaml`, `scripts/verify_grants.py`, `alembic/versions/<rev>_tenant_usage_view_and_scraper_role.py`, `docs/ops/SECRETS_BY_COMPONENT.md`, `tests/integration/test_grants_manifest.py`, `tests/unit/test_grants_manifest_schema.py`.
- **A4** — Modify `scripts/security/pip_audit_gate.py`, `apps/scrapers/Dockerfile`, `apps/scrapers-browser/Dockerfile`, the dev dependency group. Replace `scripts/security/known_critical_advisories.txt` with `scripts/security/advisory_triage.yaml`. Create `scripts/security/image_inventory.py`, `tests/unit/test_pip_audit_gate.py`.

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
- **A3** — `scripts/provision_db_roles.sql` (367 lines), `provision_db_roles.py` (961) and `rls_verify.py` (442) all exist. The existing partition-RLS guard stays and is exercised by the integration test.
- **A4** — `scripts/security/pip_audit_gate.py` is 176 lines. The two `uvx --from` sites were located by grep and are the only ones in the tree.

Task-specific notes:
- **A3** — Integration run needs the compose stack (`docker compose up -d postgres redis` from the repo root): serialized; skip if a parallel sibling is running and note it in the report. The unit suite is always required.
- **A3** — Compose Postgres is `postgres:17.5-bookworm`, not 18 - a role/grants/view migration is version-agnostic, but record the version you actually tested against.
- **A4** — `apps/scrapers-browser/Dockerfile` was already modified by A1 in an earlier wave - merge with A1's image test stage, never revert it.
- **A4** — If `pip-audit` cannot reach the network, that is a BLOCKER entry; the gate's unit tests over fixture JSON are the required evidence either way.

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

