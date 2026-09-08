# Phase B Review: Stage B — Durable work, reliable schedules
Verdict: FAIL
Base SHA: 8960632   Reviewed: main tree (uncommitted working tree; `.epa/` commits 5c4403a, 053ffe0 are run bookkeeping only)

## Integration command

Unit gate (plan Global Constraints), run as `mahmoud`, split into three disjoint chunks because a
single run exceeds the 600 s foreground cap. The three chunks are exhaustive over `tests/unit`
(303 `test_*.py` files + 7 package dirs) and reconcile exactly with B2-fix1's reported totals
(4426 passed / 19 skipped / 6 deselected).

`sudo -u mahmoud .venv/bin/pytest $(ls tests/unit/test_*.py | head -152) -q -p no:cacheprovider -m "not integration"` → exit 0
`sudo -u mahmoud .venv/bin/pytest $(ls tests/unit/test_*.py | tail -151) -q -p no:cacheprovider -m "not integration"` → exit 0
`sudo -u mahmoud .venv/bin/pytest tests/unit/{observations,models,scrapyd,netledger,strategy,domains,jobs} -q -p no:cacheprovider -m "not integration"` → exit 0

```
1889 passed, 1 skipped, 18 warnings in 365.38s (0:06:05)
2261 passed, 2 deselected, 53 warnings in 293.04s (0:04:53)
276 passed, 18 skipped, 4 deselected, 3 warnings in 5.12s
=> 4426 passed, 19 skipped, 6 deselected — no failures, no regression
```

Additional commands run by this review:

1. `sudo -u mahmoud bash scripts/check_single_head.sh` → exit 0 — `e7b34c0af219 (head)`; chain is
   linear `b6f1c40a97d2 → a4e91c7d2b58 → b6e5d1c94a72 → 096ff6d343b5 → d3f7a1b62c85 → e7b34c0af219`.
2. **Every Stage B migration executed for real** (closes B1 blocker 4 and B2's "rendered offline
   only"): a throwaway compose project (`COMPOSE_PROJECT_NAME=epab1review`, postgres 17.5 + redis
   on random loopback ports 35817/35818, own volumes, torn down with `down -v` afterwards) was
   migrated from an empty database:
   `sudo -u mahmoud env MIGRATION_DATABASE_URL=<throwaway> .venv/bin/alembic upgrade head` → exit 0,
   `... d3f7a1b62c85 -> e7b34c0af219, domain rules fleet limits` as the final step.
3. **B1's deferred integration replay test WAS RUN and PASSES** (BLOCKERS entry 2026-09-07 21:30
   is now closed):
   `pytest tests/integration/test_persistence_replay_no_refetch.py -q -m integration` → `1 passed, 1 error`
   — the assertion body passes (spool holds the batch through a `docker pause`, `replay_pending()`
   persists exactly one `request_attempts` and one `price_observations` row per `attempt_uuid`, a
   second replay writes nothing, the fetcher is called exactly twice). Reproduced twice. The `error`
   is fixture **teardown**, not the test: `cleanup_seeded_workspace` cannot `DELETE FROM workspaces`
   because `outbox_messages` rows written by the pipeline reference it. Pre-existing shared-helper
   gap, not B1's: `tests/integration/test_spider_jsonld_fixture_live.py` (untouched by this phase)
   errors the same way in the same environment, and B1 added no outbox write to `pipelines.py`
   (`git diff 8960632 -- pipelines.py | grep write_outbox_message` is empty). Follow-up below.
4. Stage-B-scoped integration files (`test_dispatch_reconcile_fake_scrapyd`, `test_dispatch_replay_matrix`,
   `test_dispatch_stamping`, `test_two_schedulers_one_occurrence`, `test_fair_scheduling`,
   `test_persistence_replay_no_refetch`) → exit 0, `21 passed, 11 skipped` (the skips are the
   self-provisioning scratch-container tests, which need a docker-group session).
5. Secret scan over the whole phase diff + every untracked file: no credential-shaped literals
   except two clearly-labelled test fixture strings (`"occurrence-scratch"` for a throwaway
   container, `"ops-service-token-for-tests"`). Nothing redacted from this review.

## Per-task

| Task | Criteria met | Evidence quality | Notes |
|---|---|---|---|
| B1 spool/idempotency (F05) | yes | strong — deferred replay test now executed and green (see above); migration applied for real | `resolve()` in the pipeline callback rather than inside `_flush_batch` is required by the plan's own test; backpressure Deferreds fire on flush *completion* (documented, prevents a permanent wedge during an outage). `crawmatic_persistence_quarantined_batches` is a module counter + Scrapy stat + log line, not an ops-metrics gauge — consistent with B9's snapshot gap. |
| B2 commit-before-send + outbox (F06) | yes | strong — 13 new unit tests, offline render, and migration now applied to a live DB | No `kind` column on `outbox_messages`; expressed as `task_name=SCRAPE_DISPATCH_JOB` + `dedup_key="job_created:<id>"` — same observable behaviour, no routing table. `grep "\.delay("` over apps/libs is empty. Backfill now rides the `ALTER … USING` rewrite (B2-fix1) — verified RLS-proof by rehearsal. |
| B2-fix1 (RLS backfill + grants) | yes | strong — unmodified `rehearse_upgrade.sh` on the same dump, 10/10 green, `verify_grants` 0 MISSING | Migration amended in place, head unchanged. `provision_db_roles.sql` arrays now match `grants_expected.yaml` group-for-group (checked by hand against each `GRANT` in the loop) and a new unit test makes them derived. |
| B3 due-time claim + fair default (F07) | yes | strong | `next_run_at <= now` inside the `FOR UPDATE SKIP LOCKED` select, occurrence INSERT before `create_scope_job`, `IntegrityError` → rollback → False, occurrence kept when the scope resolves to zero matches. `SCHEDULER_FAIR_QUEUE_ENABLED` default flipped to `True` — FLEET-WIDE behaviour change, deliberate, deferred item closed in both docs. `refresh_rule_occurrences` as a Core `Table` (not `Base`) to keep the plan's composite PK is defensible. |
| B4 two pools + time limits (F09) | yes | strong | `start.sh` renders exactly the two required commands, `wait -n` + kill + always non-zero; mode 0755 and `COPY . .` + `chown -R app:app /app` make `CMD ["/app/start.sh"]` correct. Pre-existing routing gap (4 tasks on the unconsumed default queue) correctly flagged, not silently fixed. `STRATEGY_DISCOVERY_SCAN` has no first trigger — follow-up. |
| B5 fleet admission (F10) | yes | strong — 35 new tests | Fleet gate runs last, releases the tenant slot on refusal, `denied_by="fleet"` → `FLEET_LIMITED` on the deferred target; both spiders thread `fleet_key`/`fleet_token` on `meta` and release on the `parse` **and** `errback` paths, symmetric with the existing tenant slot. `domain_rules` filed SYSTEM, SELECT-only for all three roles. |
| B6 placement + bounded queues (F11) | yes (one deviation) | strong — 35 new tests | `unreachable_pool_fallback=True` on the stall-recovery path only is a deviation from the literal "unreachable == saturated → defer", but the literal reading disables the reaper exactly when it is needed (it broke 9 F-2 tests). A reachable-but-full pool still defers there. Owner-visible, reversible by one keyword. |
| B7 async middleware + deadlines (F15) | partly — `abuse_limit` untouched (ASSUMPTIONS) | strong for what shipped | `rate_limit.py` holds only `redis.asyncio`, one Lua script, `asyncio.wait_for` 300 ms fail-open, bounded IP-key cardinality. `abuse_limit.py` keeps its Postgres-authoritative fail-closed design (recorded in ASSUMPTIONS; converting it would be a security regression). Five new settings read via `os.environ` with `CONFIG NOTE` markers — authorised for this run, consolidation owed. |
| B8 readiness split (F16) | yes | strong | One module-level `ThreadPoolExecutor(max_workers=2)` + non-blocking `_generation_lock`, `stale=True` for losers, each probe its own session; `/live`, `/health/scraping` mounted, `/health/scraping` never gates `/ready`; heartbeats stay on `/ready`. |
| B9 heartbeats + alert rules (F22) | **NO — worker per-pool heartbeats not wired** | rules logic strong (30 tests); emission incomplete | Scrapyd nodes now beat (`heartbeat:scraper:<node>`, 30 s, both node types). Nothing anywhere in `apps/`+`libs/` emits a `worker` (or `scheduler`) heartbeat — see Findings. 8 of 9 new rules read gauges via `getattr` and are inert until `snapshot.py` wiring: pre-accepted by the orchestrator as a follow-up, not counted against this verdict. `GET /admin/alerts/active` exists, service-token guarded, out of the public schema. |
| B10 release 2 doc | yes | strong | Doc is consistent with the fixed sources: both findings marked FIXED AT SOURCE, the former mandatory pre/post-migrate SQL demoted to read-only VERIFICATION queries, §2.7 step 0 removed, and a 10/10-green re-rehearsal section added. One line is now wrong — see Finding 2. |

## Findings (ordered by severity)

1. **B9 acceptance criterion unmet — no worker heartbeat emitter.** `apps/workers/app/workers/celery_app.py`
   contains no heartbeat wiring (`grep -n "heartbeat" apps/workers/app/workers/celery_app.py` →
   nothing), and `grep -rn "app_shared.heartbeat" --include=*.py apps libs` (excluding tests)
   matches only the two `scrapyd_app.py` files and `apps/api/app/routers/ready.py`. The packet's
   criterion is "Scrapyd nodes emit `heartbeat:scraper:<node>` every 30 s from `scrapyd_app.py`;
   **workers emit per pool (`critical@`, `bulk@`) matching B4's node names**". B9's report defers
   this because `apps/workers/start.sh` did not exist yet; B4 landed later **in this same phase**,
   so the stated blocker is gone and nothing closed the loop (contrast B3, which did close B2's
   owed `reconcile_inflight_intents` wiring). Fix: in `celery_app.py`, start a
   `app_shared.heartbeat.PeriodicHeartbeat(HeartbeatEmitter(get_redis_client(), service="worker",
   instance_id=sender.hostname))` on Celery's `worker_ready` signal and `stop()` it on
   `worker_shutdown` (`sender.hostname` is `critical@<host>` / `bulk@<host>`, i.e. B4's node names),
   best-effort like `_start_scraper_heartbeat`; add a unit test in the shape of
   `tests/unit/test_heartbeat_periodic.py` asserting one emitter per pool name and that a Redis
   failure does not stop the worker.
2. **`docs/ops/RELEASE_2_2026-09.md:462` states a post-deploy check that cannot pass.** The line
   reads `# must be 200; heartbeats present for READY_REQUIRED_HEARTBEAT_SERVICES=scheduler,worker (A10)`.
   `apps/api/app/routers/ready.py:_probe_heartbeats` → `heartbeat_mod.check_required_heartbeats`
   returns `ok=False` for a declared service with no live beat, and `/ready` is 503 when any check
   fails. With Finding 1 fixed the `worker` half becomes true, but **nothing emits a `scheduler`
   heartbeat either**, so setting that variable (which `docs/ops/RELEASE_1_2026-09.md:253` instructs
   as a Release 1 variable) would 503 the API service for good. Fix, one of: (a) also start a
   `PeriodicHeartbeat(service="scheduler")` in `apps/scheduler/app/scheduler/scheduler_app.py`'s
   main loop — the same ~10 lines, and it is what F22 "every process class" means; or (b) if the
   scheduler beat is deliberately out of Stage B scope, change RELEASE_2 §post-deploy to say the
   variable stays **unset** until F1 lands and record it as an owner item, so the release does not
   ship a gate that fails by construction. Either way RELEASE_1's variable table needs the same
   note (out of this phase's scope to edit — flag it to the owner).

Everything else in the phase is mergeable as it stands: the unit gate is green with no
phase-attributable failures, the Alembic chain is single-headed at the required `e7b34c0af219` and
now proven to apply from an empty database, the two release-blocking bugs B10 found are fixed at
source and re-rehearsed green on the same dump, and the one integration test the run had deferred
passes.

## Files to commit (108 paths — valid once the findings above are fixed; the fix worker will add
`apps/workers/app/workers/celery_app.py` (already listed), `docs/ops/RELEASE_2_2026-09.md`
(already listed), possibly `apps/scheduler/app/scheduler/scheduler_app.py` (already listed) and one
new test file)

alembic/versions/096ff6d343b5_strategy_discovery_state.py
alembic/versions/a4e91c7d2b58_observation_attempt_identity.py
alembic/versions/b6e5d1c94a72_dispatch_intent_node_and_state.py
alembic/versions/d3f7a1b62c85_refresh_rule_occurrences.py
alembic/versions/e7b34c0af219_domain_rules_fleet_limits.py
apps/api/app/main.py
apps/api/app/rate_limit.py
apps/api/app/routers/admin_ops.py
apps/api/app/routers/health.py
apps/api/app/routers/ready.py
apps/scheduler/app/scheduler/refresh.py
apps/scheduler/app/scheduler/scheduler_app.py
apps/scrapers-browser/price_monitor_browser/scrapyd_app.py
apps/scrapers-browser/price_monitor_browser/settings.py
apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py
apps/scrapers/price_monitor/scrapyd_app.py
apps/scrapers/price_monitor/settings.py
apps/scrapers/price_monitor/spiders/generic_price_spider.py
apps/workers/Dockerfile
apps/workers/app/workers/celery_app.py
apps/workers/app/workers/tasks_jobs.py
apps/workers/app/workers/tasks_strategy.py
apps/workers/start.sh
docs/DEFERRED-ITEMS.md
docs/PRODUCTION_READINESS_SCORE_2026-09.md
docs/ops/CAPACITY.md
docs/ops/RELEASE_2_2026-09.md
libs/scrape-core/scrape_core/limiter.py
libs/scrape-core/scrape_core/pipelines.py
libs/scrape-core/scrape_core/result_spool.py
libs/scrape-core/scrape_core/targets.py
libs/shared/app_shared/config.py
libs/shared/app_shared/costauth/service.py
libs/shared/app_shared/database.py
libs/shared/app_shared/enums.py
libs/shared/app_shared/heartbeat.py
libs/shared/app_shared/jobs/dispatch_intents.py
libs/shared/app_shared/jobs/node_load.py
libs/shared/app_shared/jobs/nodes.py
libs/shared/app_shared/jobs/service.py
libs/shared/app_shared/limiter/__init__.py
libs/shared/app_shared/limiter/fleet.py
libs/shared/app_shared/limiter/keys.py
libs/shared/app_shared/models/__init__.py
libs/shared/app_shared/models/dispatch.py
libs/shared/app_shared/models/domain_rules.py
libs/shared/app_shared/models/maintenance_cadence.py
libs/shared/app_shared/models/observations.py
libs/shared/app_shared/models/refresh_rule_occurrences.py
libs/shared/app_shared/models/strategy_discovery_state.py
libs/shared/app_shared/opsmetrics/rules.py
libs/shared/app_shared/outbox/dispatcher.py
libs/shared/app_shared/redis_client.py
libs/shared/app_shared/scrapyd/client.py
libs/shared/app_shared/scrapyd/reconcile.py
libs/shared/app_shared/task_names.py
pyproject.toml
scripts/provision_db_roles.sql
scripts/rls_table_manifest.txt
scripts/sql/grants_expected.yaml
scripts/write_release_manifest.py
tests/integration/test_dispatch_reconcile_fake_scrapyd.py
tests/integration/test_dispatch_replay_matrix.py
tests/integration/test_persistence_replay_no_refetch.py
tests/integration/test_two_schedulers_one_occurrence.py
tests/load/test_api_with_slow_dependencies.py
tests/unit/_jobs_fake_session.py
tests/unit/jobs/test_cancellation.py
tests/unit/test_admin_ops_endpoint.py
tests/unit/test_celery_time_limits.py
tests/unit/test_choose_node.py
tests/unit/test_create_scope_job.py
tests/unit/test_db_statement_timeout.py
tests/unit/test_discovery_chunk_cursor.py
tests/unit/test_dispatch_intent_state_machine.py
tests/unit/test_dispatch_kill_after_post.py
tests/unit/test_dispatch_reconcile_schedule.py
tests/unit/test_engine_hygiene.py
tests/unit/test_fire_refresh_rule_predicate.py
tests/unit/test_fleet_admission.py
tests/unit/test_grants_manifest_schema.py
tests/unit/test_health_scraping_signal.py
tests/unit/test_heartbeat_periodic.py
tests/unit/test_jobs_dispatch_task.py
tests/unit/test_jobs_router.py
tests/unit/test_jobs_service.py
tests/unit/test_limiter_middleware_fleet.py
tests/unit/test_list_pagination_contract.py
tests/unit/test_migration_offline_dispatch_intent_backfill.py
tests/unit/test_node_load_cache.py
tests/unit/test_observability_logs.py
tests/unit/test_ops_rules_new.py
tests/unit/test_persistence_batching.py
tests/unit/test_pipeline_flush_retry.py
tests/unit/test_pipeline_target_terminalization.py
tests/unit/test_rate_limit_async.py
tests/unit/test_rate_limit_middleware.py
tests/unit/test_ready_endpoint.py
tests/unit/test_ready_no_overlap.py
tests/unit/test_redis_client_timeouts.py
tests/unit/test_redispatch_interleave.py
tests/unit/test_refresh_pass_isolation.py
tests/unit/test_release_identity.py
tests/unit/test_result_spool.py
tests/unit/test_variants_rescrape_route.py
tests/unit/test_w4_flag_defaults.py
tests/unit/test_worker_start_script.py
tests/unit/test_write_release_manifest.py

## Follow-ups (non-blocking)

- **B9's 8 inert rules** — `freshness_fraction_24h`, `persistence_pending/quarantined_batches`,
  `costauth_denials_1h`, `disk_free_fraction`, `restore_verify_failed`,
  `ledger_linked_attempt_fraction_24h`, `dispatch_ambiguous_intents`, `heartbeat_missing` read via
  `getattr(snapshot, …, None)` and fire nothing until `opsmetrics/snapshot.py` collects them.
  Pre-accepted by the orchestrator; schedule it before D5's scorecard depends on it.
- **`cleanup_seeded_workspace` cannot delete a workspace that has `outbox_messages` rows**
  (`tests/integration/_scrapyd_spider_live_support.py:504`). Pre-existing and shared by several
  live tests; add `DELETE FROM outbox_messages WHERE workspace_id = :ws` before the products delete,
  otherwise B1's new replay test reports a teardown ERROR on every host where it actually runs.
- **`STRATEGY_DISCOVERY_SCAN` has no first trigger** (B4's own note): the chunking, cursor and
  self-re-enqueue are built and tested, but no beat entry or cadence starts pass #1.
- **Four tasks still route to the unconsumed default queue** (`COSTAUTH_RESERVATION_SWEEP`,
  `MAINTENANCE_RECONCILE_PROVIDER_USAGE`, `MAINTENANCE_COST_ROLLUP`,
  `MAINTENANCE_ENTITLEMENT_REFRESH`) — pre-existing, cost/entitlement-adjacent, correctly left
  alone by B4 but still never executed by either pool.
- **`dispatch_job` releases the cost grant on any exception around `client.schedule`**
  (`apps/workers/app/workers/tasks_jobs.py` ~`:983`, comment "Failure BEFORE dispatch"). After B2
  the POST may in fact have been accepted and lost only its response; the intent then reconciles to
  CONFIRMED and runs while its budget hold has been returned. Under-reservation, not double-spend —
  worth a C-stage look alongside the reservation sweep.
- **B7's `os.environ` settings** (`REDIS_SOCKET_TIMEOUT_SECONDS`, `REDIS_CONNECT_TIMEOUT_SECONDS`,
  `DB_STATEMENT_TIMEOUT_MS`, `DB_POOL_ACQUIRE_TIMEOUT_SECONDS`, `API_RATE_LIMIT_MAX_KEYS`) should
  become typed `Settings` fields now that `config.py` is no longer held by a parallel worker;
  `get_auth_engine()`/`get_system_engine()` still carry no statement timeout.
- **`tests/integration/test_fair_scheduling.py` pins host port 55498**, inside this host's ephemeral
  range, so it silently self-skips after any run that leaves that port in `TIME_WAIT` (B3's finding).
- **`mahmoud` is not in the `docker` group**, so every docker-backed integration test skips under the
  packets' own commands; this review ran those as root with `PYTHONDONTWRITEBYTECODE=1`.
- **Host disk**: running the FULL `tests/integration` suite in this environment triggers an image
  build and drove `/` to 100 % (43 MB free). This review reclaimed it with `docker buildx prune -f`
  (build cache only — no volume, image or container was deleted); `/` is back to 2.3 GB free / 97 %.
  Do not run the whole integration directory on this host until 0.1 step 6 disk relief lands.
