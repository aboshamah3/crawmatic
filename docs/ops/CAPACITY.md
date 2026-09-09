# Worker capacity: RAM budget and DB connection demand

Created by EPA B4 (F09, "Two Celery consumer pools; time limits; resumable
long maintenance"). Two sections: the worker service's own RAM budget
under the two-consumer-pool split (`apps/workers/start.sh`), and the
DB-connection-demand formula F20 (release certification) is responsible
for pinning to a concrete, verified number.

## Worker RAM budget: `2 pools × concurrency × ~150 MB`

`apps/workers/start.sh` runs two independent `celery worker` processes in
the one container:

| Pool          | Queues                                                  | Concurrency setting             | Default |
|---------------|----------------------------------------------------------|----------------------------------|---------|
| `critical@%h` | `scrape_dispatch`, `maintenance`                          | `CELERY_CRITICAL_CONCURRENCY`    | 2       |
| `bulk@%h`     | `price_analysis`, `strategy_discovery`, `webhook_events`  | `CELERY_BULK_CONCURRENCY`        | 2       |

`~150 MB` is the steady-state resident-set estimate for one prefork child
running this task mix (short DB/broker calls, occasional `STRATEGY_
DISCOVERY_RUN`/`recompute_variant` work — no scrapy/twisted/playwright in
this process at all, Constitution V) — the same order of magnitude the
existing `CELERY_MAX_MEMORY_PER_CHILD_KB` default (300,000 KB ≈ 293 MB)
budgets as its *ceiling* before a child is recycled
(`libs/shared/app_shared/config.py`); ~150 MB is the typical, not the cap.

At the defaults above:

```
worker RAM ≈ (CELERY_CRITICAL_CONCURRENCY + CELERY_BULK_CONCURRENCY) × ~150 MB
           ≈ (2 + 2) × 150 MB
           ≈ 600 MB steady-state prefork-child RAM
```

Plus each pool's own parent/beat-less supervisor process overhead
(small, tens of MB per `celery worker` parent — two parents instead of
one is the fixed cost of the split) and whatever the container's base
Python/uv environment holds resident regardless of task load. Raise
`CELERY_CRITICAL_CONCURRENCY`/`CELERY_BULK_CONCURRENCY` (env-tunable, no
rebuild) if a queue's backlog grows under load — re-run this formula
against the container's actual memory limit (Railway: `WATCHDOG_MEMORY_
LIMIT_MB`, see `app_shared.memory_watchdog`) before raising either past
the point this budget clears it.

## DB connection demand (F20 formula)

> `api threads + worker procs + scheduler + scrapers ≤ PgBouncer pool`

Every process that imports `app_shared.database` and calls `get_session()`
builds its **own** SQLAlchemy engine sized `DB_POOL_SIZE` (8) +
`DB_MAX_OVERFLOW` (4) = **12 connections it can hold toward PgBouncer at
once** — and, critically, this is per **process**, not per service or per
consumer pool: `apps/workers/celery_app.py`'s `worker_process_init` hook
(`_dispose_inherited_engine`) deliberately disposes any engine inherited
from `fork()`, so each Celery **prefork child** builds its own fresh
engine on first DB use rather than sharing one pool with its siblings.
The same is true of each Scrapyd spider process (`app_shared.database` is
shared code) and of the scheduler's single long-lived process.

Counting actual DB-connection-capable processes under this change:

| Actor                                   | Processes                                                      | Max conns each | Max conns total |
|------------------------------------------|-----------------------------------------------------------------|-----------------|-------------------|
| API                                      | `API_THREAD_POOL_SIZE` (8) threads share ONE process's engine    | 12              | 12                |
| Worker — `critical@%h`                   | `CELERY_CRITICAL_CONCURRENCY` prefork children (default 2)       | 12 each         | 24                |
| Worker — `bulk@%h`                       | `CELERY_BULK_CONCURRENCY` prefork children (default 2)           | 12 each         | 24                |
| Scheduler                                | 1 long-lived process                                             | 12              | 12                |
| Scrapyd spiders                          | one process per concurrently-running spider run                 | 12 each         | (unbounded today) |

`api threads` in the F20 formula collapses to one row above because
`ANYIO`'s thread pool shares its parent process's single engine/pool —
it is the reason `API_THREAD_POOL_SIZE` is kept `<= DB_POOL_SIZE +
DB_MAX_OVERFLOW` in the first place (see that setting's own comment).
`worker procs` is **two rows**, not one, specifically because of the
two-consumer-pool split this task introduces: raising `CELERY_CRITICAL_
CONCURRENCY`/`CELERY_BULK_CONCURRENCY` (worker RAM budget, above) also
raises this table's worker rows 1:1 — the two budgets are not
independent.

**What this task does NOT do**: pin PgBouncer's own `default_pool_size`
(the real Postgres backend connections it maintains) to a concrete
number, or verify the sum above against it on the actual Railway
deployment — that reconciliation, plus a cap on concurrently-running
Scrapyd spider processes (today's genuinely open variable in the
"scrapers" term), is F20 (release certification)'s job, per the plan's
task dependency (`F09 maintenance capacity | B4 | F20 release
certification`). This section exists so F20 has the per-actor connection
math already worked out rather than starting from zero.

<!-- EPA B6: node-addressing decision section starts here. Leave this
     delimiter in place -- B6 extends this file with its own section
     below, in a later wave. Do not pre-write that content here. -->

## Scrapyd node addressing: distinct services, never replicas (EPA B6, F11)

Added by EPA B6 ("Capacity-aware placement persisted on the intent;
bounded node queues"). It records the decision that makes a *pool* of
Scrapyd nodes addressable at all, because capacity-aware placement is
meaningless without it.

**The decision: each Scrapyd node is its own Railway service.** The
browser pool is `scrapers-browser-1`, `scrapers-browser-2`, ...
`scrapers-browser-n` — `n` separate services from the same image, each
with its own Railway **private hostname**, all `n` listed in
`SCRAPYD_BROWSER_URLS` (comma-separated, the same convention
`SCRAPYD_HTTP_URLS` uses). The HTTP pool scales the same way when it
needs to.

**Why not Railway replicas of one service.** Replicas are the obvious
answer and they are the wrong one here. Every replica of a Railway
service answers on the **same** private hostname; the platform load
balances between them and does not expose a per-replica address. Scrapyd
is not stateless behind that hostname:

* A job id exists on exactly ONE replica. `schedule.json` lands wherever
  the balancer sent it, and `listjobs.json`/`cancel.json` for that id
  will as likely be answered by a replica that has never heard of it.
* EPA B2/F06's whole recovery protocol is "ask the node the intent
  recorded whether `scrapyd_job_id` exists there"
  (`dispatch_intents.reconcile_inflight_intents` ->
  `ScrapydDispatchClient.list_jobs`). Under replicas, `intent.node_url`
  names a *hostname*, not a *node* — "not found" would mean "you asked a
  different replica", and "not found" is the one verdict that authorizes
  a re-POST. Replicas therefore turn a crash-recovery re-POST into a
  duplicate paid scrape.
* `daemonstatus.json` has the same problem in the other direction: a
  probe answered by the idle replica would authorize placing work on the
  busy one. There is no reconciliation available: nothing in a Scrapyd
  response identifies which replica produced it.

Distinct services cost nothing extra (the same total container count)
and make both properties trivially true: one hostname, one Scrapyd, one
job table, one queue depth.

**What the placement rule does with the pool.**

| Pool size | Placement | Probe |
|-----------|-----------|-------|
| 1 node    | `jobs.nodes.select_node` (deterministic hash — unchanged) | none |
| 2+ nodes  | `jobs.nodes.choose_node`: least `running + pending`, skipping any node at `SCRAPYD_MAX_PENDING_PER_NODE` (default 4) or unreachable; domain-affinity hash breaks ties | `daemonstatus.json` per node, cached in Redis 10 s (60 s when the node did not answer) |

* **Placement is decided once and persisted.** `dispatch_intents.node_url`
  (EPA B2) is written in the same transaction that plans the batch;
  every later attempt at that identity reads it back
  (`DispatchIntentStore.planned_node_url`) instead of re-choosing. A
  retry that moved a batch would be a run `list_jobs` cannot find.
* **A saturated pool DEFERS.** When `choose_node` returns `None` —
  every node unreachable or at `SCRAPYD_MAX_PENDING_PER_NODE` — the
  batch is not authorized, not planned, not POSTed and not stamped. Its
  targets stay `PENDING`/unclaimed and `redispatch_pending_jobs` offers
  them again. POSTing onto a full node cannot make the work run sooner:
  the browser nodes are `max_proc = 1`
  (`apps/scrapers-browser/scrapyd.conf`), so anything past the one
  running spider is pure queue depth, and a queued batch holds a
  cost-authorization grant and a `claimed_at` phase clock open while it
  waits.
* **`max_proc` stays 1 on the browser nodes** and 8 on the HTTP nodes
  (`apps/scrapers/scrapyd.conf`). `SCRAPYD_MAX_PENDING_PER_NODE` bounds
  the *queue*, not the concurrency; the two are independent knobs and B6
  changes neither `scrapyd.conf`.

**Adding a browser node** is therefore: deploy `scrapers-browser-<n+1>`
from the same image and `scrapyd.conf`, then append its private
hostname to `SCRAPYD_BROWSER_URLS` on the worker service. No worker
restart ordering matters — placement re-reads the pool from settings on
every pass, and a pool resize does not change any batch's dispatch
identity (`_batch_identity` keys on the `{project}:{spider}` pool, never
on the node URL), so work already POSTed is unaffected.
