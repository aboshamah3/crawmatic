# Task Report: B9-fix1 — Phase B gate fix cycle 1 (heartbeats for every process class, F22)
Status: DONE
Attempt: 1
Files changed:
- apps/workers/app/workers/celery_app.py — Finding 1: `_start_worker_pool_heartbeat` on Celery's `worker_ready` and `_stop_worker_pool_heartbeat` on `worker_shutdown`; module-level `_worker_heartbeat: PeriodicHeartbeat | None`; imports `worker_ready`/`worker_shutdown`, `HeartbeatEmitter`/`PeriodicHeartbeat`/`default_instance_id`, `get_redis_client`
- apps/scheduler/app/scheduler/scheduler_app.py — Finding 2: `_start_scheduler_heartbeat()` helper (`service="scheduler"`, `default_instance_id()`, lazy `get_redis_client` import, best-effort) started in `main()` before the boot-time cadence/health passes and stopped after the tick loop
- libs/shared/app_shared/heartbeat.py — `PeriodicHeartbeat` docstring: replaced the now-stale "not yet wired into celery_app.py" paragraph with the three real call sites (scrapyd nodes / worker pools / scheduler) and why that makes `READY_REQUIRED_HEARTBEAT_SERVICES=scheduler,worker` a check that can pass. No behaviour change.
- docs/ops/RELEASE_2_2026-09.md — the `/ready` post-deploy step (~`:462`) now records that both declared services genuinely emit, names the two emitters and their process classes, states the per-pool key rule, and adds `curl … /ready | jq '.checks.heartbeats'` to confirm the individual instances. The line is now true by construction rather than by intention.
- tests/unit/test_process_heartbeats.py (new) — 8 tests covering the wiring for both findings

## What was fixed

**Finding 1 — worker pool heartbeats.** `@worker_ready.connect` starts one
`PeriodicHeartbeat(HeartbeatEmitter(get_redis_client(), service="worker",
instance_id=sender.hostname))`. `sender` on `worker_ready` is the pool's own Celery
`Consumer`, whose `hostname` is exactly the `-n critical@%h` / `-n bulk@%h` node name
`apps/workers/start.sh` (B4) sets, so the two pools in the one container land on two
distinct `heartbeat:worker:*` keys with independent fences — a wedged `bulk@` cannot be
masked by a healthy `critical@`. `worker_shutdown` stops it.

`worker_ready` rather than `worker_init` (the reviewer's suggestion named `worker_ready`,
and it is also the correct one): `worker_init` fires **before** the prefork pool is
created, so a thread started there would be inherited by every forked child — N processes
beating under one instance id, each holding a copy of a pre-fork Redis connection.
`worker_ready` fires in the parent after the pool exists, so the thread is the parent's
alone, and it is also the point at which the process can genuinely consume, which is what
the beat asserts. Best-effort like the neighbouring `_start_memory_watchdog` and the
Scrapyd nodes' `_start_scraper_heartbeat`: a Redis outage costs the beat, never the worker.

**Finding 2 — scheduler heartbeat (option (a), the reviewer's preferred branch).** The
scheduler is the one process class with no port for anything to poll, so its silence is
otherwise invisible. `_start_scheduler_heartbeat()` is called in `main()` immediately after
the signal handlers and **before** `_run_durable_cadence_tick`/`_run_health_tick`, so a
scheduler wedged in its first boot pass still reads as alive rather than as one that never
booted; the daemon thread keeps beating across the loop's `time.sleep`s and slow DB passes.
Stopped after the loop so a deliberately drained scheduler stops claiming to be alive at
once instead of for one more TTL. Same best-effort discipline; `get_redis_client` is
imported lazily for the reason `_ops_snapshot_redis` documents (its first-use
`maxmemory-policy` probe raises on an eviction-capable server).

With both emitters live, `READY_REQUIRED_HEARTBEAT_SERVICES=scheduler,worker` is now a
real check rather than a permanent 503, so **RELEASE_1's variable table needs no
correction** — the reviewer's fallback note (option (b)) does not apply. Doc option (b) was
not taken; no owner item was created.

## Verification
`sudo -u mahmoud .venv/bin/pytest tests/unit/test_process_heartbeats.py tests/unit/test_heartbeat_periodic.py -q -p no:cacheprovider -m "not integration"` → exit 0
```
............                                                             [100%]
12 passed in 10.4s
```

Unit gate, three disjoint foreground chunks (304 `test_*.py` files + 7 package dirs;
`head -152` / `tail -152` now that this task added a 304th file — the reviewer's
`tail -151` would leave a gap):

`sudo -u mahmoud .venv/bin/pytest $(ls tests/unit/test_*.py | head -152) -q -p no:cacheprovider -m "not integration"` → exit 0
`sudo -u mahmoud .venv/bin/pytest $(ls tests/unit/test_*.py | tail -152) -q -p no:cacheprovider -m "not integration"` → exit 0
`sudo -u mahmoud .venv/bin/pytest tests/unit/{observations,models,scrapyd,netledger,strategy,domains,jobs} -q -p no:cacheprovider -m "not integration"` → exit 0
```
1889 passed, 1 skipped, 18 warnings in 367.56s (0:06:07)
2269 passed, 2 deselected, 53 warnings in 307.80s (0:05:07)
276 passed, 18 skipped, 4 deselected, 3 warnings in 4.80s
=> 4434 passed, 19 skipped, 6 deselected — no failures
```
That is the review's 4426 + exactly the 8 new tests; no pre-existing test changed state.

`sudo -u mahmoud bash scripts/check_single_head.sh` → exit 0
```
check_single_head: OK — exactly 1 head.
e7b34c0af219 (head)
```

Not run, deliberately: the full `tests/integration` suite (host disk, per the review's own
note). No docker-free heartbeat/ready integration test was added — the two seams under
test are process-lifecycle signals, and driving them through Celery's real
`worker_ready`/`worker_shutdown` dispatch and through the real `main()` in a subprocess (as
the new unit tests do) covers them without a container.

## What the new tests assert
`tests/unit/test_process_heartbeats.py`, subprocesses in the
`test_celery_time_limits.py` / `test_fire_refresh_rule_predicate.py` idiom (each of
`apps/api`, `apps/scheduler`, `apps/workers` ships its own top-level `app` package):
1. `worker_ready.send(...)` / `worker_shutdown.send(...)` — driven through Celery's real
   signal dispatch, so a receiver that is defined but never `.connect`ed fails here.
2. `critical@box-1` and `bulk@box-1` each produce one `service="worker"` emitter under
   their own node name, and `heartbeat_key` yields two distinct keys.
3. Start is idempotent; shutdown stops exactly once and is safe to call twice / with
   nothing running.
4. A raising `get_redis_client` does not propagate — no emitter, no crash, shutdown still
   copes (the "a Redis failure does not stop the worker" case the reviewer asked for).
5. A sender with no `hostname` still yields a non-empty instance id.
6. The scheduler helper builds a `service="scheduler"` emitter; a Redis failure returns
   `None` without raising.
7. `main()` calls the heartbeat starter **before** the cadence and health passes and stops
   it at exit (asserted as an ordered list, not just "it was called").

## Deviations
- New test file is `tests/unit/test_process_heartbeats.py` rather than extending
  `tests/unit/test_heartbeat_periodic.py`. That file is a pure-timing test of the
  `PeriodicHeartbeat` mechanism and needs no subprocess; the wiring tests need one per
  process class. Keeping them apart keeps a mechanism failure distinguishable from a wiring
  failure. The reviewer asked for a test "in the shape of" that file, which this is.
- `libs/shared/app_shared/heartbeat.py` was edited (docstring only, no code) — it is
  already in the review's files-to-commit list. Nothing else outside the two findings was
  touched.
Deviation impact: MINOR

## Blockers
none

## Escalation
none

## Notes for reviewer
- `celery_app.py` now imports `app_shared.redis_client` at module scope (mirroring
  `scrapyd_app.py`). That module has no import-time side effects — the
  `maxmemory-policy` probe happens inside `get_redis_client()` on first call, which now
  only ever happens on `worker_ready`, i.e. in the parent, after the fork. Full unit gate
  green confirms no import-boundary or fork-safety test regressed.
- Chunking note for the next gate run: `tests/unit` is now 304 `test_*.py` files, so
  `head -152` + `tail -152` is the exhaustive split; the review's `head -152`/`tail -151`
  would now skip one file.
- `docs/PRODUCTION_READINESS_SCORE_2026-09.md:80` (H1) reads "the `/ready` heartbeat
  machinery already exists; only `READY_REQUIRED_HEARTBEAT_SERVICES` is unset in prod".
  That claim was optimistic before this fix and is accurate now; left unedited.
- Still open from the review, untouched here (all explicitly non-blocking): the 8 inert
  `rules.py` gauges awaiting `opsmetrics/snapshot.py` wiring — note that the
  `heartbeat_missing{service}` rule among them is the *evaluation* side of what this task
  wired the *emission* side of, so those two now want scheduling together.
- Intended commit message for the phase commit:
  `fix(ops): emit worker-pool and scheduler heartbeats so READY_REQUIRED_HEARTBEAT_SERVICES can pass (F22)`
