# Phase 1 Data Model: Retention, Rollups & Partition Maintenance

**Feature**: SPEC-15 | **Date**: 2026-07-06 | **Spec**: [spec.md](./spec.md) | **Research**: [research.md](./research.md)

This feature adds **one new table** (`variant_price_daily_rollups`) and **one code-level registry**
(no table). It does **not** create or alter the append-heavy parent tables — they already exist
(`price_observations`, `request_attempts`, `price_alert_events`). Monthly partitions of those parents
are created/dropped at **runtime** (R2), not via schema migrations.

---

## 1. `variant_price_daily_rollups` — NEW durable daily summary (US2)

Workspace-owned, **not partitioned** (durable 2-year summary; current-state convention). ORM in a new
`libs/shared/app_shared/models/rollups.py` (`Base + WorkspaceScopedBase + TimestampMixin`), exported
from `models/__init__.py`, registered in `WORKSPACE_OWNED_MODELS`
(`libs/shared/app_shared/repository.py`). Closest template: `VariantPriceState`
(`libs/shared/app_shared/models/alerts.py:56-95`) — reuse its field vocabulary verbatim.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `Uuid` (UUIDv7) | no | PK, from `Base` (`default=new_uuid7`) |
| `workspace_id` | `Uuid` | no | **FK → workspaces.id** (RLS anchor); indexed |
| `product_id` | `Uuid` | no | soft ref (no FK) |
| `product_variant_id` | `Uuid` | no | soft ref (no FK); part of unique key |
| `date` | `Date` | no | UTC calendar date = `price_observations.scraped_at::date` (clarification) |
| `currency` | `CHAR(3)` | no | client variant currency (aggregation is same-currency only) |
| `client_price` | `Money` (`Numeric(18,4)`) | no | from `variant_price_states.client_price` (FR-009, FR-013) |
| `cheapest_competitor_price` | `Money` | yes | min comparable same-currency competitor price that day (NULL if count 0) |
| `average_competitor_price` | `Money` | yes | avg comparable same-currency competitor price (exact `Decimal`, FR-012) |
| `highest_competitor_price` | `Money` | yes | max comparable same-currency competitor price |
| `comparable_competitor_count` | `Integer` | no | count of included prices; `0` allowed (FR-013) |
| `latest_alert_type` | `enum_column(AlertType)` → `String(32)` | no | from `variant_price_states.latest_alert_type` |
| `created_at` / `updated_at` | `TZDateTime` (UTC) | no | `TimestampMixin` |

**Constraints & indexes**
- `PrimaryKeyConstraint("id")` — single-column (not partitioned).
- `UniqueConstraint("workspace_id", "product_variant_id", "date",
  name="uq_variant_price_daily_rollups_workspace_id_product_variant_id_date")` — the **upsert conflict
  arbiter** (FR-010; `ON CONFLICT … DO UPDATE`).
- `ForeignKeyConstraint(["workspace_id"], ["workspaces.id"],
  name="fk_variant_price_daily_rollups_workspace_id_workspaces")`.
- `Index("ix_variant_price_daily_rollups_workspace_id", "workspace_id")`.
- `Index("ix_variant_price_daily_rollups_date", "date")` — supports the retention/coverage
  `WHERE date >= d0 AND date < dN` range scans (R7).

**Migration** (`alembic/versions/<rev>_variant_price_daily_rollups.py`, `down_revision='93511d5f7885'`):
`op.create_table(...)` + the two indexes + `for stmt in emit_rls_policy("variant_price_daily_rollups"):
op.execute(stmt)` — RLS ENABLE+FORCE+fail-closed policy in the **same** migration (§32, Principle II).
Hand-authored (no live Postgres in build env), mirroring `2db33dea5e14`. Single-head guarded by a new
offline test.

**Validation rules**
- All money via `Money` (`float`/non-finite rejected at bind — FR-012).
- `date` is a UTC calendar date; all bound/cutoff datetimes are tz-aware UTC (FR-025).
- Upsert (never blind insert) on the unique key (FR-010, US2 AS-2).
- One row per `(workspace_id, product_variant_id, date)` (SC-006).

---

## 2. Partitioned-table registry entry — code constant (US1/US3), NOT a table

New scraping-free module `libs/shared/app_shared/maintenance/registry.py`. A frozen dataclass +
module-level tuple; retention windows read from `Settings` (R3).

```
@dataclass(frozen=True)
class PartitionedTable:
    name: str                 # e.g. "price_observations"
    partition_key: str        # e.g. "scraped_at"  (the RANGE column)
    feeds_rollups: bool       # True only for price_observations -> verify-before-drop
    retention_setting: str    # Settings attr name, e.g. "RETENTION_PRICE_OBSERVATIONS_DAYS"
```

| name | partition_key | feeds_rollups | retention default (days) |
|---|---|---|---|
| `price_observations` | `scraped_at` | **True** | 90 |
| `request_attempts` | `created_at` | False | 90 |
| `price_alert_events` | `created_at` | False | 365 |
| `webhook_events` | `created_at` | False | 90 (table absent until SPEC-16 — skipped, FR-002) |

`variant_price_daily_rollups` is **not** in this registry (not partitioned); its 2-year retention is a
separate age-based row policy (R7), keyed off `RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS=730`.

---

## 3. Monthly partition (runtime-managed physical child) — US1/US3

Not an ORM entity; a physical child table of a registered parent, created/dropped by job DDL.
- **Name**: `{parent}_{YYYY}_{MM}` (e.g. `price_observations_2026_08`).
- **Bounds**: half-open `[YYYY-MM-01, <next-month>-01)` in UTC (mirrors migration `_month_partition_bounds`).
- **Create**: `CREATE TABLE {name} PARTITION OF {parent} FOR VALUES FROM ('{start}') TO ('{end}')` —
  idempotent via `IF NOT EXISTS` / catalog pre-check (FR-006). RLS inherited from parent (no extra DDL).
- **Drop**: `DROP TABLE {name}` (partition-drop, never bulk DELETE — FR-015). Idempotent via
  `IF EXISTS` (FR-020).
- **Discovery**: existing partitions/bounds read from `pg_catalog`
  (`pg_inherits` + `pg_get_expr(relpartbound)`), or derived from the `{YYYY}_{MM}` name; existence of
  the parent via `to_regclass` (R4).

**State/lifecycle**: `absent → created (in advance) → live (accepting writes for its month) →
expired (whole range < cutoff) → [rollup-verified if feeds_rollups] → dropped`.

---

## 4. Soft reference (existing, tolerated) — US4

`match_current_prices.observation_id` (`observations.py:186`): plain nullable `Uuid`, **no FK**, into
`price_observations.id`. After the target's partition is dropped, the id dangles. No schema change —
readers rely on `match_current_prices`' denormalized fields (`price`, `currency`, `comparable`,
`stock_status`, `scraped_at`, `success`, …). SPEC-15 adds a read-time/maintenance tolerance check
(FR-021/FR-022), not a constraint.

---

## 5. Maintenance run report — structured log record (FR-023)

Not a table (v1). Each task emits one structured JSON summary per run (§31 observability):
`{"job": "partition_create|daily_rollup|retention_drop", "partitions_created": [...],
"tables_skipped_absent": [...], "rollups_upserted": N, "partitions_dropped": [...],
"partitions_skipped_pending_rollups": [...], "dangling_soft_refs_tolerated": N}`. Emitted via the
standard logger, best-effort (never blocks/fails the core guarantee — FR-024).

---

## 6. New `Settings` knobs (`libs/shared/app_shared/config.py`, Principle IV)

| Setting | Default | Purpose |
|---|---|---|
| `RETENTION_PRICE_OBSERVATIONS_DAYS` | `90` | FR-017 |
| `RETENTION_REQUEST_ATTEMPTS_DAYS` | `90` | FR-017 |
| `RETENTION_PRICE_ALERT_EVENTS_DAYS` | `365` | FR-017 |
| `RETENTION_WEBHOOK_EVENTS_DAYS` | `90` | FR-017 |
| `RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS` | `730` | FR-017 (rollup age policy) |
| `PARTITION_CREATE_INTERVAL_SECONDS` | `86400` | US1 cadence (daily → weeks of lead, SC-001) |
| `DAILY_ROLLUP_INTERVAL_SECONDS` | `86400` | US2 cadence |
| `RETENTION_INTERVAL_SECONDS` | `86400` | US3 cadence |
| `PARTITION_CREATE_LOOKAHEAD_MONTHS` | `1` | how far ahead to create (current + next, FR-004) |

Reuses existing `SYSTEM_DATABASE_URL` (BYPASSRLS system role, R9) — no new session infra.
</content>
