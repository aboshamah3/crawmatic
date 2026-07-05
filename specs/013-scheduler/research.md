# Phase 0 Research: SPEC-13 Scheduler

All decisions below resolve the Technical Context unknowns. Format per decision:
**Decision / Rationale / Alternatives considered.**

Source anchors: PROJECT_SPEC.md §22 (models), §25 (job flow), §28 (scheduler), §32
(workspace isolation); SPEC-08 jobs service; SPEC-11 match locks; SPEC-12 scheduler loop.

---

## R1. Cron / interval next_run_at computation library

**Decision**: Add `croniter` (pure-Python) as a dependency of `libs/shared` (`app_shared`) and put
all cadence math in a new scraping-free module `app_shared/scheduling/cadence.py`. `interval_minutes`
needs no library (`next = run_time + timedelta(minutes=interval)`).

**Rationale**:
- No cron library is currently present (grep of root `pyproject.toml` / `uv.lock`: no `croniter`,
  `apscheduler`, `cron-converter`, `crontab`). A dependency must be added or the logic hand-rolled.
- 5-field UTC cron (per autospec-decisions) has enough edge cases (ranges, steps, DOW/DOM
  interaction, month lengths) that a hand-rolled parser is a correctness liability; a wrong
  next-run is a silently missed or duplicated refresh.
- `croniter` is a tiny, widely-used, pure-Python package. It imports **none** of
  Scrapy/Twisted/Playwright/FastAPI, so it keeps `app_shared` and the scheduler image
  scraping-free (Principle I, FR-019) and keeps `tests/unit/test_import_boundaries.py` green.
- Placing cadence math in `app_shared` (not `apps/scheduler` or `apps/api`) means one
  implementation shared by **both** the API (compute first `next_run_at` on create/update,
  validate the cron string) and the scheduler (recompute on each run). Unit-testable in isolation.

**Alternatives considered**:
- *Hand-rolled 5-field parser* — rejected: correctness risk vs. near-zero benefit; croniter is
  smaller than a well-tested internal parser.
- *APScheduler* — rejected: heavyweight, brings its own scheduler/executor abstractions we do not
  use; the master doc mandates a **custom DB-driven** scheduler, not APScheduler.
- *Put cadence math in `apps/scheduler` only* — rejected: the API also needs it at create/update
  time; duplicating would drift.

### Cadence semantics (settled)

| Trigger | cron_expression | interval_minutes |
|---|---|---|
| First `next_run_at` on **create/update** | `croniter(expr, base=now_utc).get_next(datetime)` | `now_utc + interval_minutes` |
| Recompute on each **run** (run_time = now) | `croniter(expr, base=run_time).get_next(datetime)` | `run_time + interval_minutes` |

Both branches base the computation on the **actual run time / now**, never on the stale
`next_run_at`. This gives FR-016 backlog tolerance for free: a rule whose `next_run_at` is far in
the past fires **once** on the next pass and its new `next_run_at` lands strictly in the future
(next cron occurrence after `now`, or `now + interval`) — no per-missed-interval catch-up.
All datetimes are timezone-aware UTC (`TZDateTime`, FR-018).

---

## R2. Cross-tenant claim under FORCE RLS — scheduler session seam

**Decision**: The scheduler's due-rule claim is inherently cross-workspace (it must find due rules
across **all** tenants in one `SELECT ... FOR UPDATE SKIP LOCKED`). Under `FORCE ROW LEVEL
SECURITY` the ordinary pooler role returns zero rows with no `app.workspace_id` set. So the
scheduler pass runs on a **BYPASSRLS system session**, mirroring the existing `get_auth_session()`
seam. Add `get_system_session()` in `app_shared/database.py`, bound to a new
`Settings.SYSTEM_DATABASE_URL` that **falls back to `AUTH_DATABASE_URL`** when unset (ops may point
it at the existing `crawmatic_auth` BYPASSRLS role or a dedicated `crawmatic_scheduler` role).

Workspace isolation is preserved by **application-level scoping**: every read/write the scheduler
performs for a claimed rule goes through `scoped_select(...)` / explicit `workspace_id=` on the
job/target inserts (the SPEC-08 job service already does this). RLS is defense-in-depth that a
trusted system component legitimately bypasses — exactly as the auth credential-lookup seam does.

**Rationale**: Principle II mandates app-level scoping as the primary control and names RLS as
defense-in-depth; §28's claim SQL is explicitly cross-tenant. A per-workspace claim loop would
(a) require enumerating workspaces, (b) defeat the single SKIP-LOCKED batch, and (c) still need a
way to discover which workspaces have due rules. The BYPASSRLS seam already exists and is the
established pattern for trusted, pre-/cross-context reads.

**Alternatives considered**:
- *Reuse `get_auth_session()` directly* — rejected: its docstring pins it to "credential
  resolution ONLY"; overloading it erodes that contract. A parallel, clearly-named helper is
  cleaner and keeps auth's contract intact.
- *Iterate workspaces, set context per workspace, claim within each* — rejected: no cheap
  "which workspaces have due rules" signal; N round-trips; loses one-shot SKIP-LOCKED batching.
- *Grant the pooler role BYPASSRLS* — rejected: the pooler role serves the whole API; making it
  BYPASSRLS would silently disable RLS defense-in-depth for every request.

This is the **one** documented deviation and is tracked in plan.md → Complexity Tracking.

---

## R3. Enqueue-before-commit ordering (FR-012/FR-014) — reuse SPEC-08 transaction posture

**Decision**: Reuse the SPEC-08 job service's existing posture: the service **flushes but never
commits** and enqueues the Celery dispatch task **inside** the caller's open transaction. The
scheduler owns the transaction: it claims the rule, calls the job service (which enqueues the
`scrape_dispatch.dispatch_job` task before returning), advances the rule's scheduling fields, then
commits **once**. Enqueue therefore happens strictly before commit, satisfying §28 / FR-012.

**Rationale**: `app_shared/jobs/service.py` already calls `_enqueue_dispatch(...)` at the end of
`create_match_job` / `create_variant_job`, with commit owned entirely by the caller
(FastAPI dep today; the scheduler transaction here). This is precisely the required ordering —
no refactor of the commit boundary is needed, only a new **scope-aware** entry point (R4).

**Crash safety (FR-014)**: if the claiming transaction fails to commit, `FOR UPDATE SKIP LOCKED`
releases the row lock on rollback and `next_run_at` is unchanged, so a later pass re-runs the
rule. Any dispatch message that already reached the broker is neutralized downstream by the
SPEC-08 idempotent dispatch guard (`dispatch_key(scrape_job_id, batch_index)` Redis `SET NX`,
`app_shared/scrapyd/client.py`) and the SPEC-11 in-flight match lock — favoring a possible
duplicate over a missed run.

**Alternatives considered**:
- *Commit-then-enqueue / transactional outbox* — rejected: explicitly forbidden by §28 and FR-014
  ("never commit-then-enqueue"). The whole point is to bias toward duplicates, not misses.

---

## R4. Reuse of the SPEC-08 job path — one new shared entry point

**Decision**: The SPEC-08 service today exposes only `create_match_job` and `create_variant_job`,
both hard-coding `type=MANUAL, source=API` and covering only MATCH/VARIANT scopes. Add:

1. `app_shared/jobs/scopes.py` — `resolve_scope_matches(session, *, workspace_id, scope, target_id)
   -> list[CompetitorProductMatch]`: the single scope→active-match resolver for all six scopes
   (pure query logic, unit-testable). This is the "same scope→match resolution the manual run
   flows use" required by FR-010 (the manual scope-run endpoints for workspace/competitor/product/
   group are future work and will call the same resolver).
2. `app_shared/jobs/service.py` — `create_scope_job(session, *, workspace_id, scope, target_id,
   requested_by, job_type=ScrapeJobType.MANUAL, source=ScrapeJobSource.API)
   -> tuple[uuid.UUID | None, ScrapeJobStatus | None]`: resolves matches via
   `resolve_scope_matches`, creates the `ScrapeJob` (with the given `type`/`source`/`scope`) +
   one `ScrapeJobTarget` per active match, flushes, and enqueues dispatch before returning.

The scheduler calls `create_scope_job(..., job_type=SCHEDULED, source=SCHEDULER)`.

**Scope→active-match resolution** (all filtered `workspace_id == ws AND status == ACTIVE`,
using `scoped_select(CompetitorProductMatch, ws)`):

| Scope | Predicate on `CompetitorProductMatch` (`M`) |
|---|---|
| WORKSPACE | (base only) |
| COMPETITOR | `M.competitor_id == target_id` |
| PRODUCT | `M.product_id == target_id` (match carries `product_id` directly) |
| VARIANT | `M.product_variant_id == target_id` |
| MATCH | `M.id == target_id` |
| PRODUCT_GROUP | `EXISTS(product_group_items PGI WHERE PGI.product_group_id == target_id AND (PGI.product_id == M.product_id OR PGI.product_variant_id == M.product_variant_id))` |

`product_group_items` membership rows carry `product_id` **XOR** `product_variant_id`; the OR of
the two EXISTS arms covers both member kinds (a product-arm member pins all variants of a product;
a variant-arm member pins one variant).

**Rationale**: Centralizing resolution in `app_shared` satisfies FR-010/FR-011 (reuse, don't
duplicate) and lets the eventual manual scope-run endpoints share the exact logic. Existing
`create_match_job`/`create_variant_job` are left untouched to avoid SPEC-08 regressions; they MAY
later delegate to `create_scope_job` (out of scope here).

**Alternatives considered**:
- *Duplicate resolution logic inside `apps/scheduler`* — rejected: violates "reuse, do not
  reinvent"; would drift from manual run flows (FR-010).
- *Add `type`/`source` params to the two existing functions and branch there* — rejected: they are
  scope-specific; a single generic `create_scope_job` is the clean seam and covers all six scopes.

### Zero-match handling (FR-015 / US2 AS-4)

**Decision**: When `resolve_scope_matches` returns empty, `create_scope_job` creates **no** job and
**no** dispatch (returns `(None, None)`); the scheduler still advances `next_run_at`/`last_run_at`.
This avoids both a wasted dispatch and accumulating empty COMPLETED job rows for a recurring rule
whose scope is currently empty (e.g. an hourly WORKSPACE rule before any matches exist).

**Alternative considered**: mirror `create_variant_job`, which for zero matches inserts a COMPLETED
job row (audit trail) — rejected for the scheduler path because a recurring empty rule would
accrue an empty job every tick; the manual API path keeps its COMPLETED-job response semantics.

---

## R5. Claiming & concurrency (FR-007/008/009)

**Decision**: One claim query per pass on the BYPASSRLS system session:

```sql
SELECT * FROM refresh_rules
WHERE enabled AND next_run_at <= :now
ORDER BY next_run_at
FOR UPDATE SKIP LOCKED
LIMIT :batch_limit;   -- SCHEDULER_CLAIM_BATCH_LIMIT
```

Expressed in SQLAlchemy as `select(RefreshRule).where(RefreshRule.enabled, RefreshRule.next_run_at
<= now).order_by(RefreshRule.next_run_at).with_for_update(skip_locked=True).limit(batch_limit)`.
The whole pass (claim + per-rule job creation + rule updates + all enqueues) runs in **one
transaction** committed once. **No** global/advisory pass-lock (FR-009). A transaction-scoped
per-rule `pg_advisory_xact_lock` is permitted as belt-and-suspenders but is **not** planned —
`SKIP LOCKED` already guarantees exclusive per-row claiming across instances.

**Rationale**: `FOR UPDATE SKIP LOCKED` is exactly §28's prescription; disjoint batches let N
instances run concurrently with 0 duplicate / 0 missed runs (SC-003). A `LIMIT` bounds how long a
pass holds row locks while it enqueues (a network hop per rule), keeping lock windows short so
sibling instances make progress. A partial index `(next_run_at) WHERE enabled` makes the ordered
due-scan cheap at scale (Principle VIII).

**Alternatives considered**:
- *Global advisory pass lock (`lock:scheduler:refresh-rules`)* — rejected: FR-009 / §28 forbid it;
  it would serialize instances and negate SKIP LOCKED.
- *One transaction per rule* — acceptable but chattier; a single bounded batch transaction is
  simpler and still crash-safe (whole batch rolls back → all locks release, no `next_run_at`
  advanced). Batch size is the tunable that trades throughput vs. lock-hold time.

---

## R6. Scope enum reuse

**Decision**: Reuse the existing `app_shared.enums.ScrapeScope` (WORKSPACE, COMPETITOR, PRODUCT,
VARIANT, PRODUCT_GROUP, MATCH) for `refresh_rules.scope` rather than minting a parallel
`RefreshScope`. The members are identical to §22's refresh scopes and map 1:1 to the resulting
scrape job's `scope`.

**Rationale**: DRY; a refresh rule's scope *is* the scrape job's scope. Avoids a second enum to
keep in sync.

---

## R7. FK ondelete for scope-target columns (FR-020)

**Decision**: Each scope-target column on `refresh_rules` (`product_id`, `product_variant_id`,
`product_group_id`, `competitor_id`, `match_id`) is a **nullable workspace-local composite FK**
(`(workspace_id, X) -> table(workspace_id, id)`) with **`ondelete="CASCADE"`**. Deleting a
product/variant/group/competitor/match therefore deletes the rules that target it — the delete is
never blocked and the scheduler never dereferences a missing target.

**Rationale**: FR-020 accepts either "reference cleared" or "rule removed." CASCADE (rule removed)
is the cleaner of the two: `SET NULL` would leave a non-WORKSPACE rule with a NULL target id,
violating the scope↔target-id invariant (FR-002). Existing entity→entity FKs are `NO ACTION`
(they restrict), but those are integrity edges between live rows; a refresh rule is a
policy attachment that has no meaning once its target is gone, so cascading it away is correct.
Defense-in-depth: `resolve_scope_matches` filters by id, so even a momentarily dangling target id
naturally resolves to zero matches (handled by R4 zero-match path) — the pass never crashes.

**Alternatives considered**:
- *`SET NULL`* — rejected: produces an invalid scoped rule (NULL target on a non-WORKSPACE scope).
- *Plain soft ref (no FK), like `ScrapeJobTarget.match_id`* — rejected: soft refs suit historical
  records; a live policy row should be cleaned up transactionally when its target dies.

---

## R8. Scheduler poll interval & batch knobs (deferred from clarify)

**Decision**: Add two `Settings` fields (env-overridable, same pattern as
`STRATEGY_STATS_FLUSH_INTERVAL_SECONDS`):
- `SCHEDULER_POLL_INTERVAL_SECONDS: int = 30`
- `SCHEDULER_CLAIM_BATCH_LIMIT: int = 100`

The existing `scheduler_app.py` loop already ticks every `_TICK_SECONDS = 1.0` and fires
maintenance enqueues on an interval; add a second independent interval accumulator for the refresh
pass. 30 s is a sane default: fine enough that a due rule fires within half a minute, coarse enough
to avoid hammering Postgres. Not load-bearing on architecture or acceptance tests (autospec-clarify
deferred it here explicitly).

**Rationale**: matches the established cadence-knob pattern; keeps the value out of code.

---

## R9. Validation & error codes (FR-002/003)

**Decision**: Validate at the API layer (Pydantic + repository check) **and** enforce a DB CHECK
constraint (defense-in-depth):
- Exactly one cadence: DB CHECK `num_nonnulls(cron_expression, interval_minutes) = 1`; schema-level
  rejection with error code `INVALID_CADENCE`.
- `interval_minutes > 0` when present: DB CHECK; schema `ge=1`.
- Scope↔target-id consistency (WORKSPACE ⇒ all target ids NULL; each other scope ⇒ exactly its
  target id NON-NULL and the rest NULL): DB CHECK `ck_refresh_rules_scope_target`; schema-level
  cross-field validator returning `SCOPE_TARGET_MISMATCH`.
- Cron string parseability (croniter) at create/update: reject with `INVALID_CRON`.
- Target-id belongs to the caller's workspace: verified via `scoped_get` before insert (returns
  404/422 if cross-workspace or missing), same as SPEC-05/06 routers.

**Rationale**: mirrors the repo's structured `{"error": {"code","message"}}` envelope and the
belt-and-suspenders posture (Pydantic + DB constraint) used by SPEC-05/08.

---

## Resolved unknowns summary

| Unknown | Resolution |
|---|---|
| Cron library | `croniter` in `app_shared` (R1) |
| Cadence math home | `app_shared/scheduling/cadence.py`, shared by API + scheduler (R1) |
| Cross-tenant claim vs RLS | BYPASSRLS `get_system_session()`, app-level scoping preserved (R2) |
| Enqueue vs commit | Reuse SPEC-08 flush-not-commit + enqueue-inside-txn (R3) |
| Scope resolution reuse | New `resolve_scope_matches` + `create_scope_job` in `app_shared.jobs` (R4) |
| Zero-match behavior | No job, no dispatch, still advance schedule (R4) |
| Claim SQL / concurrency | `FOR UPDATE SKIP LOCKED` batch, no global lock (R5) |
| Scope enum | Reuse `ScrapeScope` (R6) |
| FK ondelete | Composite FK `ondelete=CASCADE` (R7) |
| Poll interval | `SCHEDULER_POLL_INTERVAL_SECONDS=30`, `SCHEDULER_CLAIM_BATCH_LIMIT=100` (R8) |
| Validation/errors | Pydantic + DB CHECK; `INVALID_CADENCE`/`SCOPE_TARGET_MISMATCH`/`INVALID_CRON` (R9) |
