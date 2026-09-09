# Final Verification — plan-core-production-readiness-2026-09-07

Branch: `epa/plan-core-production-readiness-2026-09-07` @ `83e177479f75261f56a9529af2af4d77f9d9ef0b`
Tree: clean (`git status --porcelain` shows only `.epa/.../STATE.md`, ignoring `__pycache__`)
No files modified outside `.epa/`. No deploys, no spend, no pushes.

## 1. Unit suite — `tests/unit -m "not integration"`, 7 chunks + 7 package subdirs

Command shape: `sudo -u mahmoud .venv/bin/pytest <files> -q -p no:cacheprovider -m "not integration"`, foreground, one call per chunk (no `run_in_background`/`Monitor` requested; the harness auto-backgrounded two calls that exceeded its 120s default sub-timeout — chunks 3 and 4 — and returned control via a task-completion notification carrying the same exit code, output preserved in full).

| Chunk | Files (sed range) | Result | Exit |
|---|---|---|---|
| 1 | test_*.py 1–49 | 763 passed | 0 |
| 2 | test_*.py 50–98 | 592 passed, 1 skipped | 0 |
| 3 | test_*.py 99–147 | 615 passed | 0 |
| 4 | test_*.py 148–196 | 638 passed, 1 deselected | 0 |
| 5 | test_*.py 197–245 | 666 passed | 0 |
| 6 | test_*.py 246–294 | 883 passed | 0 |
| 7 | test_*.py 295–339 | 537 passed, 1 deselected | 0 |
| subdirs | domains,jobs,models,netledger,observations,scrapyd,strategy | 277 passed, 18 skipped, 4 deselected | 0 |

**Aggregate: 4,971 passed, 19 skipped, 6 deselected, 0 failed.**

This is an exact match to the aggregate cited in `docs/PRODUCTION_READINESS_SCORE_2026-09.md`
("The unit gate this section's own verification ran against ... is `4,971 passed, 19 skipped,
6 deselected, 0 failed` — identical to Task D1's own aggregate") — confirms no regression from
any uncommitted work landing on top of it since that doc was last written.

No failures. Full tail output for every chunk retained at
`/tmp/claude-0/-srv-crawmatic/f8ee62d4-9d5b-4e3d-b568-ac492b4f62e8/scratchpad/finalverify/chunk{1..7}.log`,
`chunk_subdirs.log` (scratchpad, not part of the repo).

## 2. Single alembic head

```
$ sudo -u mahmoud bash scripts/check_single_head.sh
check_single_head: OK — exactly 1 head.
f6b28c714a93 (head)
```
Exit: 0. Matches the expected head `f6b28c714a93`.

## 3. Throwaway Postgres 18 — migrate-from-empty + roles + grants + RLS

Disk was 99% full (1.3–1.4 GB free); `postgres:18-alpine` was already cached locally (no pull).
Container run with `--tmpfs /var/lib/postgresql` (postgres 18's image layout moved the data
directory up one level from `/var/lib/postgresql/data`; the first attempt with the old mount
point failed immediately with the image's own directory-layout error, corrected on retry with no
disk writes to the host caused by either attempt), bound to loopback on a random high port
(23728), named `epa-finalverify-pg`.

- `alembic upgrade head` (as `postgres` superuser, `MIGRATION_DATABASE_URL` pointed at the empty
  throwaway DB) — ran every migration from empty to `f6b28c714a93`. **Exit 0.**
- `scripts/provision_db_roles.py --provision --adopt-ownership` (executes
  `scripts/provision_db_roles.sql`) — `RESULT: PASSED — 0 FAIL, 5 WARN`. The 5 WARNs are the
  documented, pre-annotated fleet-owned-table isolation gaps already on record in the SQL file's
  own comments (`network_operations`, `network_operation_resource_summaries`,
  `network_operation_settlements`, `network_operations_pre_partition`, `provider_usage_records`) —
  not new findings. **Exit 0.**
- `scripts/verify_grants.py` — 3 roles checked (`crawmatic_app`, `crawmatic_auth`,
  `crawmatic_scraper`), 63 manifest tables each, 0 FAIL. WARNs only for monthly partition tables
  (`*_2026_09`, `*_2026_10`) not individually listed in the static manifest — the known,
  documented partition-naming gap, not a drift finding. **Exit 0.**
- `scripts/rls_verify.py` — seeded two throwaway workspaces + one `products` row (workspace A) as
  superuser directly in the throwaway container so checks C/D had real data to probe (an empty DB
  would otherwise fail check C "own context sees own rows" vacuously, which the script's own
  docstring flags as meaningless — this is local scratch data, torn down with the container, not
  a deploy or a spend action). Ran as the ordinary `crawmatic_app` role:
  ```
  A. role attributes: PASS (non-superuser, no BYPASSRLS, owns no tables)
  B. ENABLE+FORCE RLS: PASS (45 workspace-scoped tables, >=1 policy each)
  C/D. own context sees own rows: PASS (products=1)
       foreign context sees 0 of workspace A's rows: PASS
       no context sees 0 rows (fail closed): PASS
  RESULT: PASSED — the ordinary connection is confined by RLS.
  ```
  **Exit 0.**

Teardown: `docker rm -f epa-finalverify-pg` → exit 0. Confirmed removed: `docker ps -a --filter
name=epa-finalverify-pg` returns no rows; `docker volume ls` shows no leftover volume (tmpfs data
dir, nothing persisted to a named volume).

## 4. Workspace scoping + lockfile

```
$ sudo -u mahmoud .venv/bin/python scripts/check_workspace_scoping.py
check_workspace_scoping: OK — no unscoped workspace-owned model access.
```
Exit: 0.

```
$ sudo -u mahmoud uv lock --check
Resolved 108 packages in 2ms
```
Exit: 0.

## 5. Load / fault-injection dry run

```
$ sudo -u mahmoud .venv/bin/pytest tests/load/test_fault_injection_dry_run.py -q -m load
29 passed in 0.29s
```
Exit: 0.

## 6. Full integration suite — NOT RUN (disk), and doc's own deferred-gate rows

Per instructions, the full `tests/integration` suite was not run (disk at 99%). The 12-area gate
table in `docs/PRODUCTION_READINESS_SCORE_2026-09.md` (§ "Row check: 12 audit §13 areas, 0 PASS")
has **zero rows marked PASS**. Verbatim `Area` column values, all NOT YET MEASURED or PARTIAL:

- Daily freshness — NOT YET MEASURED
- Headroom — NOT YET MEASURED
- Tail latency — NOT YET MEASURED
- Durability — NOT YET MEASURED
- Tenant fairness — PARTIAL — not measured as a whole row
- Host protection — NOT YET MEASURED
- Security — PARTIAL — not measured as a whole row
- Quality — PARTIAL — not measured as a whole row
- Economics — NOT YET MEASURED
- Database — PARTIAL — not measured as a whole row
- Recovery — NOT YET MEASURED
- Operations — PARTIAL — not measured as a whole row

Every row's own "Blocking gate" column names an owner-gated staging/deploy/spend step outside
this run's authority (per ASSUMPTIONS.md and this run's no-deploy/no-spend constraint).

## Verdict

All commands that could run without a deployed environment, real spend, or a decision only the
owner can make returned exit 0 with zero failures, and the unit-suite aggregate exactly reproduces
the number already on record in the readiness doc. The whole-branch gate PASSES on that scope. The
document's own 12-row certification table remains correctly un-PASSable from this run's authority
— that is a pre-existing, honestly-labeled state, not a new finding.
