# Implementation Plan: Retention, Rollups & Partition Maintenance

**Branch**: `015-retention-rollups-partition-maintenance` | **Date**: 2026-07-06 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/015-retention-rollups-partition-maintenance/spec.md`

## Summary

Deliver the three scheduled maintenance jobs that keep the already-partitioned append-heavy tables
healthy, plus the durable rollup table they enable. A code-level **partition registry** (`price_observations`
by `scraped_at`/90d/feeds-rollups, `request_attempts` by `created_at`/90d, `price_alert_events` by
`created_at`/365d, `webhook_events` by `created_at`/90d — absent until SPEC-16) drives all three jobs.
(1) A **partition-creation** job issues runtime `CREATE TABLE … PARTITION OF` DDL to ensure current +
next month exist for every *existing* registered table (self-healing, idempotent, correct across
Dec→Jan). (2) A **daily-rollup** job upserts one `variant_price_daily_rollups` row per (workspace,
variant, UTC day): competitor min/avg/max + comparable count aggregated from that day's
`price_observations` (comparable, same-currency only), with client price / currency / alert type read
(not recomputed) from the SPEC-09 `variant_price_states` surface. (3) A **retention** job drops whole
expired monthly partitions via `DROP TABLE` (never bulk DELETE), and for `price_observations` only
after a date-level verify that its covering rollups are complete. A **soft-reference tolerance** guard
(US4) confirms readers of `match_current_prices.observation_id` rely on denormalized fields once its
partition is dropped, plus an operator-visibility dangling-ref check.

Two findings materially shape this plan versus the spec's stated assumptions, both grounded in the
codebase (see [research.md](./research.md) R1/R2): **`variant_price_daily_rollups` does not exist yet
— SPEC-09 explicitly deferred it here** — so this feature creates that table (its one Alembic
migration); and **monthly partition create/drop is runtime DDL, not migrations** (matching the repo's
zero-runtime-partition-management starting point and §29's runtime-maintenance framing). All three
jobs are Celery `maintenance`-queue tasks enqueued by the existing scheduler loop on fixed cadences
(the `finalize_jobs`/`strategy_stats_flush` precedent), executing under the BYPASSRLS **system
session** (the sanctioned SPEC-13 cross-tenant seam) with app-level workspace scoping preserved on
every rollup write.

Full rationale in [research.md](./research.md); entities in [data-model.md](./data-model.md); behavior
in [contracts/](./contracts/); validation in [quickstart.md](./quickstart.md).

## Technical Context

**Language/Version**: Python 3.13 (uv workspace).

**Primary Dependencies**: SQLAlchemy 2.0 + Alembic (sync `Session`; hand-authored migration — no live
Postgres in build env), Celery + Redis (task enqueue via `app_shared.messaging.enqueue`), PostgreSQL
(RANGE partitions, runtime `CREATE/DROP TABLE … PARTITION OF`, RLS, `to_regclass`/`pg_catalog`). **No
new third-party dependency.** No Scrapy/Twisted/Playwright anywhere (FR-003, Principle I/V).

**Storage**: PostgreSQL via PgBouncer (transaction pooling). **One new table**
`variant_price_daily_rollups` (workspace-owned, RLS-forced, not partitioned). Reads
`price_observations`, `variant_price_states`; manages partitions of `price_observations`,
`request_attempts`, `price_alert_events` (+ `webhook_events` when it exists). Cross-tenant work uses
the existing `SYSTEM_DATABASE_URL` BYPASSRLS role — no new session infrastructure.

**Testing**: pytest. Pure-unit (no DB): partition month-bounds (Dec→Jan/Feb), retention eligibility
(whole-range-past-cutoff), rollup aggregation (currency filter, min/avg/max, Decimal, count), registry
+ existence gate, import-boundary. Offline alembic render + single-head test for the new migration.
Live-DB `*_live.py` (`skipif` probe, skip cleanly with no Postgres here): partition create/idempotence,
rollup upsert, retention drop + verify-before-drop, soft-ref tolerance, RLS on the new table.

**Target Platform**: Linux server, multi-service deploy. Jobs run in `worker-service`
(`apps/workers`), enqueued by `scheduler-service` (`apps/scheduler`). No API surface (no request path
— FR-003).

**Project Type**: Backend monorepo (uv workspace: `apps/*` + `libs/*`). No frontend (v1 backend-only).

**Performance Goals**: 2k products / 10k–20k matches per workspace, millions of raw rows/month
(Principle VIII). Retention keeps raw tables bounded by dropping whole partitions (no vacuum storm).
Rollup aggregation is a per-day, indexed scan of one month's partition; the `date`-indexed rollup table
keeps coverage/retention range scans cheap. Create-ahead runs daily → weeks of lead so next-month
always exists before the month begins (SC-001).

**Constraints**: partition-drop only, never bulk DELETE on raw tables (FR-015/SC-003); verify-before-
drop is a hard ordering guarantee (FR-016/SC-004); all jobs idempotent + concurrency-safe
(FR-006/020/SC-002); TIMESTAMPTZ/UTC for all bounds/cutoffs/dates (FR-025); Decimal/NUMERIC finite-only
money (FR-012); UUIDv7 PK, RLS from birth on the new table, single alembic head; runtime partition DDL
(not migrations) matching the established pattern.

**Scale/Scope**: 1 new table + migration; 1 new `app_shared/maintenance/` package (registry, partition
DDL, rollup aggregation, retention, soft-ref check); 1 new `apps/workers/tasks_maintenance.py` (3 tasks);
3 scheduler-loop cadence accumulators + task-name constants + celery route wiring; ~9 new `Settings`
knobs; a US4 read-path audit + tolerance check. No new API endpoints, no new dependency.

## Constitution Check

*GATE: evaluated against `.specify/memory/constitution.md` v1.0.1.*

| Principle | Assessment |
|---|---|
| I. API-First / Service-Oriented / scraping-free `app_shared` | **PASS.** New logic in `app_shared/maintenance` (scraping-free), executed in `apps/workers`, enqueued from `apps/scheduler`. No Scrapy/Twisted/Playwright import; import-boundary test extended. Maintenance jobs are explicitly **not** scraping (`libs/scrape-core` untouched). |
| II. Workspace Isolation (NON-NEGOTIABLE) | **PASS (1 justified deviation).** New `variant_price_daily_rollups` is workspace-owned: real `workspace_id` FK, `emit_rls_policy` in its first migration, registered in `WORKSPACE_OWNED_MODELS`, every aggregate filter + upsert carries `workspace_id` explicitly; cross-workspace RLS-denial test. Deviation: the inherently cross-tenant source scans (which workspaces had observations on day D; whole-partition coverage) run on the BYPASSRLS **system session** — app-level scoping is retained on every workspace-owned read/write, mirroring SPEC-13. See Complexity Tracking. |
| III. Variant-Level Pricing & Explicit Matching | **PASS.** Rollups are per **variant** per day; no matching/pricing recomputation — client price/alert type read from the SPEC-09 variant surface. |
| IV. Database-Driven Configuration | **PASS.** Retention windows + job cadences are env/DB-tunable `Settings` (never hardcoded literals); the partition-table set is a code constant because each entry is bound to a real schema object (no operational add path without a migration) — the honest boundary. |
| V. Disciplined Scraping Runtime (NON-NEGOTIABLE) | **PASS.** No spider, no reactor, no dispatch; jobs are off the request path and idempotent (FR-003/006/010/020). |
| VI. Internal-Only & Legally Compliant Access (NON-NEGOTIABLE) | **PASS.** No access-method, network, or legal-surface change. |
| VII. Monetary & Extraction Correctness | **PASS.** Rollup money uses `Money`/`NUMERIC(18,4)`, finite-only, exact Decimal aggregation (FR-012); currency-mismatched competitor prices excluded from min/avg/max + count (FR-011), reusing SPEC-09's persisted `comparable` flag — never comparing across currencies. |
| VIII. Scale-Safe Data & Concurrency | **PASS (embodies §29).** Monthly partition create-ahead + retention-by-drop (never bulk DELETE), hot reads from current-state surfaces, no hot-row writes, all traffic PgBouncer-safe (`to_regclass`/DDL on the system engine, `prepare_threshold=None`); idempotent + concurrency-safe via `IF [NOT] EXISTS` + upsert. |
| Tech & Security constraints | **PASS.** UUIDv7 PK, TIMESTAMPTZ/UTC, single alembic head (`down_revision='93511d5f7885'`), structured error/observability logging; no `/v1` surface (no API). Runtime partition DDL matches the established migration-time convention, not a stack substitution. |
| Workflow / scope discipline | **PASS.** Incremental spec, correctly sequenced after 07/09/13; no forbidden v1 scope. Corrects the spec's mistaken "rollup table already exists" assumption by building it here (where SPEC-09 deferred it). |

**Gate result: PASS** (one deviation documented and justified below). Re-checked after Phase 1 design —
unchanged (the new table + runtime-DDL jobs introduce no additional deviation).

## Project Structure

### Documentation (this feature)

```text
specs/015-retention-rollups-partition-maintenance/
├── plan.md              # This file
├── research.md          # Phase 0 — decisions R1–R10
├── data-model.md        # Phase 1 — rollup table + registry + partition/soft-ref entities
├── quickstart.md        # Phase 1 — validation scenarios (US1–US4 + SC map)
├── contracts/
│   ├── partition-creation.md          # US1 create-ahead job DDL
│   ├── daily-rollup.md                # US2 aggregation + upsert
│   ├── retention-drop.md              # US3 drop + verify-before-drop ordering
│   └── soft-reference-tolerance.md    # US4 dangling-ref reader guard + check
├── spec.md
└── tasks.md             # (later, /speckit-tasks)
```

### Source Code (repository root)

```text
libs/shared/app_shared/
├── models/
│   ├── rollups.py                 # NEW — VariantPriceDailyRollup (Base+WorkspaceScopedBase+TimestampMixin)
│   └── __init__.py                # EDIT — export VariantPriceDailyRollup
├── repository.py                  # EDIT — add VariantPriceDailyRollup to WORKSPACE_OWNED_MODELS
├── maintenance/                   # NEW package (scraping-free)
│   ├── __init__.py                # NEW
│   ├── registry.py                # NEW — PartitionedTable dataclass + PARTITIONED_TABLES + retention lookup
│   ├── partitions.py              # NEW — month bounds, to_regclass gate, create/drop DDL, catalog discovery
│   ├── rollups.py                 # NEW — run_daily_rollup aggregation + upsert
│   ├── retention.py               # NEW — eligibility, rollups_cover verify, run_retention
│   └── soft_refs.py               # NEW — dangling-soft-reference tolerance check (FR-022)
├── task_names.py                  # EDIT — MAINTENANCE_PARTITION_CREATE / _DAILY_ROLLUP / _RETENTION_DROP
└── config.py                      # EDIT — 5 retention-day + 3 interval + 1 lookahead knobs

apps/workers/app/workers/
├── tasks_maintenance.py           # NEW — 3 @app.task wrappers on get_system_session
└── celery_app.py                  # EDIT — include tasks_maintenance; route 3 names → maintenance queue

apps/scheduler/app/scheduler/
└── scheduler_app.py               # EDIT — 3 fixed-cadence accumulators enqueue the 3 maintenance tasks

apps/api/app/…                     # AUDIT ONLY (US4/FR-021) — confirm match_current_prices readers
                                   #   rely on denormalized fields, no hard join on observation_id

alembic/versions/
└── <rev>_variant_price_daily_rollups.py   # NEW — create table + unique + indexes + RLS; down_revision=93511d5f7885

docker-compose.yml                 # EDIT — scheduler service depends_on redis (it now enqueues more; latent gap)

tests/
├── unit/
│   ├── test_partition_bounds.py             # NEW — Dec→Jan/Feb, half-open bounds (FR-007)
│   ├── test_retention_eligibility.py        # NEW — whole-range<cutoff, per-window (FR-017/018)
│   ├── test_rollup_aggregation.py           # NEW — currency filter, min/avg/max, Decimal, count (FR-011/012/013)
│   ├── test_partition_registry.py           # NEW — registry shape + to_regclass gate (FR-001/002)
│   ├── test_migration_offline_rollups.py    # NEW — offline render, single head, down_revision (mirrors test_strategy_single_head)
│   └── test_import_boundaries.py            # EDIT — assert app_shared/maintenance + tasks_maintenance are scraping-free
└── integration/
    ├── test_partition_create_live.py        # NEW — create-ahead, idempotence, absent-table skip
    ├── test_daily_rollup_live.py            # NEW — upsert, currency exclusion, zero-competitor row, RLS denial
    ├── test_retention_drop_live.py          # NEW — drop vs skip-pending-rollups, age-only tables, no-double-drop
    └── test_soft_ref_tolerance_live.py      # NEW — dangling observation_id read succeeds; tolerance check (SC-007)
```

**Structure Decision**: Backend monorepo (uv workspace). All reusable job logic (registry, partition
DDL, rollup aggregation, retention, soft-ref check) lives in a new scraping-free
`libs/shared/app_shared/maintenance/` package (imported by the worker; honors Principle I). The three
Celery tasks live in `apps/workers`; the fixed-cadence enqueues extend the existing `apps/scheduler`
loop. The one new table + migration follow the `variant_price_states` / `2db33dea5e14` precedents.

## Complexity Tracking

| Deviation | Why needed | Simpler alternative rejected because |
|---|---|---|
| Maintenance tasks use the **BYPASSRLS** system session (`get_system_sessionmaker`, `SYSTEM_DATABASE_URL` → `AUTH_DATABASE_URL` fallback) | The rollup's "which workspaces/variants had observations on day D" scan and the retention "whole-partition date coverage" check are inherently cross-tenant — one query must see rows across **all** workspaces. Under `FORCE ROW LEVEL SECURITY` the ordinary pooler role with no `app.workspace_id` returns zero rows; partition `CREATE/DROP TABLE` DDL also needs an elevated role. App-level workspace scoping is fully preserved (every rollup aggregate filters `workspace_id=`, every upsert sets `workspace_id=` explicitly), so Principle II's mandatory control holds and RLS is the defense-in-depth layer a trusted system component legitimately bypasses — identical to the sanctioned SPEC-13 refresh-pass seam. | *Per-workspace loop with `set_workspace_context`* — the initial cross-tenant source scan still returns zero rows under forced RLS without a context, so a BYPASSRLS scan is unavoidable; one system session per task is simpler and matches the established precedent. *Grant the pooler role BYPASSRLS* — silently disables RLS platform-wide. |
| **One new bulk `DELETE`** — the 2-year age policy on the non-partitioned `variant_price_daily_rollups` | The rollup table is durable current-state-style summary data, deliberately **not** partitioned (data-model R5), so it has no partition to drop; a bounded `DELETE WHERE date < cutoff` is the only retention mechanism. | *Partition the rollup table too* — reintroduces the exact monthly-partition maintenance burden this spec bounds, for a small, slow-growing summary table that resembles the unpartitioned `variant_price_states`. SC-003's "0% bulk DELETE" targets the **raw append-heavy partitions**, which this table is not. |

No other deviations. Runtime partition DDL (vs migrations) is **not** a deviation — it is the correct,
established treatment of recurring monthly-partition lifecycle (research R2, §29); Alembic is used only
for the one new durable table.

---

## Phase 0 — Outline & Research

Complete → [research.md](./research.md). All Technical Context unknowns resolved: R1 (rollup table
must be created here — SPEC-09 deferred it); R2 (partition create/drop = runtime DDL, not migrations);
R3 (code-registry + env-tunable retention windows); R4 (`to_regclass` existence gate); R5 (rollup
table shape); R6 (rollup source = that day's `price_observations` + SPEC-09 client/alert surface, with
documented backfill limitation); R7 (retention windows + date-level verify-before-drop + rollup age
policy); R8 (scheduler-enqueue → worker-execute hosting); R9 (BYPASSRLS system session, scoping
preserved); R10 (the single dangling soft ref + tolerance). No `NEEDS CLARIFICATION` remain.

## Phase 1 — Design & Contracts

Complete. Artifacts: [data-model.md](./data-model.md),
[contracts/partition-creation.md](./contracts/partition-creation.md),
[contracts/daily-rollup.md](./contracts/daily-rollup.md),
[contracts/retention-drop.md](./contracts/retention-drop.md),
[contracts/soft-reference-tolerance.md](./contracts/soft-reference-tolerance.md),
[quickstart.md](./quickstart.md).

**Agent context update**: skipped — this project does not use GitHub Copilot
(`.github/copilot-instructions.md` was removed; the `after_plan` agent-context hook is `enabled: false`
in `.specify/extensions.yml`, and user memory records "No GitHub Copilot"). No agent context file to
update.

## Phase 2

Task generation is performed by `/speckit-tasks` (not this command).
</content>
