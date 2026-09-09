# Phase D Review: Stage D — Certify and expand
Verdict: PASS
Base SHA: 9e932fa   Reviewed: main tree (uncommitted), branch `epa/plan-core-production-readiness-2026-09-07`

## Integration command

Primary — the unit gate over the whole tree with phase D's uncommitted work in place, chunked in 4
foreground runs (339 top-level files + 7 subpackages), each
`sudo -u mahmoud .venv/bin/pytest <chunk> -q -p no:cacheprovider -m "not integration"`:

| Chunk | Exit | Result |
|---|---|---|
| `$(ls tests/unit/*.py \| sed -n '1,114p')` | 0 | `1504 passed, 1 skipped in 162.10s` |
| `... '115,171p'` | 0 | `612 passed in 240.35s` |
| `... '172,285p'` | 0 | `1785 passed, 1 deselected in 263.00s` |
| `... '286,339p'` + `tests/unit/{domains,jobs,models,netledger,observations,scrapyd,strategy}` | 0 | `1070 passed, 18 skipped, 5 deselected in 102.72s` |

**Aggregate: 4,971 passed, 19 skipped, 6 deselected, 0 failed** — byte-identical to the aggregate
reports/D1.md and reports/D4.md each recorded independently. The one standing failure D2 reported
(`test_migration_offline_network_operations_partition.py::test_single_head`) is gone; D5's fix to
that assertion is in the tree and is correct (it now asserts "exactly one head, and the swap
revision is still in `alembic history`" instead of pinning the head string).

Secondary — the review focus's first item, run by me, not taken from a report. Throwaway
`postgres:18-alpine` (the production major per the newest dump manifests) on a random loopback port
with a tmpfs data dir, torn down after; `/` unchanged at 99% / 1.4 GB free, `docker ps -a` back to
the 5 pre-existing containers.

`sudo -u mahmoud .venv/bin/alembic -x db_url=postgresql+psycopg://…@127.0.0.1:<rnd>/revdb upgrade head` → exit 0

```
INFO  [alembic.runtime.migration] Running upgrade e2a4f80c6b71 -> b3f0c95a7d21, rollup completion checkpoint (EPA C7)
INFO  [alembic.runtime.migration] Running upgrade b3f0c95a7d21 -> d1f7a3c9e284, network operation resource summaries (EPA C9)
INFO  [alembic.runtime.migration] Running upgrade d1f7a3c9e284 -> a5e0c74b13d9, partition network_operations by month (EPA C9)
INFO  [alembic.runtime.migration] Running upgrade a5e0c74b13d9 -> f6b28c714a93, fleet daily cost/freshness scorecard (EPA D5)
```

Then, against that same fresh database:

* `scripts/provision_db_roles.sql` (ON_ERROR_STOP=1) → exit 0, no error, no
  `fleet_daily_scorecard` "names table … but it does not exist" warning
* `scripts/verify_grants.py --dsn-stdin` → **exit 0**, `63 table(s) in manifest` per role, **0 MISSING**
  (only the pre-existing partition-child `[WARN] … not in the manifest at all` lines, unchanged
  from phase C)
* `\d fleet_daily_scorecard` → all 15 plan-named metric columns present, every one nullable with no
  default; `date` is the PK, no surrogate `id`; `relrowsecurity = f` (SYSTEM classification honoured)
* `sudo -u mahmoud bash scripts/check_single_head.sh` → exit 0, `f6b28c714a93 (head)`
* `sudo -u mahmoud .venv/bin/python scripts/check_workspace_scoping.py` → exit 0
* `sudo -u mahmoud .venv/bin/pytest tests/load/test_fault_injection_dry_run.py -q -m load` → exit 0, `29 passed`

Not run (correctly, per Pre-Flight): the full integration suite, any staging run, any spend, any
deploy, any Railway write.

## Per-task

| Task | Criteria met | Evidence quality | Notes |
|---|---|---|---|
| D1 (fixture origin, seeder, fleet report) | yes | strong — verified independently | `apps/fixture-origin/app/main.py` imports no HTTP/DB/broker client (checked; only fastapi/stdlib). Seeder's four-part guard verified in source: `--i-know-this-is-staging` required, `--database-url` never read from env/settings, `RAILWAY_ENVIRONMENT_NAME` in {production,prod} refuses, host must be loopback/container-local or contain `staging`/`fixture`/`fleet-test`. Origin URLs go through the production `validate_competitor_url`; the `--allow-loopback-origin` escape hatch is checked against the **parsed IP** (`ipaddress.is_loopback`) and explicitly refuses the bare name `localhost`, so 10/8, 192.168/16 and 169.254.169.254 stay rejected. `fleet_test_report.py` bars match the plan verbatim (0.99 terminal, 7 cycles, 2× load, 1800 s rollup, 100 ms pool wait); `exit_code_for` returns 1 on any FAIL and **3 on any NO DATA**, and every evaluator returns `_no_data(...)` naming the missing inputs rather than coercing an absent number. Steps 2–4 deferred as instructed. |
| D1 — `tests/benchmarks/test_rollup_500k.py` fixes | yes | sound | Both fixes are real defects, minimally repaired: (a) `variant_ids` collected `variant.id` before the INSERT that evaluates `default=new_uuid7`, so the list was N `None`s — now an explicit `id=new_uuid7()`, keeping the single-flush shape; (b) the observation seed named `created_at`/`updated_at`, which `price_observations` genuinely does not have. The third change (`ANALYZE` before measuring) is justified and, more importantly, is the phase's most valuable finding — a bulk-loaded partition plans a nested loop that turned a 0.19 s batch into >18 min. |
| D2 (fault-injection matrix) | yes | good, with one gap (follow-up 1) | `_common.require_staging` is the single gate every script imports: `--staging-target` must equal the literal `staging` (case-insensitive) — no project name or environment id can satisfy it — every DSN must be typed explicitly and never falls back to `$DATABASE_URL`/`get_settings()`, and `RAILWAY_ENVIRONMENT_NAME` naming production refuses outright. `--dry-run` bypasses the DSN requirement but **not** the staging-target requirement. Nothing was executed here. Dry-run tests are meaningful (29 tests: pass AND fail fixtures per script, guard refusals, CLI end-to-end). |
| D3 (canary reconciliation) | yes | good | `compute_reconciliation` is pure; bytes ratio = ledger/provider with ±10%, CPU ratio = modeled/(measured vCPU-min × 60) with ±25%, `cost_model_drift_ratio` = the bytes ratio judged against [0.9, 1.1] — matching the plan and A5's gauge definition — and a zero/absent provider or measured figure yields `None` + a "cannot be evaluated" reason, never a silent pass (`passed` requires all four booleans true). `--max-usd` refusal verified live: exit 2 with the flag absent. Step 1 (spend) not run. |
| D4 (rollout runbook) | yes, with one non-executable command (follow-up 2) | good | ADVANCE bar and STOP rule are quoted verbatim and then spelled out; §0 states the staging environment is deleted after D2 sign-off before Step 1; §4 is a per-step gate table with `pending owner` in all four rows and "a blank cell is not a pass"; Step 2 deferred. The report's Deviation 1 is correct and load-bearing: the "C11-approved cost-per-valid-refresh bound" does not exist — `docs/ops/RELEASE_3_2026-09.md` §2.11 has exactly three decisions (retention ratification, `EXTRACTION_RANKING_POLICY`, `BROWSER_DOCUMENT_ONLY_DOMAINS`), none of them a dollar figure. Surfacing it rather than inventing a number was the right call. |
| D5 (daily scorecard) | yes | strong — verified independently | All 15 plan-named fields present on the migration's `CREATE TABLE` (confirmed against a live PG18 `\d`), the ORM model, `ScorecardRow` and the UPSERT binds. RLS manifest / `grants_expected.yaml` / `provision_db_roles.sql` trio all carry `fleet_daily_scorecard` with the `rollup_completion` posture, and `verify_grants.py` returns 0 MISSING against a freshly provisioned database. Both new routes sit on the existing `admin_ops` router, which carries `dependencies=[Depends(require_service_token)]` at construction — no second router, no unguarded path. `GET /admin/scorecard` is fleet-global by design (the table has no `workspace_id`) and behind the service-token guard, which is the correct scoping for a fleet table. `POST /admin/ops/backup-report` validates its payload's `schema` field against `crawmatic.backup-report.v1` and returns 422 on anything else; it is deliberately fail-soft afterwards (202 even when Redis is down) to match the poster's own fail-soft posture — a defensible asymmetry, documented in both the route and `record_backup_report`. |
| D6 (re-score) | yes | honest, verified | §8's table is 12 rows, **zero PASS**. I checked the two rows most at risk of overclaiming: Database cites D1's real 500k benchmark run and the A10/B10/C11 rehearsals but says plainly "pool exhaustion is genuinely unmeasured: no `pool_wait_p95` metric exists"; Security marks the live RLS suite as Stage-A vintage rather than assuming it still holds. The `PARTIAL` label (report Deviation 2) is a presentation choice, not a claim — every PARTIAL row still ends in "not achieved". `DEFERRED-ITEMS.md` closes the proxy-bytes item (A7/C6) with evidence, closes the Amazon HTTP-leg **tooling** half while opening a separate item for the still-deferred spend run, confirms the fair-queue item was already closed 2026-09-07, and adds a ten-item 2026-09-08 batch. |

Cross-cutting checks:

* **Scope** — `git status --porcelain` outside `.epa/` is exactly the 49 paths listed below; nothing
  under `/srv/crawmatic/saas`, no unauthorized file. The two out-of-packet edits
  (`tests/benchmarks/test_rollup_500k.py` by D1, `tests/unit/test_migration_offline_network_operations_partition.py`
  by D5) are both declared, both justified, and both were explicitly directed by the orchestrator.
* **`config.py` append (D5)** — one setting, `SCORECARD_INTERVAL_SECONDS`. No duplicate setting name
  anywhere in the file (checked by extracting every class-level `NAME:` and looking for repeats:
  none), and the block sits before the `@field_validator` section, not interleaved.
* **uv workspace excludes** — `exclude = ["apps/dr-backup", "apps/fixture-origin"]`. Necessary:
  `members = ["apps/*"]` would otherwise claim a directory with no `pyproject.toml` and break every
  `uv run`. D1 recorded `uv lock --check` → exit 0 (`uv.lock` untouched).
* **Docs consistency** — RELEASE_3 §2.11's three owner decisions are the ones the re-score's Quality
  (Decision 2) and Database (Decision 1) rows cite, and ROLLOUT §2 correctly reports that no fourth,
  cost-bound decision exists there. DEFERRED-ITEMS' 2026-09-08 batch matches the gaps the re-score
  names (pool_wait_p95, the cost bound, the stale RLS evidence, the 55-min startup gap, disk at 99%).
  No contradiction found between the four documents.
* **Secrets** — pattern scan over all 49 changed paths for assigned password/secret/token/api-key
  literals: no hits. The seeder's refusal text deliberately prints only the DSN host, never the user
  or password (asserted by its own test). No `env`/`printenv` was run.
* **Destructive actions** — none taken by any worker or by me. No Railway call, no deploy, no spend,
  no `git commit`, no `git checkout --`, no push. My throwaway container was removed.

## Findings (FAIL only, ordered by severity)

none.

## Files to commit (PASS only)

49 paths — `git status --porcelain --untracked-files=all` minus `.epa/`, untracked directories
(`apps/fixture-origin/`, `tests/load/fault_injection/`) expanded to files.

alembic/versions/f6b28c714a93_fleet_daily_scorecard.py
apps/api/app/routers/admin_ops.py
apps/fixture-origin/Dockerfile
apps/fixture-origin/app/__init__.py
apps/fixture-origin/app/main.py
apps/fixture-origin/railway.json
apps/scheduler/app/scheduler/scheduler_app.py
apps/workers/app/workers/celery_app.py
apps/workers/app/workers/tasks_maintenance.py
docs/DEFERRED-ITEMS.md
docs/PRODUCTION_READINESS_SCORE_2026-09.md
docs/ops/COST_MODEL_2026-09.md
docs/ops/FAULT_INJECTION_2026-09.md
docs/ops/FLEET_TEST_2026-09.md
docs/ops/ROLLOUT_2026-09.md
docs/ops/STAGING_ENVIRONMENT.md
libs/shared/app_shared/config.py
libs/shared/app_shared/maintenance/scorecard.py
libs/shared/app_shared/models/__init__.py
libs/shared/app_shared/models/fleet_daily_scorecard.py
libs/shared/app_shared/models/maintenance_cadence.py
libs/shared/app_shared/task_names.py
pyproject.toml
scripts/fleet_test_report.py
scripts/provision_db_roles.sql
scripts/reconcile_canary_costs.py
scripts/rls_table_manifest.txt
scripts/seed_synthetic_fleet.py
scripts/sql/grants_expected.yaml
tests/benchmarks/test_rollup_500k.py
tests/load/fault_injection/README.md
tests/load/fault_injection/_common.py
tests/load/fault_injection/host_limit_hold.py
tests/load/fault_injection/kill_scraper_after_fetch.py
tests/load/fault_injection/kill_worker_after_post.py
tests/load/fault_injection/one_broken_tenant.py
tests/load/fault_injection/pause_postgres_60s.py
tests/load/fault_injection/pause_redis_60s.py
tests/load/fault_injection/two_schedulers.py
tests/load/test_fault_injection_dry_run.py
tests/unit/test_admin_ops_scorecard_and_backup_report.py
tests/unit/test_fixture_origin_pages.py
tests/unit/test_fleet_test_report_gates.py
tests/unit/test_grants_manifest_schema.py
tests/unit/test_migration_offline_fleet_daily_scorecard.py
tests/unit/test_migration_offline_network_operations_partition.py
tests/unit/test_reconcile_canary_costs.py
tests/unit/test_scorecard_fields.py
tests/unit/test_seed_synthetic_fleet_shape.py

## Follow-ups (non-blocking)

1. **Four of the seven fault-injection scripts return `passed == True` on an EMPTY observation set**
   — a staging run where the injection never fired would print `verdict=PASS`. This contradicts the
   discipline `scripts/fleet_test_report.py` states in its own header ("a gate with no measurement
   is NO DATA, never PASS"), and these verdicts are what an operator copies into the report's
   `fault_injection` dict to settle the §13 Durability / Tenant fairness rows.
   * `tests/load/fault_injection/kill_worker_after_post.py:96` — needs `self.intents_examined > 0 and …`
   * `tests/load/fault_injection/pause_postgres_60s.py:93` — same, plus a positive-record guard
   * `tests/load/fault_injection/pause_redis_60s.py:90` — same
   * `tests/load/fault_injection/two_schedulers.py:93` — needs `occurrences_examined > 0` (or the
     equivalent field) alongside `duplicate_occurrence_rows == 0`
   `host_limit_hold.py:90`, `kill_scraper_after_fetch.py:81` and `one_broken_tenant.py:104` already
   carry exactly this guard and are the pattern to copy. Cheap fix; worth doing before D2 step 1 runs.
2. **`docs/ops/ROLLOUT_2026-09.md` §5 step 5 documents a command its own tool refuses.**
   `scripts/reconcile_canary_costs.py … --max-usd 0` exits 2 with
   `REFUSED: --max-usd is required and must be a positive number` (reproduced live), and the command
   also omits `--database-url`, which a live run requires. Either drop the step from the runbook or
   rewrite it with a positive cap and the DSN. Note that `--max-usd` is a *cap compared against the
   window's ledger cost*, so `0` would fail the spend gate even if the parser accepted it.
3. **`_common.require_staging` has no host allowlist on `--database-url`/`--redis-url`**, unlike
   `scripts/seed_synthetic_fleet.py:require_staging`, which refuses a DSN whose host is not
   loopback/container-local or does not contain `staging`/`fixture`/`fleet-test`. An operator who
   types `--staging-target staging` alongside a production DSN, from a shell without
   `RAILWAY_ENVIRONMENT_NAME`, is not stopped. Porting the seeder's host check into `_common.py`
   would make the strongest guard in the phase the shared one.
4. **`fleet_daily_scorecard.queue_oldest_seconds_p95` is a documented proxy**, not the p95 of the
   `queue_oldest_pending_seconds` gauge (reports/D5.md Deviation 1) — no time-series sample store
   exists to compute the real thing. Fine as a display metric; it must not later be quoted as the
   audit's queue-age measurement without that caveat.
5. **`railway_cpu_seconds`/`railway_ram_gb_hours`/`railway_egress_gb` are structurally always NULL**
   until a durable Railway-usage importer exists (D5 Deviation 3, pre-authorized by the packet).
   They will drag `missing_metric_fraction` up on every row, which feeds the re-score's Operations
   gate — worth a named importer task before that row can ever clear.
6. **`browser_share`/`proxied_share` count top-level `network_operations` rows only**, not C6's
   parent+child billing predicate (D5 Deviation 2). Acceptable for a mix gauge; must be swapped for
   `admin_usage.py`'s CTE if the scorecard is ever asked to match billing.
7. **Root-owned new files** (`alembic/versions/f6b28c714a93_*.py`, `libs/shared/app_shared/maintenance/scorecard.py`,
   `libs/shared/app_shared/models/fleet_daily_scorecard.py`, `docs/ops/`) — the same ownership drift
   the phase C review flagged. World-readable, so nothing is blocked, but a `chown -R mahmoud`
   over the repo before the release build is worth doing.
8. **D1's stale-statistics finding deserves an owner ticket beyond the docs.** A freshly
   restored/created partition plans the rollup batch as a nested loop; the fix is a
   `VACUUM ANALYZE` step inside `scripts/dr/rehearse_upgrade.sh` and the monthly partition-rollover
   path, not only a sentence in `docs/ops/STAGING_ENVIRONMENT.md` §5.
