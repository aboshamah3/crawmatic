# Phase 1 Data Model: Current Prices & Alert Logic

Three new tables in `libs/shared/app_shared/models/alerts.py`, exact PROJECT_SPEC §22
shapes. All **workspace-owned** (`WorkspaceScopedBase` — `workspace_id NOT NULL`, indexed,
real FK → `workspaces.id`), added to `app_shared.repository.WORKSPACE_OWNED_MODELS`, and
given `emit_rls_policy` (ENABLE + FORCE + fail-closed) in the creating Alembic migration
(not here — the model module only declares ORM shape, matching `models/observations.py`).
Enum-like columns use `enum_column` (app-validated `VARCHAR`, never a DB-native enum).
Money is `Money()` → `NUMERIC(18,4)`. Timestamps are `TZDateTime` (`TIMESTAMPTZ`, naive
rejected). Soft references (`product_id`, `product_variant_id`, `alert_state_id`,
`latest_alert_state_id`) are plain indexed UUIDs with **no FK** — the §22 soft-reference
philosophy (tolerate retention-by-drop dangling; the current-state row carries every field
readers need).

New enums added to `app_shared.enums` (all `StrEnum` → `VARCHAR`):

- `AlertType`: `NO_COMPETITOR_DATA, RISK, HIGH_PRICE, CHANCE_TO_INCREASE_PRICE, NORMAL,
  CLOSE_TO_COMPETITORS`
- `AlertSeverity`: `NONE, LOW, MEDIUM, HIGH, CRITICAL`
- `AlertStatus`: `ACTIVE, RESOLVED`
- `AlertEventType`: `CREATED, UPDATED, RESOLVED, REOPENED, UNCHANGED`

Severity is **derived from type** (FR-011), never stored independently of the map:

| AlertType | AlertSeverity |
|---|---|
| NO_COMPETITOR_DATA | LOW |
| RISK | CRITICAL |
| HIGH_PRICE | HIGH |
| CHANCE_TO_INCREASE_PRICE | MEDIUM |
| NORMAL | NONE |
| CLOSE_TO_COMPETITORS | MEDIUM |

---

## Entity: VariantPriceState (`variant_price_states`) — current price position

The current computed pricing position of one variant vs its comparable competitors. One row
per workspace+variant; upserted by `price_analysis`. **Not partitioned; single-column PK.**
Uses `TimestampMixin` (adds `created_at` + `updated_at`; `created_at` supports the shared
cursor even though no list endpoint pages this table today — D10).

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | UUID (uuidv7) | no | PK |
| `workspace_id` | UUID | no | indexed; FK → `workspaces.id`; RLS column; part of unique |
| `product_id` | UUID | no | soft ref (no FK) |
| `product_variant_id` | UUID | no | soft ref (no FK); part of unique |
| `client_price` | `NUMERIC(18,4)` | no | the variant's own `current_price` at analysis time |
| `currency` | `CHAR(3)` | no | the variant's `currency` at analysis time |
| `cheapest_competitor_price` | `NUMERIC(18,4)` | yes | `min(comparable)`; `NULL` when count 0 |
| `average_competitor_price` | `NUMERIC(18,4)` | yes | `avg(comparable)`; `NULL` when count 0 |
| `highest_competitor_price` | `NUMERIC(18,4)` | yes | `max(comparable)`; `NULL` when count 0 |
| `comparable_competitor_count` | INT | no | number of included competitors; `0` allowed |
| `latest_alert_type` | `AlertType` VARCHAR | no | last computed type |
| `latest_alert_severity` | `AlertSeverity` VARCHAR | no | last computed severity (from map) |
| `latest_alert_state_id` | UUID | yes | soft ref → `variant_alert_states.id` (no FK) |
| `calculated_at` | TIMESTAMPTZ | no | when the analysis last ran |
| `created_at` | TIMESTAMPTZ | no | `TimestampMixin`; row birth |
| `updated_at` | TIMESTAMPTZ | no | `TimestampMixin`; last upsert |

- **Unique**: `unique(workspace_id, product_variant_id)` — the upsert conflict arbiter.
- **Index**: `workspace_id` (RLS/scoping); `(workspace_id, product_variant_id)` via unique.

## Entity: VariantAlertState (`variant_alert_states`) — current alert

The current alert for one variant: type, severity, status, the prices that justified it, a
human message, lifecycle timestamps. One row per workspace+variant; upserted by
`price_analysis`. **Not partitioned; single-column PK.** Uses `TimestampMixin`.

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | UUID (uuidv7) | no | PK; referenced (soft) by events' `alert_state_id` and price-state's `latest_alert_state_id` |
| `workspace_id` | UUID | no | indexed; FK → `workspaces.id`; RLS; part of unique |
| `product_id` | UUID | no | soft ref |
| `product_variant_id` | UUID | no | soft ref; part of unique |
| `type` | `AlertType` VARCHAR | no | current alert type |
| `severity` | `AlertSeverity` VARCHAR | no | current severity (from map) |
| `status` | `AlertStatus` VARCHAR | no | `ACTIVE` while type non-NORMAL; `RESOLVED` (with `resolved_at`) when back to NORMAL/NONE |
| `client_price` | `NUMERIC(18,4)` | no | client price at this alert |
| `benchmark_price` | `NUMERIC(18,4)` | yes | the competitor benchmark that justified the type (e.g. highest for RISK, cheapest for HIGH_PRICE); `NULL` for NO_COMPETITOR_DATA |
| `cheapest_competitor_price` | `NUMERIC(18,4)` | yes | snapshot; `NULL` when count 0 |
| `average_competitor_price` | `NUMERIC(18,4)` | yes | snapshot; `NULL` when count 0 |
| `message` | TEXT | no | human-readable summary |
| `details` | JSONB | yes | structured context (counts, discount_vs_average, mismatched competitor ids) |
| `first_seen_at` | TIMESTAMPTZ | no | when this (non-NORMAL) alert first appeared; preserved across UPDATED, reset on REOPENED |
| `last_seen_at` | TIMESTAMPTZ | no | advanced on every analysis (incl. UNCHANGED) |
| `resolved_at` | TIMESTAMPTZ | yes | set on RESOLVED; cleared on REOPENED |
| `created_at` | TIMESTAMPTZ | no | `TimestampMixin`; row birth (stable cursor key) |
| `updated_at` | TIMESTAMPTZ | no | `TimestampMixin`; last upsert |

- **Unique**: `unique(workspace_id, product_variant_id)` — the upsert conflict arbiter.
- **Index**: `workspace_id`; optionally `(workspace_id, type)` / `(workspace_id, severity)`
  to support the `alerts/current` filters (advisory; add if needed).

**Status/lifecycle rules** (driven by the engine transition, D5 in research):
`ACTIVE` iff `type` is non-NORMAL; `RESOLVED` iff `type` ∈ {NORMAL} (with `resolved_at`
stamped). `first_seen_at` set when the alert first becomes non-NORMAL (CREATED/REOPENED),
carried through UPDATED; `last_seen_at` always advanced; `resolved_at` set on RESOLVED,
cleared on REOPENED.

## Entity: PriceAlertEvent (`price_alert_events`) — append-only history, PARTITIONED

An immutable record of each alert transition. **Monthly-partitioned by `created_at` from
birth**; composite `PRIMARY KEY (id, created_at)`. One row written **only** when the alert
type or severity changes (CREATED / UPDATED / RESOLVED / REOPENED); UNCHANGED is never
persisted. Many rows per variant over time.

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | UUID (uuidv7) | no | PK part 1 |
| `created_at` | TIMESTAMPTZ | no | **PK part 2 = partition key**; event time (cursor key) |
| `workspace_id` | UUID | no | indexed; FK → `workspaces.id`; RLS |
| `product_id` | UUID | no | soft ref |
| `product_variant_id` | UUID | no | soft ref; indexed (alert-events `?variant_id` filter) |
| `alert_state_id` | UUID | no | soft ref → `variant_alert_states.id` (no FK) |
| `event_type` | `AlertEventType` VARCHAR | no | CREATED / UPDATED / RESOLVED / REOPENED (never UNCHANGED) |
| `previous_type` | `AlertType` VARCHAR | yes | `NULL` for CREATED |
| `new_type` | `AlertType` VARCHAR | no | the resulting type |
| `previous_severity` | `AlertSeverity` VARCHAR | yes | `NULL` for CREATED |
| `new_severity` | `AlertSeverity` VARCHAR | no | the resulting severity |
| `message` | TEXT | no | human-readable transition summary |
| `details` | JSONB | yes | structured context |

- **PK**: `(id, created_at)` (partition rule).
- **Partition**: `PARTITION BY RANGE (created_at)`; initial current + next-month partitions
  created in the migration (SPEC-07 `_month_partition_bounds` pattern).
- **Index**: `workspace_id`; `product_variant_id` (variant filter). Partition-local indexes
  are created on the parent and propagate (Postgres 11+ declarative partitioning).

---

## Registration & metadata

- `libs/shared/app_shared/models/alerts.py` declares the three classes on
  `Base, WorkspaceScopedBase` (+ `TimestampMixin` on the two current-state tables).
- `libs/shared/app_shared/models/__init__.py` imports and re-exports
  `VariantPriceState, VariantAlertState, PriceAlertEvent` (so `Base.metadata` /
  `target_metadata` sees them and callers can `from app_shared.models import …`).
- `libs/shared/app_shared/repository.py` adds all three to `WORKSPACE_OWNED_MODELS` (so
  `scoped_select`/`scoped_get` refuse an unscoped query and the RLS invariant is enforced).

## Relationships (all soft, no FK except `workspace_id`)

```text
product_variants (1) ──< variant_price_states (1)      unique(ws, variant)
product_variants (1) ──< variant_alert_states (1)      unique(ws, variant)
variant_alert_states (1) ──< price_alert_events (N)    events.alert_state_id (soft)
variant_price_states.latest_alert_state_id ─→ variant_alert_states.id  (soft)
match_current_prices (N, comparable) ──> engine inputs for a variant   (read-only)
product_variants.current_price/currency ──> engine client_price/currency (read-only)
```

## State transitions (VariantAlertState.status × event)

```text
(no state) ──analysis NORMAL/NONE──────────────→ (state, ACTIVE? no — NORMAL) , no event
(no state) ──analysis non-NORMAL──────────────→ ACTIVE,  CREATED
ACTIVE     ──same (type,sev)───────────────────→ ACTIVE,  (UNCHANGED, not persisted), last_seen_at++
ACTIVE     ──different non-NORMAL──────────────→ ACTIVE,  UPDATED
ACTIVE     ──NORMAL/NONE────────────────────────→ RESOLVED (resolved_at set), RESOLVED
RESOLVED   ──non-NORMAL (had history)──────────→ ACTIVE (resolved_at cleared, first_seen_at reset), REOPENED
```
