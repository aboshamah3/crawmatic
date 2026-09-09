# Phase B Review (pass 2): Stage B — Durable work, reliable schedules
Verdict: PASS
Base SHA: 8960632   Reviewed: main tree (uncommitted working tree; `.epa/` commits 5c4403a, 053ffe0 are run bookkeeping only)

Scope of this pass: verify that the two FAIL findings of `phases/phase-B-review.md` are closed by
`reports/B9-fix1.md`, that the fix introduced no regression elsewhere in phase B, and re-run the unit
gate. The first pass's full investigation (per-task criteria, migrations applied from an empty DB,
B1's replay test, Stage-B integration files, secret scan of the whole diff) is NOT redone — those
verdicts stand. The full integration suite was deliberately NOT run (host disk: 2.3 GB free / 97 %).

## Integration command

Unit gate (plan Global Constraints), run as `mahmoud`, three disjoint foreground chunks. `tests/unit`
is now **304** `test_*.py` files (B9-fix1 added the 304th), so `head -152` + `tail -152` is exactly
exhaustive — the first pass's `tail -151` would now skip one file. Plus the 7 package dirs
(`domains jobs models netledger observations scrapyd strategy`), which is all of them.

`sudo -u mahmoud .venv/bin/pytest $(ls tests/unit/test_*.py | head -152) -q -p no:cacheprovider -m "not integration"` → exit 0
`sudo -u mahmoud .venv/bin/pytest $(ls tests/unit/test_*.py | tail -152) -q -p no:cacheprovider -m "not integration"` → exit 0
`sudo -u mahmoud .venv/bin/pytest tests/unit/{observations,models,scrapyd,netledger,strategy,domains,jobs} -q -p no:cacheprovider -m "not integration"` → exit 0

```
1889 passed, 1 skipped, 18 warnings in 363.75s (0:06:03)
2269 passed, 2 deselected, 53 warnings in 302.93s (0:05:02)
276 passed, 18 skipped, 4 deselected, 3 warnings in 4.94s
=> 4434 passed, 19 skipped, 6 deselected — exit 0 on every chunk
```

That is the first pass's 4426 plus **exactly** the 8 tests B9-fix1 added
(`tests/unit/test_process_heartbeats.py`); no pre-existing test changed state, so the fix caused no
regression anywhere in the phase. B9-fix1's reported totals reconcile digit-for-digit with this run.

Also re-run by this pass:

- `sudo -u mahmoud bash scripts/check_single_head.sh` → exit 0 — `check_single_head: OK — exactly 1 head.` / `e7b34c0af219 (head)`, the required revision.
- Working-tree path set recomputed from `git status --porcelain` and diffed against the first pass's
  108-path list: the **only** difference is the added `tests/unit/test_process_heartbeats.py`. The fix
  touched nothing outside the four files it declared, and no file was left behind or dropped.
- Secret scan of the five fix-touched files: only `tests/unit/test_process_heartbeats.py`'s env
  scaffold, whose `SCRAPYD_PASSWORD="change-me"`, `JWT_SECRET="test-jwt-secret"` and `ENCRYPTION_KEYS`
  Fernet literal are the repo's long-standing shared test placeholders (the same `ENCRYPTION_KEYS`
  string appears in ~10 pre-existing unit tests, e.g. `tests/unit/test_config.py`). Nothing new,
  nothing redacted from this review.

## Finding closure

**Finding 1 — worker per-pool heartbeat: CLOSED.**
`apps/workers/app/workers/celery_app.py:417` `@worker_ready.connect def _start_worker_pool_heartbeat`
builds `PeriodicHeartbeat(HeartbeatEmitter(get_redis_client(), service="worker", instance_id=...))`
and `.start()`s it; `:455` `@worker_shutdown.connect def _stop_worker_pool_heartbeat` stops it and
clears the module-level `_worker_heartbeat` (`:83`).
- *`worker_ready` not `worker_init`* — correct, and the rationale in the docstring is the real one:
  `worker_init` fires before the prefork pool exists, so the daemon thread would be inherited by
  every forked child (N processes beating under one instance id, each on a pre-fork Redis socket).
  `worker_ready` fires in the parent after the pool exists. Consistent with the neighbouring
  `worker_process_init` → `dispose_engine()` fork-hygiene handler in the same file.
- *Per-pool instance ids* — `instance_id = str(getattr(sender, "hostname", "") or "") or default_instance_id()`.
  Celery sends `worker_ready` with `sender` = the pool's own `Consumer`, whose `hostname` is exactly
  the `-n critical@%h` / `-n bulk@%h` node name `apps/workers/start.sh` (B4) sets, so the two pools in
  the one container take two distinct `heartbeat:worker:*` keys with independent fences — a wedged
  `bulk@` cannot be masked by a healthy `critical@`, which was the point of the criterion. The
  `getattr` fallback means a sender without `hostname` still yields a non-empty id rather than
  `heartbeat:worker:`.
- *Best-effort on a Redis outage* — the whole construction, including `HeartbeatEmitter.start()`'s
  fencing-token `INCR`, is inside the `try`; the `except` logs and leaves `_worker_heartbeat = None`.
  A Redis outage costs the beat, never the worker. Start is guarded against a re-delivered signal;
  stop is safe with nothing running.
- Module-scope `from app_shared.redis_client import get_redis_client` is fork-safe: that module has no
  import-time side effects (checked — only `import redis`, two `os.environ` reads and a `None`
  module global; the `maxmemory-policy` probe is inside `get_redis_client()`, first called on
  `worker_ready`, i.e. in the parent after the fork).

**Finding 2 — scheduler heartbeat + doc line: CLOSED (option (a), the preferred branch).**
`apps/scheduler/app/scheduler/scheduler_app.py:752` `_start_scheduler_heartbeat()` returns a started
`PeriodicHeartbeat(HeartbeatEmitter(..., service="scheduler", instance_id=default_instance_id()))`,
called at `:1515` in `main()` **after** the signal handlers and **before** the boot-time
`_run_durable_cadence_tick` / `_run_health_tick`, and stopped at `:1643` after the tick loop. Lazy
`get_redis_client` import and a `try/except` that returns `None` — same best-effort discipline, so
Redis being down at boot cannot stop the scheduler from running cadences. Ordering is the right
choice: a scheduler wedged in its first (slow, DB-bound) boot pass now reads as alive rather than as
one that never booted.
With both emitters live, `READY_REQUIRED_HEARTBEAT_SERVICES=scheduler,worker` is a check that can
pass, so the review's fallback (b) — and the flagged correction to RELEASE_1's variable table
(`docs/ops/RELEASE_1_2026-09.md:253`) — correctly does **not** apply. No owner item is owed.
`docs/ops/RELEASE_2_2026-09.md:462` is now true by construction: the step names both emitters, their
process classes, the per-pool key rule, the best-effort-at-boot caveat, and adds
`curl … /ready | jq '.checks.heartbeats'` to confirm the individual instances. `.checks.heartbeats` is
the real key (`apps/api/app/routers/ready.py:404`).
`libs/shared/app_shared/heartbeat.py:478` `PeriodicHeartbeat`'s docstring is a docstring-only edit that
replaces the stale "not yet wired into celery_app.py" paragraph with the three real call sites. All
three emitters use the same 30 s `HEARTBEAT_EMIT_INTERVAL_SECONDS` against a 120 s
`HEARTBEAT_TTL_SECONDS`, i.e. F22's "every 30 s" with a 4× fence.

**Test evidence — adequate.** `tests/unit/test_process_heartbeats.py` (8 tests, 339 lines) runs each
process class in a fresh subprocess (the `test_celery_time_limits.py` idiom, required because
`apps/api`, `apps/scheduler` and `apps/workers` each ship a top-level `app` package) and patches
`PeriodicHeartbeat` with a recorder rather than driving a real thread. It drives Celery's **real**
`worker_ready.send` / `worker_shutdown.send` dispatch, so a receiver that were defined but never
`.connect`ed would fail; it asserts two distinct keys for `critical@box-1` / `bulk@box-1`, start
idempotence, the raising-`get_redis_client` path for both classes, the empty-`hostname` fallback, and
`main()`'s ordering as an exact list `["heartbeat", "cadence", "health", "stop"]` rather than a mere
"was called". That is the test the first pass asked for; splitting it out from
`tests/unit/test_heartbeat_periodic.py` (pure timing of the mechanism) is a MINOR deviation and a
defensible one — it keeps a mechanism failure distinguishable from a wiring failure.

## Per-task

| Task | Criteria met | Evidence quality | Notes |
|---|---|---|---|
| B1 … B8, B10 | yes (as pass 1) | unchanged | Not re-investigated; pass 1's verdicts stand. None of these files was touched by the fix, and the unit gate shows no changed test state. |
| B9 heartbeats + alert rules (F22) | **yes — now closed** | strong: 8 new wiring tests through the real Celery signal dispatch and the real `main()`, plus the 30 pre-existing rules tests | Every long-lived process class emits: scrapyd nodes (`scraper`), both Celery pools (`worker`, per `-n` node name), scheduler (`scheduler`). The 8 inert `rules.py` gauges remain a pre-accepted follow-up, not counted against this verdict. |
| B9-fix1 (this cycle) | yes | strong — verbatim gate output reconciles exactly with this review's independent re-run | Files exactly as declared; nothing outside the two findings touched. |
| B10 release 2 doc | yes | strong | The one wrong line from pass 1 is fixed and now over-documents rather than under-documents. |

## Files to commit (109 paths)

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
tests/unit/test_process_heartbeats.py
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

(Identical to pass 1's 108 plus `tests/unit/test_process_heartbeats.py`; verified by diffing the
recomputed `git status --porcelain` set, minus `.epa/`, against that list. The four `.epa/` modified
files and the four untracked `.epa/` reports/review files are run bookkeeping, staged by the
orchestrator's own convention, not phase content.)

## Follow-ups (non-blocking)

New in this pass:

- **`apps/api/app/routers/ready.py:99-101` is now stale**: "no service publishes heartbeats until
  Task A5 Step 7's deploy wires `READY_REQUIRED_HEARTBEAT_SERVICES`". Three process classes publish
  as of this phase; only the *variable* is still unset. The `not-configured` behaviour it documents is
  unchanged and correct — it is the one sentence of justification that is out of date. Docstring only,
  no behaviour, so not a blocker; fold into the next touch of that file.
- **`PeriodicHeartbeat.stop()` does not clear `_thread`**, so a stopped instance cannot be restarted
  (`start()` returns early). Neither call site restarts, and both drop the handle, so this is latent
  only; worth one line if a third caller ever appears.
- **`heartbeat_missing{service}` (one of B9's 8 inert `rules.py` gauges) is the evaluation half of
  what this fix wired the emission half of.** Schedule the `opsmetrics/snapshot.py` collection work
  with that pairing in mind — the emitters now make that rule meaningful the moment the gauge lands.

Carried forward unchanged from `phases/phase-B-review.md` (all still open, none re-verified here):
the 8 inert `rules.py` gauges awaiting `opsmetrics/snapshot.py`; `cleanup_seeded_workspace` cannot
delete a workspace holding `outbox_messages` rows (`tests/integration/_scrapyd_spider_live_support.py:504`);
`STRATEGY_DISCOVERY_SCAN` has no first trigger; four tasks still route to the unconsumed default queue;
`dispatch_job` releases the cost grant on any exception around `client.schedule`; B7's five `os.environ`
settings owe promotion to typed `Settings` fields; `tests/integration/test_fair_scheduling.py` pins
host port 55498; `mahmoud` is not in the `docker` group; and the host disk (2.3 GB free / 97 %) still
forbids running the whole `tests/integration` directory here until 0.1 step 6 lands.
