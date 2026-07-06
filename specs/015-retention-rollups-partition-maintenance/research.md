# Phase 0 Research: Retention, Rollups & Partition Maintenance

**Feature**: SPEC-15 | **Date**: 2026-07-06 | **Spec**: [spec.md](./spec.md)

All decisions below are grounded in the existing codebase (verified by inspection, file paths
cited) and PROJECT_SPEC §22/§29. No `NEEDS CLARIFICATION` remains. The spec's four Session
2026-07-06 clarifications are treated as settled inputs, not re-derived here.

---

## R1 — `variant_price_daily_rollups` does NOT exist yet; SPEC-15 must CREATE it

**Decision**: SPEC-15 authors the `variant_price_daily_rollups` **table** (ORM model + one Alembic
migration + RLS + `WORKSPACE_OWNED_MODELS` registration), not merely rows into a pre-existing table.

**Rationale**: The spec's *Assumptions* (spec.md:164) claim the table "already exist[s], defined by
… SPEC-09," but the codebase contradicts this. SPEC-09 explicitly deferred it to SPEC-15:
`specs/009-current-prices-alerts/spec.md:144` ("`variant_price_daily_rollups` … are SPEC-15"),
`specs/009-current-prices-alerts/tasks.md:38` ("OUT OF SCOPE (do NOT build): `variant_price_daily_rollups`"),
`specs/009-current-prices-alerts/plan.md:55`. There is no model, migration, or `__init__` export
for it anywhere. FR-009/FR-010/FR-013/FR-014 and the Key Entity fully specify its shape, so no human
decision is required — only a correction of the erroneous assumption. This is the single new table
(and single new migration) this feature adds; it does **not** create or alter the append-heavy parent
tables, which already exist (`price_observations`, `request_attempts`, `price_alert_events`).

**Alternatives considered**: Treat it as pre-existing and only write rows — rejected: it would fail
at import/insert time (no table). Add it in a SPEC-09 patch — rejected: SPEC-09 is shipped and
deliberately deferred it here.

---

## R2 — Partition create/drop is RUNTIME job DDL, not Alembic migrations

**Decision**: The create-next-month and drop-expired-partition operations are **runtime DDL issued
from job logic** (pg_partman-style, in-app), never Alembic migrations. Alembic is used **only once**
in this feature — for the new `variant_price_daily_rollups` table.

**Rationale**: The existing partitioned tables were born with only *current + next* month partitions,
created inside their birth migration via raw `op.execute("CREATE TABLE … PARTITION OF …")`
(`alembic/versions/2db33dea5e14_observations_current_prices_tables.py:114-118`, and the alerts
migration for `price_alert_events`). Migrations are a one-time schema event; monthly partition
lifecycle is a *recurring* operation that cannot live in the linear migration history (you cannot ship
a migration every month). PROJECT_SPEC §29 and the constitution (Principle VIII) frame partitioning +
retention as a runtime maintenance concern. There is currently **zero** runtime partition-management
code in the repo (verified: no `pg_partman`, no runtime `CREATE TABLE … PARTITION OF`, no
`DROP TABLE`/`DETACH PARTITION` from Python) — SPEC-15 is greenfield for this and mirrors the
migration-time conventions below.

**Conventions to mirror** (from `2db33dea5e14` `_month_partition_bounds`):
- Child partition name: `{parent}_{YYYY}_{MM}` (e.g. `price_observations_2026_07`) — stays within
  Postgres's 63-byte identifier budget.
- Half-open monthly bounds: `FOR VALUES FROM ('YYYY-MM-01') TO ('<next-month>-01')`, UTC.
- RLS applied to the partitioned **parent propagates to every partition** (current + future), so
  runtime-created partitions need **no** per-partition RLS DDL (verified in the `2db33dea5e14`
  docstring lines 22-24, 193-196). This is critical: FR-014's isolation holds automatically.

**Alternatives considered**: Monthly Alembic migrations — rejected (unbounded history, breaks the
single-head model, needs a deploy per month). A DB extension (`pg_partman`) — rejected: the locked
stack (constitution Tech Constraints) does not include it, and the create/drop logic is small.

---

## R3 — Registry: code-level constant in `app_shared`, retention windows env-tunable

**Decision**: The partitioned-table registry (FR-001) is a module-level constant — a tuple of
frozen dataclass entries `(table_name, partition_key_column, feeds_rollups, retention)` — in a new
scraping-free module `libs/shared/app_shared/maintenance/registry.py`. Retention **window lengths**
are `Settings` fields (Principle IV, DB/env-tunable) with the §29 defaults; the **set of tables** and
their partition-key columns are code constants.

**Rationale**: Each registry entry is intrinsically bound to a real table + its declared
`postgresql_partition_by` column — you cannot operationally "add" a partitioned table without a
model + migration, so a DB config row for the table set would be dishonest. What §29 says "may be
adjusted operationally" is the retention *durations*, which map cleanly to `Settings` knobs
(`RETENTION_PRICE_OBSERVATIONS_DAYS=90`, `RETENTION_REQUEST_ATTEMPTS_DAYS=90`,
`RETENTION_PRICE_ALERT_EVENTS_DAYS=365`, `RETENTION_WEBHOOK_EVENTS_DAYS=90`,
`RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS=730`). This matches how SPEC-13 added interval/limit knobs
to `config.py` rather than a job-config table (there is no generic "job types" table in the repo —
only `refresh_rules`, which is scrape-scope-specific). Initial registry:

| table | partition key | feeds_rollups | retention default |
|---|---|---|---|
| `price_observations` | `scraped_at` | **yes** | 90 days |
| `request_attempts` | `created_at` | no | 90 days |
| `price_alert_events` | `created_at` | no | 365 days |
| `webhook_events` | `created_at` | no | 90 days (absent until SPEC-16) |

`variant_price_daily_rollups` is **not** in the partition registry (it is not partitioned — see R5);
its 2-year retention is enforced by a separate age-based row policy, not partition drop.

**Alternatives considered**: A DB `partition_registry` table — rejected (no operational add path
without a migration; over-engineered for 4 fixed entries). Hardcoding per-job — rejected (Principle
IV / DRY: all three jobs share the one registry).

---

## R4 — Table-existence gate via `to_regclass` (skip `webhook_events` cleanly)

**Decision**: Before touching any registered table, each job probes existence with
`SELECT to_regclass('public.<table>')` and skips (logging "absent") when it returns `NULL` (FR-002).

**Rationale**: `webhook_events` is registered but is introduced by SPEC-16 and does not exist yet.
`to_regclass` returns `NULL` for a missing relation without raising — a single, cheap, catalog-safe
probe. A registered-but-absent table must never fail the whole pass (FR-002, US1 AS-4). Confirmed no
existing helper; this is a new one-liner in `app_shared/maintenance/partitions.py`.

**Alternatives considered**: `information_schema.tables` query — equivalent but more verbose.
try/except around the DDL — rejected (masks real errors, not idempotent-clean).

---

## R5 — `variant_price_daily_rollups` shape: NOT partitioned, workspace-owned, upsert key

**Decision**: A non-partitioned, workspace-owned current/durable table with single-column UUIDv7 `id`
PK and `unique(workspace_id, product_variant_id, date)` as the upsert arbiter (FR-010). Columns:
`workspace_id` (FK → workspaces, RLS anchor), `product_id` (soft UUID, no FK), `product_variant_id`
(soft UUID, no FK), `date` (`Date`, the UTC calendar date), `currency` (`CHAR(3)`), `client_price`
(`Money`, NOT NULL), `cheapest_competitor_price`/`average_competitor_price`/`highest_competitor_price`
(`Money`, nullable), `comparable_competitor_count` (`Integer`, NOT NULL), `latest_alert_type`
(`enum_column(AlertType)`), plus `TimestampMixin`. Closest template: `VariantPriceState`
(`libs/shared/app_shared/models/alerts.py:56-95`).

**Rationale**: 2M rows/month is a *raw* concern; a per-(workspace, variant, day) rollup is far
smaller and lives 2 years — partitioning it is unnecessary and its 2-year retention is a cheap
age-based DELETE-by-date-range... *no* — see R7: even the rollup's own retention is a simple
`DELETE WHERE date < cutoff` guarded by volume (it is bounded), OR the rollups table itself could be
partitioned. It is NOT partitioned in v1 (matches `variant_price_states`/`match_current_prices`
current-state convention; keeps the upsert-on-unique simple). The field names deliberately reuse
`VariantPriceState`'s (`cheapest/average/highest_competitor_price`, `comparable_competitor_count`,
`client_price`, `currency`) so the rollup is a faithful snapshot of the SPEC-09 surface, not a new
vocabulary. `Money` (`Numeric(18,4)`, finite-only) satisfies FR-012; `Date` + UTC satisfies FR-025.

**Alternatives considered**: Partition the rollup monthly by `date` — rejected for v1 (adds the same
maintenance burden this spec is trying to bound, for a table that is already small; the current-state
tables it resembles are unpartitioned). Composite natural PK `(workspace_id, product_variant_id,
date)` with no `id` — rejected (breaks the repo-wide UUIDv7-`id` convention from `Base`).

---

## R6 — Rollup source & aggregation: driven by that day's `price_observations`

**Decision**: The daily-rollup job for UTC date `D` is **driven by `price_observations` whose
`scraped_at::date = D`** (the clarified rollup-date rule). For each `(workspace_id,
product_variant_id)` that had at least one observation on `D`:
- **Competitor min/avg/max + comparable count** are aggregated from that day's observations, filtered
  to `comparable = true AND currency = <client currency>` (FR-011). The `comparable` flag is the
  *persisted* SPEC-09 decision (currency-mismatch already flipped it false) — reading it is **not**
  recomputing the comparison logic.
- **`client_price`, `currency`, `latest_alert_type`, `product_id`** are read from the SPEC-09
  current-comparison surface `variant_price_states` for that variant (`alerts.py:56-95` already
  materializes exactly these via `apps/workers/app/workers/tasks_analysis.py`).
- A variant with observations but **zero** comparable ones → row written with `client_price`, NULL
  competitor min/avg/max, count 0 (FR-013, US2 AS-3).
- A variant with **no** observations on `D` → **no** rollup row (edge case "no spurious rows").

**Rationale**: The clarification pins rollup-date to `price_observations.scraped_at`'s UTC date and
defines coverage as "every date that had source pricing data" — i.e. observation dates. Driving the
rollup off observations (a) makes the verify-before-drop coverage check exact (the rollup for date
`D` creates precisely the rows that cover `D`), and (b) enables idempotent backfill of any past day
while its raw partition still exists (a stated capability, FR-010). Competitor aggregates MUST come
from the raw per-competitor prices (FR-011 talks about excluding individual mismatched prices), which
only `price_observations` retains per-day. `variant_price_states` is **current-state only** (one row
per variant, overwritten each recompute; no `date` dimension), so it cannot supply per-day competitor
aggregates for a past day — but it is the correct, non-recomputed source for the client price /
currency / alert type of the variant.

**Known limitation (documented, accepted)**: for the *default* cadence — the most recent completed
UTC day — `variant_price_states` still reflects that day, so `client_price`/`alert_type` are exact.
For a *deep backfill* of an older day, `variant_price_states` carries the latest snapshot, so
`client_price`/`alert_type` on a backfilled old-day rollup reflect current state, not that day's. This
is an inherent consequence of the platform retaining client price only as current-state (no per-day
client-price history exists anywhere) and is consistent with the spec's Assumption (spec.md:166) that
rollup source data comes from the SPEC-09 current surfaces. Competitor aggregates for a backfilled day
remain exact (from raw observations).

**Alternatives considered**: Pure snapshot of `variant_price_states` (copy its pre-computed
cheapest/average/highest) — rejected: cannot backfill arbitrary past days, and the clarification
frames the source as dated observations, and FR-011 requires per-price currency filtering that a
pre-aggregated snapshot cannot re-express. Recompute the full alert decision tree — rejected:
violates "reads and aggregates … rather than recomputing comparison logic" (Assumption, spec.md:166);
the alert type is read from `variant_price_states`, not re-decided.

---

## R7 — Retention windows & the verify-before-drop ordering guarantee

**Decision**:
- **Partition-drop retention** (FR-015/017/018): a monthly partition is droppable only when its
  **entire** half-open range `[start, end)` is strictly older than `now_utc - retention_window`
  (partition-granular, deterministic — FR-018). Drop via `DROP TABLE <partition>` (a partition of a
  RANGE-partitioned parent detaches+drops atomically); **never** bulk `DELETE` (FR-015, SC-003).
- **Verify-before-drop** (FR-016, only for `feeds_rollups=true`, i.e. `price_observations`): before
  dropping partition `P` covering dates `[d0, dN)`, confirm coverage — the set of UTC dates in
  `[d0, dN)` that **had observations** minus the set of dates that **have ≥1 rollup row** must be
  empty. Concretely (cross-tenant, so run on the system session):
  `SELECT DISTINCT scraped_at::date FROM <P>` `EXCEPT`
  `SELECT DISTINCT date FROM variant_price_daily_rollups WHERE date >= d0 AND date < dN`.
  Non-empty ⇒ retain `P`, record `skipped_pending_rollups` (FR-016, US3 AS-2, SC-004). Empty ⇒ drop.
- **Non-rollup tables** (`request_attempts`, `price_alert_events`, `webhook_events`) drop by age
  alone (FR-019, US3 AS-4), each with its own window.
- **Rollup table's own retention** (`variant_price_daily_rollups`, 2 years): the rollups table is not
  partitioned, so its retention is an age-based `DELETE FROM variant_price_daily_rollups WHERE date <
  (now_utc::date - 730 days)`. This is the **one** sanctioned bulk delete in the feature and applies
  to a small, bounded, non-append-heavy summary table — SC-003 ("100% of *raw* partitions by drop")
  is about the append-heavy raw tables, which this is not.

**Rationale**: The clarification defines "verified complete" as date-level coverage ("a date with no
source data needs no rollup"), so the `EXCEPT` at date granularity is exactly the specified rule.
Dropping only when the whole range is past the cutoff (FR-018) avoids ever dropping a partition that
still holds in-window rows. The coverage check is inherently cross-tenant (one partition holds every
workspace's rows), so it uses the BYPASSRLS system session (R9).

**Alternatives considered**: Per-(workspace, variant, date) coverage — stronger but heavier and
**not** what the clarification specifies (it explicitly chose date-level). Kept as a possible future
tightening, noted in the retention contract. `DETACH PARTITION` then `DROP` in two steps — rejected:
a single `DROP TABLE` of the child is atomic and simpler; nothing needs the detached table.

---

## R8 — Job hosting: scheduler enqueues on a cadence, workers execute

**Decision**: Three new Celery **maintenance** tasks execute the job logic in `apps/workers`; the
existing scheduler loop enqueues them fire-and-forget on fixed cadences (mirroring
`_enqueue_stats_flush`). New task-name constants in `app_shared/task_names.py`:
`MAINTENANCE_PARTITION_CREATE = "maintenance.partition_create"`,
`MAINTENANCE_DAILY_ROLLUP = "maintenance.daily_rollup"`,
`MAINTENANCE_RETENTION_DROP = "maintenance.retention_drop"`, all routed to the existing `maintenance`
queue (`apps/workers/app/workers/celery_app.py` `task_queues`/`task_routes`). Implemented in a new
`apps/workers/app/workers/tasks_maintenance.py` (added to celery_app `include`). Cadence knobs in
`config.py`: `PARTITION_CREATE_INTERVAL_SECONDS` (default e.g. 86400 — daily, giving weeks of lead so
next-month always exists before the month begins per SC-001), `DAILY_ROLLUP_INTERVAL_SECONDS`,
`RETENTION_INTERVAL_SECONDS`.

**Rationale**: This is the established maintenance pattern — SPEC-08's `finalize_jobs` /
`recover_stalled_batches` and SPEC-12's `strategy_stats_flush` are `@app.task` maintenance sweeps on
the `maintenance` queue, triggered by the scheduler's fixed-cadence enqueues. Partition DDL, rollup
aggregation, and retention are heavier cross-tenant sweeps that belong in a worker (keeps the
scheduler loop light and single-purpose), not in the SKIP-LOCKED refresh pass (which is for
per-row *scrape* claiming). Ordering: creation and rollup have comfortable lead; retention runs on
its own cadence and internally re-checks coverage every pass, so a not-yet-rolled-up partition is
simply retained until a later retention pass (self-healing, edge case "rollups incomplete
indefinitely").

**Operational note**: `docker-compose.yml`'s `scheduler` service currently `depends_on` only
`pgbouncer`, not `redis`/broker — yet it already enqueues (the SPEC-13 refresh pass). SPEC-15 adds
more scheduler→broker enqueues; the compose `scheduler` service should add the broker dependency
(flagged for tasks; a deploy-topology detail, not job logic).

**Alternatives considered**: A DB-driven `refresh_rules`-style table per maintenance job — rejected
(over-engineered; these are singleton platform sweeps, not per-workspace scheduled scrapes). Run the
sweeps directly in the scheduler process like `run_refresh_pass` — rejected (heavier DDL/aggregation
work belongs off the scheduler tick, on the worker pool, matching `finalize_jobs`). `celery beat` —
rejected (does not exist in the repo yet; the hand-rolled accumulator is the established mechanism).

---

## R9 — Execution context: BYPASSRLS system session, app-level scoping preserved

**Decision**: All three maintenance tasks run under the existing BYPASSRLS **system session**
(`app_shared.database.get_system_sessionmaker`/`get_system_session`, `SYSTEM_DATABASE_URL` →
`AUTH_DATABASE_URL` fallback). Partition create/drop DDL and the cross-tenant coverage/existence
catalog reads are inherently cross-workspace and touch no workspace-owned rows. The daily-rollup
task's cross-tenant source scan (which workspaces/variants had observations on `D`) also needs to see
every workspace's rows, which `FORCE ROW LEVEL SECURITY` blocks for the ordinary pooler role.
**Workspace isolation is preserved at the application layer**: every rollup aggregate query filters
`workspace_id = <that workspace>` and every `variant_price_daily_rollups` upsert sets `workspace_id=`
explicitly — identical to how SPEC-13's `create_scope_job` keeps scoping under the system session.

**Rationale**: This mirrors the exact, already-sanctioned SPEC-13 deviation (plan Complexity
Tracking): the cross-tenant scan bypasses RLS, but no query fetches a workspace-owned row without an
explicit `workspace_id` predicate, so Principle II's mandatory control holds and RLS is the
defense-in-depth layer a trusted system component legitimately bypasses. DDL (`CREATE/DROP TABLE`) is
not subject to RLS but does need an elevated role with DDL privilege — the system role is the
designated elevated context. The unscoped source scans are annotated `# noqa: workspace-scope`, like
the existing pre-auth `User`/`ApiKey` and `finalize_jobs` scans, and checked by
`scripts/check_workspace_scoping.py`.

**Alternatives considered**: Ordinary `get_session` + per-workspace `set_workspace_context` (the
`finalize_jobs` pattern) — viable for the rollup writes and keeps RLS enforced, but the *initial*
cross-tenant "which workspaces had activity" scan of `price_observations` still returns zero rows
under forced RLS without a context, so a BYPASSRLS scan is needed regardless; using one session
(system) for the whole task is simpler and matches SPEC-13. Granting the pooler role BYPASSRLS —
rejected (silently disables RLS platform-wide).

---

## R10 — Soft-reference tolerance (US4): the one dangling reader is `match_current_prices.observation_id`

**Decision**: The only soft reference that dangles into a droppable partition is
`match_current_prices.observation_id` (plain nullable UUID, no FK, into `price_observations.id` —
`libs/shared/app_shared/models/observations.py:186`). SPEC-15 (a) audits every reader of
`match_current_prices` to confirm none hard-joins to `price_observations` on `observation_id` in a way
that would drop/error the row when the partition is gone (FR-021), and (b) adds a **dangling-soft-
reference tolerance check** — a maintenance helper that treats an `observation_id` pointing at an
absent row as an expected, tolerated condition, reported for operator visibility, never as corruption
(FR-022).

**Rationale**: The observations model docstring already declares `observation_id` "may dangle after a
retention-by-drop partition removal, §22" — retention (this spec) is what makes dangling the steady
state. Readers must rely on the denormalized fields `match_current_prices` already carries (`price`,
`currency`, `comparable`, `scraped_at`, …) rather than the raw row. This is a correctness guard over
existing read paths plus a small operator-visibility check, not a new capability — hence US4 is P3.

**Alternatives considered**: Add an FK with `ON DELETE SET NULL` — rejected: FKs into partitioned
tables are exactly what §22 forbids (they block partition drop); the whole point is *no* FK. A DB
trigger to null out dangling refs on drop — rejected (couples retention to every referencing table;
tolerance-on-read is the §22 design).

---

## Cross-cutting conventions confirmed (reused, not re-decided)

- **Single alembic head**: new migration `down_revision = '93511d5f7885'` (current head =
  `93511d5f7885_refresh_rules`); enforced by a new offline `test_migration_offline_rollups.py`
  asserting single head + `down_revision`, mirroring `tests/unit/test_strategy_single_head.py`.
- **UUIDv7** `id` via `Base` (`app_shared.ids.new_uuid7`); **Money** = `Numeric(18,4)` finite-only
  (`app_shared.money.Money`); **TZDateTime**/UTC everywhere (FR-025); **RLS** via
  `emit_rls_policy("variant_price_daily_rollups")` in the same migration (fail-closed
  `NULLIF(current_setting('app.workspace_id', true),'')::uuid`).
- **No scraping** anywhere in this feature (FR-003, Principle I/V): all new code in `app_shared`,
  `apps/workers`, `apps/scheduler` imports no Scrapy/Twisted/Playwright; import-boundary test extended.
</content>
</invoke>
