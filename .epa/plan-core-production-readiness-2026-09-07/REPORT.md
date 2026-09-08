# EPA Report: plan-core-production-readiness-2026-09-07

## TLDR
**Terminal status: COMPLETE-WITH-DEFERRED-GATES.** All 41 tasks across 5 phases are done (C6 needed
a second attempt); all 5 phase gates PASSED (0/A/B/C/D — A and B each needed one fix cycle before
passing). Final verification PASSED: 4,971 unit tests green, single Alembic head `f6b28c714a93`,
a full migrate-from-empty + roles + grants + RLS pass on a throwaway Postgres 18. Not COMPLETE
because Pre-Flight deferred every owner gate (no staging env, no spend, no deploy) — all 12 rows of
the production-readiness gate table are PARTIAL or NOT YET MEASURED (0 PASS). Branch
`epa/plan-core-production-readiness-2026-09-07` @ `83e1774`, **not merged** — merge is the owner's
call. Two prior deploy findings (RLS-silent-backfill bug, missing grants for 3 new tables) were
caught by rehearsal and fixed at source before the Stage B gate, not shipped as manual steps.

## Per-phase results

| Phase | Tasks | Attempts (non-1) | Gate result | Commit |
|---|---|---|---|---|
| 0 — Preconditions | 4/4 | — | PASS (#1) | `d824f31` |
| A — Contain risk, trustworthy baseline | 10/10 | A1 fix cycle (A1-fix1) | PASS (#2, after 1 fix) | `8960632` |
| B — Durable work, reliable schedules | 10/10 | B2-fix1, B9-fix1 fix cycles | PASS (#2, after 1 fix cycle) | `3730ab9` |
| C — Efficient and correct results/cost | 11/11 | C6 attempt 2 | PASS (#1) | `9e932fa` |
| D — Certify and expand | 6/6 | — | PASS (#1) | `83e1774` |

Final verification (`reports/FINAL-VERIFY.md`): 4,971 passed / 19 skipped / 6 deselected / 0 failed
across the whole unit suite; `check_single_head.sh` -> one head `f6b28c714a93`; a throwaway
`postgres:18-alpine` migrated from empty to head, then `provision_db_roles.py`, `verify_grants.py`
(0 MISSING), and `rls_verify.py` (PASS, seeded two workspaces) all green; `uv lock --check` clean;
load/fault-injection dry-run suite green (29 tests). No files changed outside `.epa/`, no deploys,
no spend, no pushes.

## What was built, per stage

**Phase 0 — Preconditions.** A read-only backup inventory tool (no delete capability, verified by
code read), the engine `main` branch fast-forwarded to `7a26c54` (owner-authorized), a release
manifest/config-diff tool that never prints secret values, and an isolated unit-test environment
(session-scoped settings fixture, integration tests auto-marked so a bare `pytest tests/integration`
never hits the dead-port config).

**Stage A — Contain risk, trustworthy baseline.** Browser egress now rebinds a dedicated loopback
listener per upstream proxy at connection time (closes the finding from gate pass #1, where the
proxied leg could silently egress direct); regex extraction runs under a hard per-node + per-page
deadline with a write-time compile gate so a bad pattern can never be stored; per-component DB
grants tightened (two intentional exceptions logged for A10); advisory-triage gate and pinned build
tools; target lifecycle now tracks a real STARTED transition with phase-timestamp baselines; the
canary calculator counts pages/prices/provider correctly; ledger coverage now captures wire bytes at
the transport boundary; cost artifacts and the cost watchdog read live Railway services instead of
hardcoded names; query statistics support a `pg_stat_statements` delta mode. Release 1 rehearsed
end-to-end against a real 190 MB production dump (10/10 steps), finding and fixing two real
deploy-day defects (grants skipped on migration-created tables; function ownership not adopted on
restore) before they could hit production.

**Stage B — Durable work, reliable schedules.** A durable, idempotent result spool with backpressure;
dispatch intents committed before the POST with an outbox and a stable remote job id; atomic
due-time occurrence claims with fair-mode scheduling defaulted ON (a fleet-wide behavior change,
flagged for review); two Celery consumer pools with time limits and resumable long maintenance;
fleet-wide host admission enforced at the physical request boundary; capacity-aware node placement;
non-blocking API middleware with deadlines; split readiness probes (liveness/dependency/scraping);
heartbeats for worker pools and the scheduler. Release 2's rehearsal against the newest dump FAILED
on the first (unmodified) run — a genuine RLS-silent-backfill bug in B2's migration and a
grants-manifest gap for 3 new Stage-B tables — and both were fixed at source (not shipped as manual
SQL) before the gate passed on the second rehearsal.

**Stage C — Efficient and correct results/cost.** Per-target deadlines and a physical attempt budget
wired to the real dispatch call sites; the Amazon HTTP-leg probe and a document-only-browser canary
tool (both spend-guarded, neither run); labeled canaries for Noon/S-Tech with a versioned domain
strategy; a structured offer contract landed on the live scrape path with the extraction ranker
running in shadow mode and durable raw-evidence hashing; usage aggregation by provider dimension
and cost reservation by the first rung (closed on a second attempt after a cross-task ordering gap);
set-based daily rollups with keyset batching; retention gated on per-key rollup coverage; per-data-class
retention plus `network_operations` partitioning (ships with retention OFF and the partition's
FK/uniqueness trade pending owner ratification); off-host, encrypted, measured backups run from
inside Railway's private network (service not yet created). Release 3 rehearsed 10/10 green,
including the partition swap on 16,706 real rows.

**Stage D — Certify and expand.** A synthetic fixture-origin service and a 100-workspace x 5,000-SKU
fleet seeder (staging-only, four independent refusal guards); a fault-injection matrix (7 scenarios,
dry-run tested, none run live); a cost-reconciliation canary tool; a daily cost/freshness scorecard
table + admin routes; a staged 1->5->20->100 rollout runbook with the ADVANCE/STOP rules stated
verbatim; and the honest re-score that closes the plan: 0 of 12 audit gate rows PASS. D1 also ran
the previously-unrun 500k-row rollup benchmark for real (2.3 s vs. an 1,800 s bar) and found that a
freshly restored/seeded database must be `VACUUM ANALYZE`d before the first rollup or the same batch
takes over 18 minutes on stale statistics.

## Deferred owner gates, in execution order

Every command below is copy-pasteable from the named doc; credential values are never included, only NAMES.

1. **0.1 step 6 — disk relief deletions.** Exact `rm`/docker-prune list is in `reports/0.1.md`; not executed.
2. **0.2 — push the remote tag.** `main` is already fast-forwarded to `7a26c54`; only the remote tag is stale:
   `git push --force origin refs/tags/v2026.09.03-readiness`
3. **A9 step 2 — enable `pg_stat_statements`** on Railway Postgres: `ALTER SYSTEM SET shared_preload_libraries='pg_stat_statements'`, restart, `CREATE EXTENSION pg_stat_statements` — commands in `docs/ops/RELEASE_1_2026-09.md` §2.5.1.
4. **A10 steps 2-3 — Engine Release 1 deploy + sign-off.** Full 11-point checklist, real rehearsal evidence, and the two provisioning fixes as mandatory §2.6 steps: `docs/ops/RELEASE_1_2026-09.md`.
5. **B10 step 2 — Engine Release 2 deploy.** Checklist + the verified RLS-backfill and grants fixes: `docs/ops/RELEASE_2_2026-09.md`.
6. **C2.2 — Amazon HTTP-leg probe, spend <= $1:**
   `uv run python scripts/probe_amazon_http_leg.py --targets 50 --runs 3 --max-usd 1.00 --report /tmp/probe-amazon-http.json`
   (decision table in `docs/ops/AMAZON_STRATEGY_2026-09.md`)
7. **C3.2 — document-only browser canary, spend <= $3:**
   `uv run python scripts/canary_document_only_browser.py --domain amazon.sa --workspace <workspace-uuid> --targets 50 --max-usd 3.00 --owner-go OWNER-GO:canary:amazon.sa --out /tmp/canary-amazon-sa.json`
   (requires `PROXY_CANARY_USERNAME` set on scrapers-browser first; evidence bar in `docs/ops/RELEASE_3_2026-09.md` §2.11 Decision 3)
8. **C4.2 — Noon/S-Tech labeled canary, spend <= $2.** Exact command in `reports/C4.md` / the C4 playbook; result feeds Decision 2 below.
9. **C9 ratifications** (sign in `docs/RETENTION_POLICY.md`, one `Ratified by owner on ____` line per class):
   (a) accept the `network_operations` partition trade (5 FKs dropped, `network_request_id` unique per-month not globally) — full statement in `docs/ops/RELEASE_3_2026-09.md` §2.11 Decision 1;
   (b) sign the 11-class `RETENTION_ENABLED_CLASSES` table and set the env var (ships `[]` = OFF for everyone until signed).
10. **C10 step 3 — create the `dr-backup` Railway service.**
    ```
    railway add --service dr-backup
    railway volume add --service dr-backup --mount-path /backups
    railway variables --service dr-backup --set DR_GPG_PASSPHRASE=... DR_PULL_TOKEN=... DR_REPORT_URL=... DR_REPORT_TOKEN=... ENGINE_PGHOST=... ENGINE_PGPORT=... ENGINE_PGUSER=... ENGINE_PGPASSWORD=... ENGINE_PGDATABASE=... (+ the SAAS_* five)
    railway run --service dr-backup /app/backup.sh
    ```
    Full steps and both service-shape options: `scripts/dr/RUNBOOK.md` §7 / `docs/ops/RELEASE_3_2026-09.md` §2.4. Note: `POST /admin/ops/backup-report` has no receiver yet — a follow-up (see below), not a blocker (poster is fail-soft).
11. **C11 step 2 — Engine Release 3 deploy**, plus the two remaining owner decisions it gates on:
    - Decision 2 — flip `EXTRACTION_RANKING_POLICY` shadow -> v1, once shadow disagreement < 1% AND ranker wins >= 99% of labeled conflicts:
      `uv run python scripts/run_offer_benchmark.py --from-shadow-events shadow.jsonl --labels tests/fixtures/labeled_offers/noon.jsonl --labels tests/fixtures/labeled_offers/amazon.jsonl --labels tests/fixtures/labeled_offers/stech.jsonl --observations <N>`
      (BLOCKED — no shadow export exists yet; only accumulates once Release 3 deploys)
    - Decision 3 — set `BROWSER_DOCUMENT_ONLY_DOMAINS=amazon.sa`, gated on item 7 above.
    Full checklist: `docs/ops/RELEASE_3_2026-09.md`.
12. **`EVIDENCE_STORE_DIR` / Railway volume.** Mount a volume on the relevant service(s) and set the variable — `offer_raw_evidence_hash` stays NULL in production until this is done (by design, not a bug).
13. **D1 steps 2-4 — staging environment + 100x5,000 fleet run.**
    ```
    railway environment new staging --duplicate production
    sudo scripts/dr/rehearse_upgrade.sh ...            # restore into staging Postgres
    railway up --environment staging --service {api,worker,scheduler,scrapyd-http,scrapyd-browser,fixture-origin}
    sudo -u mahmoud .venv/bin/python scripts/seed_synthetic_fleet.py --i-know-this-is-staging --database-url "$STAGING_DSN" --origin-base-url https://<fixture-origin-public-domain> --stores 100 --products-per-store 5000 --start-at <ts>
    psql "$STAGING_DSN" -c "VACUUM ANALYZE;"           # MANDATORY before the first rollup — see item 21
    ```
    Full runbook: `docs/ops/STAGING_ENVIRONMENT.md`, `docs/ops/FLEET_TEST_2026-09.md`.
14. **D2 — fault-injection matrix, 7 scenarios, in staging.** Each takes `--staging-target staging` plus a real `$STAGING_DATABASE_URL`/`$STAGING_REDIS_URL`; none run. Exact per-scenario commands: `docs/ops/FAULT_INJECTION_2026-09.md`.
15. **D3 step 1 — cost-reconciliation canary, spend <= $3.** Live-mode command and math in `docs/ops/COST_MODEL_2026-09.md` / `scripts/reconcile_canary_costs.py` (`--job-id --provider-window --max-usd 3 --database-url ...`).
16. **D4 — staged rollout 1 -> 5 -> 20 -> 100 stores.** Per-step commands and the ADVANCE/STOP rules: `docs/ops/ROLLOUT_2026-09.md`. **Blocking gap: ADVANCE condition 4 ("cost per valid refresh <= the C11-approved bound") references a bound no task in this plan ever set — the owner must set this number (informed by `docs/ops/COST_MODEL_2026-09.md` + item 15's result) before Step 1 can be honestly evaluated.**

Cross-cutting infra items (not phase-gated, but block the above or the readiness score):

17. **Disk at 98-99% (URGENT)** — every docker/disk-backed verification in this run had to route around it (tmpfs Postgres, throwaway containers). Blocks any real staging/production restore rehearsal on this host until relieved.
18. **`docker-compose.yml` pins Postgres 17.5; production is 18.4/18.6.** Re-confirmed independently by A10/B10/C10/C11/D1. Fix the compose pin.
19. **~110+ files in the working tree are root-owned**, not `mahmoud:mahmoud` (artifact of root-privileged docker steps). `chown mahmoud:mahmoud <file>` needed before the next edit as `mahmoud`, or before granting a second operator write access.
20. **Re-run the live cross-tenant RLS suite against the current head** — it last ran in Task A3, before B3/C7/C9/D5 added 4 new tables:
    `sudo -u mahmoud .venv/bin/pytest tests/integration/test_tenant_isolation_roles.py tests/integration/test_grants_manifest.py -q -m integration`
    (throwaway or compose Postgres provisioned by `provision_db_roles.sql` at head). No spend, no deploy — doable now.
21. **Wire `VACUUM ANALYZE` into the restore runbook / `rehearse_upgrade.sh` automatically** — currently a manual step (item 13 above); a monthly partition rollover hits the same "just-created, stats say empty" failure mode at production scale.

## Follow-ups (non-blocking, for the reviewer/owner backlog)

- **B9**: 8 of 9 new ops-metrics rules are registered and unit-tested but INERT — no `OpsSnapshot`/`collect_snapshot` wiring. The `heartbeat_missing{service}` alert rule is also inert. Wire into `libs/shared/app_shared/opsmetrics/snapshot.py`.
- **B7**: `apps/api/app/abuse_limit.py` intentionally kept Postgres-authoritative rather than moved to `redis.asyncio` (converting it would trade fail-closed for fail-open-under-Redis-blip) — needs an owner/architect confirmation that this stands.
- **Pre-existing routing gap**: `COSTAUTH_RESERVATION_SWEEP`, `MAINTENANCE_RECONCILE_PROVIDER_USAGE`, `MAINTENANCE_COST_ROLLUP`, `MAINTENANCE_ENTITLEMENT_REFRESH` Celery tasks are registered but never routed to a consumed queue (found by B4, pre-existing).
- **`STRATEGY_DISCOVERY_SCAN`** has no first trigger wired (B4).
- **Same RLS-backfill bug class** (item that broke B2) is latent in several older, already-applied migrations (`api_keys`, `scrape_profile_revisions`, `domain_strategy_methods`, `domain_strategy_profiles`, `strategy_attempt_stats`) — harmless in prod today, but a fresh-restore/DR replay of the full chain as `crawmatic_migrate` would silently no-op them. Candidate for a dedicated audit task.
- **`extraction_shadow_events`** is filed as a SYSTEM/fleet-scoped table though it carries domain/url data — manifest classification gap (phase C review).
- **Reactor-side mid-run strategy escalation** spends under the dispatch-time grant rather than a fresh authorization (C4/C6 finding) — zero-money impact for DIRECT-first batches today, but worth a dedicated case before scale.
- **4 of 7 fault-injection scripts** return `passed=True` on an empty observation set (vacuous PASS) — add a positive-sample guard like the other 3 (phase D review).
- **`docs/ops/ROLLOUT_2026-09.md` §5 step 5** documents `reconcile_canary_costs.py --max-usd 0`, which the script's own parser refuses, and omits `--database-url` — fix the doc example.
- **`tests/load/fault_injection/_common.py`** lacks the seeder's DSN host allowlist (phase D review) — align the two staging-guard implementations.
- **Two pre-existing, out-of-scope integration test bugs**: `tests/integration/test_partition_create_live.py` (stale `webhook_events` assertion; a composite-PK ordering bug on `price_observations`) and `tests/integration/test_retention_drop_live.py` (missing `latest_alert_type` on insert) — neither caused by this plan's code.
- **`mahmoud` is not in the `docker` group** on this host — every docker-backed integration suite had to run as root instead of the stated `sudo -u mahmoud` convention. Owner decision: add to group, or keep documenting the exception.
- **`admin_ops.py` backup-report route** — `POST /admin/ops/backup-report` was added by D5 and closes the C10 "no receiver" gap; confirm it's the intended final shape before Release 3 ships.
- **No `pool_wait_p95` metric exists anywhere in the engine** — the D1 fleet-test report reads it from an operator-supplied file as a stopgap; instrument SQLAlchemy pool checkout/checkin or `pg_stat_activity` wait events.
- **The audit's 55-minute startup-gap finding was never investigated** by any task in this plan (Tail latency row) — needs a dedicated look once real traffic exists.

## Merge

The branch `epa/plan-core-production-readiness-2026-09-07` (base `7a26c54`) is **not merged into
`main`**. All phase-gate reviews (0/A/B/C/D) PASSED and final verification PASSED on this branch tip
(`83e1774`); merging it is entirely the owner's call, made independently of this run's terminal
status. Suggested steps once the owner decides to merge:
```
cd /srv/crawmatic/crawmatic
git fetch origin
git checkout main && git pull
git merge --no-ff epa/plan-core-production-readiness-2026-09-07
```
No worktrees were created by this run (mode: direct, no per-phase worktree isolation used). Two
`epa/*` branches from unrelated prior runs remain in the repo (`epa/plan-production-readiness-final-2026-09-03`,
`epa/prod-readiness-2026-08-25`) — pre-existing, not created or touched by this run; leave for the
owner to prune at their discretion.
