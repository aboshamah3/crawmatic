# Contract: ORM models — `app_shared/models/alerts.py`

Declares the three SPEC-09 tables. **Shape only** — RLS + partitions live in the migration
(`contracts/migration-alerts.md`). Mirrors `models/observations.py` conventions exactly.

## Imports / bases

```python
from app_shared.enums import AlertType, AlertSeverity, AlertStatus, AlertEventType, enum_column
from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase
from app_shared.money import Money
```

- Both current-state tables: `class X(Base, WorkspaceScopedBase, TimestampMixin)`.
- Events table: `class PriceAlertEvent(Base, WorkspaceScopedBase)` (no `TimestampMixin` —
  append-only, explicit `created_at` PK part, like `PriceObservation.scraped_at`).

## `VariantPriceState`

- `__tablename__ = "variant_price_states"`.
- `__table_args__`: `UniqueConstraint("workspace_id", "product_variant_id",
  name="uq_variant_price_states_workspace_id_product_variant_id")`,
  `ForeignKeyConstraint(["workspace_id"], ["workspaces.id"],
  name="fk_variant_price_states_workspace_id_workspaces")`.
- Columns per data-model.md: `product_id`, `product_variant_id` (`Uuid`, not null);
  `client_price` (`Money()`, not null), `currency` (`CHAR(3)`, not null);
  `cheapest/average/highest_competitor_price` (`Money()`, nullable);
  `comparable_competitor_count` (`Integer`, not null);
  `latest_alert_type` (`enum_column(AlertType, nullable=False)`),
  `latest_alert_severity` (`enum_column(AlertSeverity, nullable=False)`),
  `latest_alert_state_id` (`Uuid`, nullable); `calculated_at` (`TZDateTime()`, not null).
  `created_at`/`updated_at` from `TimestampMixin`.

## `VariantAlertState`

- `__tablename__ = "variant_alert_states"`.
- `__table_args__`: `UniqueConstraint("workspace_id", "product_variant_id",
  name="uq_variant_alert_states_workspace_id_product_variant_id")` + workspace FK.
- Columns: `product_id`, `product_variant_id`; `type`/`severity`/`status` via `enum_column`
  (not null); `client_price` (`Money()`, not null); `benchmark_price`,
  `cheapest_competitor_price`, `average_competitor_price` (`Money()`, nullable);
  `message` (`Text`, not null); `details` (`JSONB`, nullable);
  `first_seen_at`, `last_seen_at` (`TZDateTime()`, not null), `resolved_at`
  (`TZDateTime()`, nullable). `created_at`/`updated_at` from `TimestampMixin`.

## `PriceAlertEvent` (PARTITIONED)

- `__tablename__ = "price_alert_events"`.
- `__table_args__`: `ForeignKeyConstraint(["workspace_id"], ["workspaces.id"],
  name="fk_price_alert_events_workspace_id_workspaces")`,
  `{"postgresql_partition_by": "RANGE (created_at)"}`.
- `created_at: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True)` — **PK part
  2 = partition key** (alongside inherited `id`), yielding composite `PRIMARY KEY
  (id, created_at)` — identical to `PriceObservation`.
- Columns: `product_id`, `product_variant_id` (`Uuid`, not null; `product_variant_id`
  indexed), `alert_state_id` (`Uuid`, not null); `event_type`
  (`enum_column(AlertEventType, nullable=False)`); `previous_type`
  (`enum_column(AlertType, nullable=True)`), `new_type`
  (`enum_column(AlertType, nullable=False)`), `previous_severity`
  (`enum_column(AlertSeverity, nullable=True)`), `new_severity`
  (`enum_column(AlertSeverity, nullable=False)`); `message` (`Text`, not null); `details`
  (`JSONB`, nullable).

## JSONB

Use `sqlalchemy.dialects.postgresql.JSONB` for `details` (matches repo usage of JSONB for
`api_keys.scopes` / `option_values`).

## Registration

- `models/__init__.py`: `from app_shared.models.alerts import (PriceAlertEvent,
  VariantAlertState, VariantPriceState)`; add to `__all__`.
- `repository.py`: add `VariantPriceState, VariantAlertState, PriceAlertEvent` to
  `WORKSPACE_OWNED_MODELS`.

## Acceptance

- Importing the module registers exactly three tables on `Base.metadata` with the names
  above; `PriceAlertEvent.__table__` renders `PARTITION BY RANGE (created_at)` and a 2-col
  PK; both current-state tables render `unique(workspace_id, product_variant_id)`.
- All three are in `WORKSPACE_OWNED_MODELS` (a `scoped_select` without `workspace_id`
  raises).
