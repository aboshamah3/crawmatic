# Phase C Review: Stage C — Efficient and correct results/cost
Verdict: PASS
Base SHA: 3730ab9   Reviewed: main tree (uncommitted), branch `epa/plan-core-production-readiness-2026-09-07`

## Integration command

Primary (the one the review focus names first — the whole Alembic chain applied from an EMPTY
database on the production major, then the RLS/grants/provisioning trio against the result). A
throwaway `postgres:18-alpine` on a random loopback port with a tmpfs data dir, torn down after;
`/` unchanged at 98% / 2.1 GB free.

`sudo -u mahmoud .venv/bin/alembic -x db_url=postgresql+psycopg://…@127.0.0.1:<rnd>/crawmatic upgrade head` → exit 0

```
INFO  [alembic.runtime.migration] Running upgrade d3f7a1b62c85 -> e7b34c0af219, domain rules fleet limits
INFO  [alembic.runtime.migration] Running upgrade e7b34c0af219 -> f4a19c7d2b80, domain rules request timeout
INFO  [alembic.runtime.migration] Running upgrade f4a19c7d2b80 -> a7c31d0f9e42, domain playbook strategy version
INFO  [alembic.runtime.migration] Running upgrade a7c31d0f9e42 -> c1e7b93a40df, extraction_shadow_events (EPA C5)
INFO  [alembic.runtime.migration] Running upgrade c1e7b93a40df -> e2a4f80c6b71, observation offer/provenance columns (EPA C5)
INFO  [alembic.runtime.migration] Running upgrade e2a4f80c6b71 -> b3f0c95a7d21, rollup completion checkpoint (EPA C7)
INFO  [alembic.runtime.migration] Running upgrade b3f0c95a7d21 -> d1f7a3c9e284, network operation resource summaries (EPA C9)
INFO  [alembic.runtime.migration] Running upgrade d1f7a3c9e284 -> a5e0c74b13d9, partition network_operations by month (EPA C9)
```

Supporting commands, all on the same scratch database or this tree:

| Command | Exit | Result |
|---|---|---|
| `alembic heads` | 0 | `a5e0c74b13d9 (head)` — exactly one head; chain `e7b34c0af219 → f4a19c7d2b80 → a7c31d0f9e42 → c1e7b93a40df → e2a4f80c6b71 → b3f0c95a7d21 → d1f7a3c9e284 → a5e0c74b13d9` |
| `provision_db_roles.py --provision` (empty DB, pre-migration) | 0 | `RESULT: PASSED — 0 FAIL, 2 WARN` |
| `provision_db_roles.py --provision --adopt-ownership` (post-migration) | 0 | `RESULT: PASSED — 0 FAIL, 5 WARN` — the 5 WARN are exactly the annotated GAP entries, incl. C9's two new ones |
| `verify_grants.py` | 0 | 62 tables × 3 roles, **0 MISSING / 0 UNEXPECTED**; WARNs are the monthly partition children only |
| `rls_verify.py` (as `crawmatic_app`) | 1 | Sections A + B PASS (`45 workspace-scoped tables` ENABLE+FORCE, ≥1 policy each). The single FAIL is `probe workspace available: no rows in \`workspaces\`` — an empty scratch DB has no tenant to probe; C9 ran the same script with two seeded workspaces and it exits 0 (`reports/C9.md` §4) |
| `pytest tests/unit -q -m "not integration"` (6 chunks, per runtime rules) | 0×6 | **4820 passed, 19 skipped, 6 deselected, 0 failed** — byte-identical to C9's and C11's own aggregates, on the fully merged tree |
| `pytest tests/integration/test_cost_authorization.py -q` | 0 | `28 passed in 24.33s` (26 pre-existing + the 2 this review added — see Per-task, C6) |
| `bash scripts/check_single_head.sh` (as mahmoud, via `uv run`) | 0 | `check_single_head: OK — exactly 1 head.` — C1's `uv` workspace blocker is closed by C10's `pyproject.toml` exclude |
| `python scripts/check_workspace_scoping.py` | 0 | `OK — no unscoped workspace-owned model access.` |

Schema facts verified directly on the migrated database (not taken from a report):
`network_operations` is `relkind = 'p'`, `RANGE (created_at)`, 2 monthly children, identity
`uq_network_operations_network_request_id_created_at`, **zero** inbound foreign keys (the five
documented drops), immutability trigger present, `network_operations_pre_partition` retained with
zero privileges for all three application roles.

## Per-task

| Task | Criteria met | Evidence quality | Notes |
|---|---|---|---|
| C1 | Yes | Strong (48 new unit tests + read of `attempt_budget.py`/`methods.py`) | Ladder hook lives in `app_shared/strategy/methods.py`, not the plan's non-existent `scrape_core/strategy/escalation.py` (CONTEXT unknown 3) — correct call. Both carry-forwards were closed by C4, verified at the call sites, not just claimed. Fail-OPEN on Redis error is deliberate and argued; the wall-clock deadline is the bound that survives an outage. |
| C2 | Yes | Adequate (29 unit tests; spend step deferred per ASSUMPTIONS answer 3) | The live fetch loop is intentionally absent — flagged in the report, and the deferred command is in `docs/ops/AMAZON_STRATEGY_2026-09.md` with the result rows blank. |
| C3 | Yes | Strong (13 new + 2 pre-existing suites flipped) | `should_block` no longer requires `transport == "PROXY"`; `BLOCKLIST_VERSION = 3`; the proxied setting survives as an alias. `amazon.sa` is **not** listed — confirmed by `test_amazon_sa_is_not_listed_by_this_change_itself`. |
| C4 | Yes | Strong (44 new unit tests; migration verified live) | `budget=` + `playbook=` are genuinely passed at both named call sites (`tasks_jobs._resolve_domains_and_modes`, `targets.load_targets`), with terminal refusals finalising the target FAILED and dropping it from the plan. `CrossWorkspaceCoalescingUnsupported` **survives** PC-5's edits and is the same object on all three import paths (`costauth.service` → `costauth` → `jobs.coalescing`); `coalesced_groups` raises it on mixed-workspace input. |
| C5 | Yes | Strong (54 new unit tests incl. two end-to-end spider→`_flush_batch` replays) | Offer contract on `ScrapeResult`; `EXTRACTION_RANKING_POLICY` default is `'shadow'` (verified in `config.py`, not set to `v1` anywhere); both spiders pass `raw_evidence=response.body or None`; `result_spool._NON_SPOOLED_FIELDS = {"offer","raw_evidence"}` — a genuine outage-class bug the continuation caught and fixed. `extraction_shadow_events` fleet-scoping: **accepted as fleet-scoped (no `workspace_id`, no RLS)** — the row is a fact about an extraction POLICY on a public page and any single workspace id would be arbitrary. Its manifest CLASS is wrong though; see Follow-up 1. |
| C6 | Yes | Strong (28 unit + 7 DB-backed integration; reviewer added 2 more) | F17 aggregation is over `COUNT(DISTINCT network_request_id)` with the conjunctive paid predicate and `MAX`-not-`SUM` on `op_totals`. F18 wiring landed on attempt 2: `_batch_first_rung_reservation` degrades to the pre-C6 fail-closed derivation on all three unknown paths. **The flagged untested DIRECT/zero-cost `authorize()` path is now closed by this review** — see Findings/none and Follow-up 2. |
| C7 | Yes | Strong (27 render + 7 integration written; SQL executed live) | The batch CTE is correct on read: `DISTINCT ON (workspace_id, product_variant_id, match_id) … ORDER BY … scraped_at DESC` collapses per competitor BEFORE `MIN/AVG/MAX/COUNT`; the eligibility filter is inside the CTE ("latest ELIGIBLE"); the measured half-open predicate `scraped_at >= :day_start AND < :day_end` is preserved verbatim; the keyset window is a row comparison with ascending ORDER BY, and `cursor_key` takes the batch max — a correct, non-overlapping resume. `benchmark` marker registered in `pyproject.toml`; `tests/benchmarks/test_rollup_500k.py` written and NOT run (D1, per the plan). |
| C8 | Yes | Strong (integration run green against a real fully-migrated Postgres) | `rollups_cover` requires BOTH gates and **fails closed** when `rollup_completion` is unmigrated (`completion_store_available` → `return False`), not "skipped". Gate 2 deliberately requires a `complete` checkpoint for every calendar date in range, quiet days included. |
| C9 | Yes | Strong (14 integration tests green as `crawmatic_auth` on an ownership-adopted DB; swap re-verified by this review) | `RETENTION_ENABLED_CLASSES` ships `[]` → retention is OFF for every family (fail-safe direction, pre-accepted in ASSUMPTIONS.md 2026-09-08). The FK/uniqueness trade is documented in the migration docstring, in `docs/RETENTION_POLICY.md` §2.9 and in `RELEASE_3` §2.11 decision 1 with an unsigned `Ratified by owner on ____` line — 0 `PENDING OWNER` left, 31 ratification lines present. `provision_db_roles.sql` §9 reviewed below. |
| C10 | Yes | Strong (36 unit tests, mutation-verified RED; real image build + end-to-end backup/restore drill in throwaway containers) | Production major DETERMINED (18.4/18.6 from dump headers), Dockerfile pinned to 18 and pinned by a test. Triple-enforced private-network egress guard. `pyproject.toml` `exclude = ["apps/dr-backup"]` verified — `uv run`/`check_single_head.sh` work again. `serve.py` read on merit: fail-closed bearer token via `hmac.compare_digest`, GET-only, strict `SET_RE`/`FILE_RE` + `resolve()`-inside-`SETS_DIR` check (traversal closed by construction), streamed reads. `backup_prod.sh`'s legacy dump fallback is the right call — deleting it would leave production with no backups until the deferred owner gate is taken. |
| C11 | Yes | Strong (real 10/10 rehearsal on `set-20260908T160001Z`, incl. the swap on 16,706 real rows) | `RELEASE_3_2026-09.md` is consistent with the sources I checked independently: every new variable name matches a real `Settings` field and its real code default (`EXTRACTION_RANKING_POLICY=shadow`, `EVIDENCE_STORE_DIR=None`, `RETENTION_ENABLED_CLASSES=[]`, `BROWSER_DOCUMENT_ONLY_DOMAINS` unset, `RETENTION_PRICE_OBSERVATIONS_DAYS=180`, `PROXY_BILLING_UNIT_BYTES`), the head it names is the head that exists, and the 14 owner items carry every BLOCKERS.md/ASSUMPTIONS.md entry forward. |

### `scripts/provision_db_roles.sql` §9 — privilege review (review focus 5)

The seam is the right shape and is **not** a generic DDL hole. Both functions validate against
`pg_catalog` before executing: create requires the parent to be `relkind = 'p'` and the child to
match `^<parent>_YYYY_MM$`; reclaim requires `relispartition` and treats absent as a no-op
(idempotence, FR-020). Every dynamic name goes through `format('%I')` and every range bound through
`%L`, so injection is closed. `search_path` is pinned with `pg_temp` last, `PUBLIC` is revoked
before the single `EXECUTE` grant to `crawmatic_auth`, and the functions are reassigned to
`crawmatic_migrate` so the definer is the owner — on a database where §8's ownership adoption never
ran they fail exactly as the direct DDL does, which is the correct non-escalating behaviour.
Choosing this over `GRANT crawmatic_migrate TO crawmatic_auth` is the right trade and is argued in
the file. Copying the parent's `relacl` onto the child (via `aclexplode`, `grantee <> 0`) and
re-applying the parent's RLS posture inside the same privileged call are both load-bearing and
correct: privileges are checked on the relation a query NAMES, and retention's own coverage gate
scans the child by name. Two residual observations, neither blocking, are Follow-ups 3 and 4.

## Findings (FAIL only, ordered by severity)

none.

## Files to commit (PASS only)

Every path below is `git status --porcelain` minus `.epa/`, with the two untracked directories
expanded to files (116 porcelain entries → 122 files). **Two of them carry reviewer-authored
changes**, disclosed here rather than buried: `tests/integration/test_cost_authorization.py` (two
new cases closing C6's flagged untested path) and `docs/ops/RELEASE_3_2026-09.md` (owner item 6
marked closed to match). Nothing else in the tree was touched by this review.

alembic/versions/a5e0c74b13d9_partition_network_operations.py
alembic/versions/a7c31d0f9e42_domain_playbook_strategy_version.py
alembic/versions/b3f0c95a7d21_rollup_completion.py
alembic/versions/c1e7b93a40df_extraction_shadow_events.py
alembic/versions/d1f7a3c9e284_network_operation_resource_summaries.py
alembic/versions/e2a4f80c6b71_observation_offer_provenance.py
alembic/versions/f4a19c7d2b80_domain_rules_request_timeout.py
apps/api/app/schemas/admin.py
apps/api/app/services/admin_usage.py
apps/dr-backup/Dockerfile
apps/dr-backup/backup.sh
apps/dr-backup/railway.json
apps/dr-backup/serve.py
apps/scheduler/app/scheduler/scheduler_app.py
apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py
apps/scrapers/price_monitor/spiders/generic_price_spider.py
apps/workers/app/workers/celery_app.py
apps/workers/app/workers/tasks_jobs.py
apps/workers/app/workers/tasks_maintenance.py
docs/RETENTION_POLICY.md
docs/ops/AMAZON_STRATEGY_2026-09.md
docs/ops/DOMAIN_STRATEGIES_2026-09.md
docs/ops/RELEASE_3_2026-09.md
libs/scrape-core/scrape_core/adapters/html.py
libs/scrape-core/scrape_core/attempt_budget.py
libs/scrape-core/scrape_core/errors.py
libs/scrape-core/scrape_core/extraction/pipeline.py
libs/scrape-core/scrape_core/items.py
libs/scrape-core/scrape_core/pipelines.py
libs/scrape-core/scrape_core/result_builder.py
libs/scrape-core/scrape_core/result_spool.py
libs/scrape-core/scrape_core/targets.py
libs/shared/app_shared/config.py
libs/shared/app_shared/costauth/__init__.py
libs/shared/app_shared/costauth/service.py
libs/shared/app_shared/enums.py
libs/shared/app_shared/jobs/batching.py
libs/shared/app_shared/jobs/coalescing.py
libs/shared/app_shared/maintenance/domain_timeouts.py
libs/shared/app_shared/maintenance/ledger_summaries.py
libs/shared/app_shared/maintenance/partitions.py
libs/shared/app_shared/maintenance/registry.py
libs/shared/app_shared/maintenance/retention.py
libs/shared/app_shared/maintenance/rollup_sql.py
libs/shared/app_shared/maintenance/rollups.py
libs/shared/app_shared/models/__init__.py
libs/shared/app_shared/models/cost_authorization.py
libs/shared/app_shared/models/domain_playbooks.py
libs/shared/app_shared/models/domain_rules.py
libs/shared/app_shared/models/maintenance_cadence.py
libs/shared/app_shared/models/network_operation_summaries.py
libs/shared/app_shared/models/network_operations.py
libs/shared/app_shared/models/observations.py
libs/shared/app_shared/models/rollup_completion.py
libs/shared/app_shared/observations/EVIDENCE_RETENTION.md
libs/shared/app_shared/observations/evidence_store.py
libs/shared/app_shared/opsmetrics/__init__.py
libs/shared/app_shared/opsmetrics/emit.py
libs/shared/app_shared/profiles/browser_resource_policy.py
libs/shared/app_shared/strategy/methods.py
libs/shared/app_shared/task_names.py
pyproject.toml
scripts/canary_document_only_browser.py
scripts/dr/RUNBOOK.md
scripts/dr/backup_prod.sh
scripts/dr/dr_lib.sh
scripts/dr/verify_restore.sh
scripts/probe_amazon_http_leg.py
scripts/provision_db_roles.sql
scripts/rls_table_manifest.txt
scripts/run_domain_canary.py
scripts/run_offer_benchmark.py
scripts/seed_domain_playbooks.sql
scripts/sql/grants_expected.yaml
tests/benchmarks/test_rollup_500k.py
tests/fixtures/labeled_offers/README.md
tests/fixtures/labeled_offers/amazon.jsonl
tests/fixtures/labeled_offers/noon.jsonl
tests/fixtures/labeled_offers/stech.jsonl
tests/integration/test_admin_usage_fixtures.py
tests/integration/test_cost_authorization.py
tests/integration/test_ledger_child_summarization.py
tests/integration/test_network_operations_partition_swap.py
tests/integration/test_partition_create_live.py
tests/integration/test_retention_partial_rollup_multi_tenant.py
tests/integration/test_rollup_set_based.py
tests/unit/_rollup_batch_fake.py
tests/unit/models/test_network_operations.py
tests/unit/test_admin_router.py
tests/unit/test_admin_usage_sql.py
tests/unit/test_attempt_budget.py
tests/unit/test_backfill_daily_rollups.py
tests/unit/test_backup_report_fields.py
tests/unit/test_browser_resource_policy.py
tests/unit/test_browser_resource_policy_both_legs.py
tests/unit/test_browser_ssrf.py
tests/unit/test_coalescing_key.py
tests/unit/test_current_price_confidence_guard.py
tests/unit/test_domain_timeout_tune.py
tests/unit/test_dr_backup_script_lint.py
tests/unit/test_error_classification.py
tests/unit/test_evidence_retention_gate.py
tests/unit/test_grants_manifest_schema.py
tests/unit/test_migration_offline_c5_offer_provenance.py
tests/unit/test_migration_offline_network_operations_partition.py
tests/unit/test_migration_offline_rollup_completion.py
tests/unit/test_offer_propagation.py
tests/unit/test_ops_metrics_baseline.py
tests/unit/test_ops_metrics_snapshot.py
tests/unit/test_partition_bounds.py
tests/unit/test_partition_registry.py
tests/unit/test_partition_seam.py
tests/unit/test_playbook_strategy.py
tests/unit/test_probe_amazon_report.py
tests/unit/test_ranker_shadow_events.py
tests/unit/test_reservation_first_rung.py
tests/unit/test_retention_eligibility.py
tests/unit/test_retention_registry_classes.py
tests/unit/test_rollup_aggregation.py
tests/unit/test_rollup_sql_render.py
tests/unit/test_rollup_watermark.py
tests/unit/test_spider_raw_evidence.py

## Follow-ups (non-blocking)

1. **`extraction_shadow_events` is filed SYSTEM but meets the manifest's own GAP test.** The table
   carries `domain` and `url` (competitor page URLs). `scripts/rls_table_manifest.txt`'s
   `provider_usage_records` entry states the rule explicitly — "a domain is tenant-linked evidence
   … even though no single row-level tenant owns it" — and files that table GAP for exactly this
   reason. Filing this one SYSTEM keeps it out of the tracked read-side-exposure list
   `provision_db_roles.py --verify` prints on every run. Separately, `crawmatic_app` holds `SELECT`
   on it while **no application code reads it** (`pipelines._flush_batch` only INSERTs; the C11 gate
   reads a JSONL export). Recommended: reclassify to GAP, and drop `crawmatic_app`'s `SELECT` in
   `grants_expected.yaml` + `provision_db_roles.sql`. I did NOT reverse C5's fleet-scoping decision
   itself — that reading is sound and reversing it would mean a workspace column on a page-level
   fact. Small, self-contained task.
2. **Mid-run reactor-side escalation still runs under the dispatch-time grant.** Now that a
   DIRECT-first batch reserves 0 bytes / 0 money (F18, correct), a spider that climbs to a proxied
   or browser rung inside `parse` spends against a zero-money grant. The spend is still booked and
   settled — the *next* authorization sees it in `settled_*` — but it is no longer pre-checked
   against the period ceiling the way the old blanket-PROXY reservation checked it. Pre-C6 that
   grant was always the paid one, so this is a real (fail-open direction) narrowing, bounded by the
   wall-clock deadline, the attempt budget and the next dispatch's gate. Both C4 and C6-2 flag the
   same boundary; schedule the reactor-side wiring (budget + escalation reservation) as one task.
3. **`crawmatic_drop_partition` can drop ANY partition in `public`, not only an expired one of a
   retention-registered family.** That is the capability retention needs and the role that runs it,
   so it is not a privilege *escalation* — but it is broader than the registry, and a bug or a
   compromised `crawmatic_auth` DSN can drop a live month. Cheap narrowing: require the parent to be
   in `PARTITIONED_TABLES` and/or the child's upper bound to be in the past.
4. **`crawmatic_create_partition` interpolates `parent_name` into a regex unescaped**
   (`child_name !~ ('^' || parent_name || '_[0-9]{4}_[0-9]{2}$')`). Harmless for every table name in
   this schema (no regex metacharacters), but an equality check against the derived
   `<parent>_YYYY_MM` string would remove the class entirely.
5. **110 of the 122 files in the change set are root-owned.** Docker-backed work in this stage ran
   as root (the `docker` group gap is already RELEASE_3 owner item 12), and the files those workers
   wrote inherited it. Nothing about the commit is affected — git does not record ownership — but
   `mahmoud` can no longer edit them, which will break the next stage's workers. Run
   `chown -R mahmoud:mahmoud /srv/crawmatic/crawmatic` (excluding `.git` is unnecessary; it is
   already mahmoud's) before Stage D dispatches.
6. **`RETENTION_PRICE_OBSERVATIONS_DAYS` 90 → 180 doubles the retained-partition count for the
   hottest table** once retention is enabled. It is the plan's own criterion and is documented in
   `RELEASE_3` §2.3 and `RETENTION_POLICY.md`, and it is inert while `RETENTION_ENABLED_CLASSES` is
   empty — but on a host at 98% disk it should be sized before decision 1 is signed.
7. **`rls_table_manifest.txt`'s C5 comment says "taking the count to 59"**; three more relations
   landed after it (C7's `rollup_completion`, C9's two), and the catalog now annotates 62. Cosmetic,
   but the number is the kind of thing a later reader trusts. One-line fix.
8. **49 dangling Docker volumes on this host**, accumulated by the throwaway-container work across
   Stages A–C (a `docker rm -f` without `-v` leaves the image's anonymous volume behind). Reclaimable
   space on a host at 98% — folds into the existing disk owner item. `docker volume ls -qf
   dangling=true` to inspect, `docker volume prune` to reclaim; NOT run here (pruning shared Docker
   state is not an authorized action for this review, same reasoning C10 applied to its build cache).
9. Carried forward unchanged, already recorded by the tasks and by `RELEASE_3`'s 14 owner items:
   `POST /admin/ops/backup-report` has no receiver; the browser spider's timeout is not wired to
   `domain_rules.request_timeout_seconds`; `find_orphan_references` has no caller; the four
   pre-existing live-Postgres integration-test defects (`test_partition_create_live.py`,
   `test_retention_drop_live.py`) and the four files still building partitions with raw DDL;
   `docker-compose.yml` still pinned to `postgres:17.5-bookworm` against a production 18;
   `network_operations_pre_partition` awaiting the owner's DROP; the 500k rollup benchmark unrun.
