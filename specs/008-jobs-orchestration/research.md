# Phase 0 Research: Jobs & Orchestration

All NEEDS CLARIFICATION resolved. Each decision cites the master doc (§), the spec Clarifications / autospec-decisions, and existing repo code being reused. No new third-party stack is introduced — everything is a build-out of the locked stack (FastAPI, Celery+Redis, SQLAlchemy+Alembic, Postgres, plus the SPEC-07 authenticated Scrapyd client) already present in SPEC-01→07.

---

## D1 — "Batch" is a derived grouping, not a third table

**Decision**: A dispatch batch is **not** a persisted row. It is a stable, derived grouping of a job's targets by `(competitor_domain, scrape_mode)` with a deterministic `batch_index` (the enumerated position of the group in a canonical sort of the groups). `plan_batches(targets, *, http_min=50, http_max=200)` (pure, `app_shared.jobs.batching`) yields `Batch(batch_index, mode, domain, match_ids)` where each `(domain, mode)` group is chunked into HTTP batches of 50–200. No per-batch column is added to either table; the only two tables are the §22-enumerated `scrape_jobs` + `scrape_job_targets`.

**Rationale**: §22 lists exactly two Jobs tables; the spec Key Entities note a batch "may be represented via target/job fields rather than a distinct table — an implementation choice for planning," and autospec decision #7 leaves it to planning with only the deterministic-node + idempotency guarantees binding. Deriving the batch keeps the schema at exactly the two doc-enumerated tables — the same discipline SPEC-07 used when it deliberately did **not** create these tables early (its research D9). Node choice, idempotency, and stall detection are all achievable without persisting the batch (D2/D3/D4 below).

**Alternatives rejected**: a `scrape_job_batches` table (persisted node + Scrapyd jobid + `dispatched_at`) — adds a table beyond §22's exact set for no capability that hash-by-domain + Redis `SET NX` + target-state don't already provide; it also duplicates the reused SPEC-07 idempotency key.

---

## D2 — Idempotency guard storage: reuse the SPEC-07 Redis `SET NX` key (not a DB unique row)

**Decision** (resolves the spec's deferred clarification): the dispatch idempotency guard lives in **Redis**, reusing the already-built `ScrapydDispatchClient` mechanism — `SET NX` on `dispatch_key(scrape_job_id, batch_index) = f"dispatched:{scrape_job_id}:{batch_index}"`, with the returned Scrapyd `jobid` persisted into the same key as the durable backstop (claim → commit → release ordering already implemented in `libs/shared/app_shared/scrapyd/client.py`). An at-least-once/retried delivery of the dispatch task therefore returns the persisted jobid as a no-op and never issues a second `schedule.json` for the same `(job, batch)`.

**Rationale**: Constitution Principle V names "Redis `SET NX` and/or persisted Scrapyd `jobid`" as the idempotent-dispatch mechanism, and SPEC-07 already ships exactly that client. Reusing it means the guard is decided once and shared, with no second implementation. A DB unique row would require the D1-rejected batch table.

**Alternatives rejected**: (a) a `unique(scrape_job_id, batch_index)` DB row as the guard — needs the batch table (D1) and a round-trip + integrity-error handling where a Redis `SET NX` short-circuits before the network call; (b) idempotency keyed only on `scrape_job_id` — too coarse (would block legitimate multi-batch dispatch of one job).

---

## D3 — Deterministic node selection: stateless hash-by-domain

**Decision**: `select_node(domain, nodes) -> node_url` (pure, `app_shared.jobs.nodes`) chooses a node from `Settings.SCRAPYD_HTTP_URLS` by a **stable hash of the domain** modulo the node count (a stable digest, e.g. `blake2b(domain)`, not Python's salted `hash()`). Two dispatch retries of the same batch (same domain+mode) therefore always resolve to the same node. The dispatch task passes the chosen node to the (D-extended) `ScrapydDispatchClient.schedule(..., node_url=...)`.

**Rationale**: §26 "Node handling" + FR-014 / US3-AS4 require deterministic selection so two retries can't split one batch across two nodes; a stateless domain hash needs zero persistence (supporting D1's no-batch-table decision). The client currently hardcodes `SCRAPYD_HTTP_URLS[0]`; a back-compat optional `node_url` arg (default = current behavior) is the minimal extension.

**Alternatives rejected**: round-robin with the chosen node **persisted on the batch** (the §26 alternative) — correct but requires the D1-rejected batch table to persist the node; hash-by-domain gives the same determinism statelessly. Python's builtin `hash()` — per-process salted (`PYTHONHASHSEED`), so not deterministic across worker processes; rejected in favor of a stable digest.

---

## D4 — Stall detection + timed, window-bucketed re-dispatch

**Decision**: `recover_stalled_batches` (a `maintenance`-queue Celery task) scans **non-terminal** jobs and finds targets still in a **non-progressed** state (`PENDING`, never moved to `STARTED`/terminal) whose age exceeds `SCRAPE_STALL_TIMEOUT_SECONDS` (default 900 s) relative to the job's `started_at`. It re-plans batches over **only** those still-unprogressed, un-locked targets and re-dispatches them. Re-dispatch idempotency uses a **stall-window-bucketed** batch key: `batch_index` is suffixed with `stall_window(now, timeout)` (i.e. `floor(epoch/timeout)`), so within one stall window a duplicated recovery delivery is neutralized by the same `SET NX` guard, while the next window mints a fresh key allowing a genuine later re-dispatch. Already-progressed targets (`STARTED`/terminal) and, where SPEC-11 locks exist, in-flight-locked matches are excluded from the recovery batch, so recovery never double-runs progressed work.

**Rationale**: §26 "Scrapyd's pending-job queue is per-node and not durable … detect a batch whose targets never progressed … re-dispatch after a timeout, protected by the same idempotency guards and in-flight match locks" + FR-015 / US3-AS3. Reading stall from `scrape_job_targets` state (durable Postgres, the Constitution's "job state in PostgreSQL" observability rule) needs no batch table; the window bucket makes recovery itself idempotent without a persisted generation counter.

**Alternatives rejected**: (a) a persisted `dispatched_at` + monotonic re-dispatch generation on a batch row — needs the D1-rejected table; the stall-window bucket is a stateless equivalent. (b) re-dispatching the whole original batch — would re-run already-progressed targets, violating US3-AS3; filtering to non-progressed targets is required.

---

## D5 — Counters aggregated from targets, never per-target increments

**Decision**: `aggregate_counts(session, scrape_job_id, workspace_id)` (`app_shared.jobs.targets`) runs one scoped `SELECT status, COUNT(*) FROM scrape_job_targets WHERE scrape_job_id = :id GROUP BY status`, and a single `UPDATE scrape_jobs SET success_count=…, failure_count=…, skipped_count=…` writes the derived totals. It is called (a) periodically by a `maintenance` task (`refresh_job_counters`) and (b) at finalization — **never** as a per-target `+1` on the job row. `mark_target(session, …)` (the FR-017 writer used by the spider/finalizer to transition a target `PENDING→STARTED→COMPLETED/FAILED/SKIPPED` with `started_at`/`completed_at`/`error_code`) touches only the **target** row, never the job counters.

**Rationale**: §26 price_analysis note + FR-018 + SC-004 + Principle VIII: per-target increments serialize thousands of writes on one hot job row at 10k–20k targets. `GROUP BY` aggregation makes the job-row write count independent of target count.

**Alternatives rejected**: per-target `UPDATE scrape_jobs SET success_count = success_count + 1` — the exact hot-row anti-pattern Principle VIII forbids; an in-memory running counter in the dispatch task — lost on retry/crash and can't see spider-driven transitions.

---

## D6 — Deterministic finalization + zero-target rule

**Decision**: `resolve_finalized_status(success, failure, skipped, total) -> ScrapeJobStatus` (pure, `app_shared.jobs.lifecycle`) is the single ordered rule: `total == 0` → **COMPLETED**; all targets terminal and `success == total` → **COMPLETED**; `success > 0` and (`failure > 0` or `skipped > 0`) → **PARTIAL_FAILED**; `success == 0` (with failures/skips) → **FAILED**. A job with zero targets is finalized **immediately at creation** as COMPLETED (`total_targets = 0`, `completed_at` set), with **no** dispatch enqueued. `finalize_jobs` (a `maintenance` task) finalizes a job once **all** its targets are terminal, setting `completed_at`. `started_at` is set when `dispatch_job` begins work.

**Rationale**: FR-019 + FR-020 + US2-AS4 + US3-AS2 + spec Clarification (zero-active-match → create + finalize COMPLETED, not reject, for observability + idempotency). A pure function makes the boundary values (all-success / mixed / none-success / skipped-only / zero) unit-testable deterministically, as the constitution's testing gate requires for ordered decision rules.

**Alternatives rejected**: treating a skipped target as a failure — the model carries a distinct `skipped_count` and SKIPPED status (§22 / edge cases), so a single-target job that is skipped is PARTIAL_FAILED/handled per the rule, not conflated with FAILED. Rejecting a zero-target run — contradicts the clarified observability decision.

---

## D7 — Celery fork-safety reuses the existing `worker_process_init` hook

**Decision**: FR-016 needs **no new code** — `apps/workers/app/workers/celery_app.py` already registers `@worker_process_init.connect` → `app_shared.database.dispose_engine()` (added in the SPEC-01 skeleton precisely for "before any DB-touching task exists"). SPEC-08 introduces the **first** DB-touching worker tasks (`dispatch_job`, `finalize_jobs`, `recover_stalled_batches`), which rely on that hook so each forked prefork child builds its own engine/pool on first use. A unit test asserts the hook disposes the inherited engine before first task use.

**Rationale**: §4/§35 "Celery engine fork-safety" + FR-016 + the spec Edge Case. The hook exists and is correct; the discipline is to *rely on it* (and test it) rather than re-implement, matching the constitution's "dispose inherited engine on `worker_process_init`."

**Alternatives rejected**: disposing the engine inside each task body — redundant and racy versus the once-per-fork signal already wired.

---

## D8 — API→worker enqueue seam (`app_shared.messaging`), no `apps/workers` import from the API

**Decision**: `app_shared.messaging.enqueue(name, *, queue, kwargs=None)` wraps a lazily-constructed `celery.Celery(broker=REDIS_URL)` producer and sends by **task name** (from `app_shared.task_names`). The API router (and later the scheduler) enqueue `scrape_dispatch.dispatch_job` through this seam; they never import `apps/workers`. The dispatch task itself is registered in `apps/workers` under the same name.

**Rationale**: Constitution I + the `task_names` module's stated purpose ("any process … can enqueue work via Celery's `send_task(name, …)` without importing `apps/workers`"). A single shared producer keeps the worker import closure (and, later, scrapy/twisted) out of the API. `app_shared` importing `celery` is within the boundary (the ban is scrapy/twisted/playwright/fastapi, not celery); `task_names.py` itself stays celery-free, the new `messaging.py` is where the celery import lives.

**Alternatives rejected**: the API constructing its own Celery app inline — duplicates broker config and the send path in every producer; importing `apps.workers.tasks_jobs` to call `.delay()` — pulls the worker (and its future scrapy-adjacent deps) into the API import graph, violating Principle I.

---

## Cross-cutting: unit-vs-live split (no container engine in this env)

Per the SPEC-02→07 deferred-verification pattern and the master REPO CONTEXT: DB/Redis/Scrapyd-independent logic (batching + 50–200 bounds, deterministic node selection, the finalized-status rule incl. boundary values, counter aggregation + the service + dispatch orchestration against fake sessions/clients/redis, the messaging seam against a fake producer, model/unique/RLS DDL render via offline `alembic upgrade head --sql`, import boundaries + workspace-scoping guard, endpoint request/response with a dependency-overridden session) is **fully unit-tested here**. Live-stack behavior (real run-match/run-variant end-to-end, actual `unique(scrape_job_id, match_id)` + composite-FK enforcement, RLS row denial, real `schedule.json` per-batch dispatch, retried-dispatch no-double-run) is **authored and skip-marked** for a full-stack host. No test contacts a real competitor domain.

## Migration head

Current Alembic head is **`2db33dea5e14`** (`observations_current_prices_tables`, SPEC-07). The new migration's `down_revision = 2db33dea5e14`; single linear head preserved (CI head guard).
