# Contract: Alembic migration — `<newrev>_alerts_price_states_tables.py`

Hand-authored (no live Postgres in the build env), reproducing the ORM shapes and the
SPEC-07 partitioned-table precedent verbatim.

## Chain

- `down_revision = "a6b0234cd4ad"` — the **current single head** (SPEC-08
  `scrape_jobs_targets_tables`). After this migration the chain **still has exactly one
  head** (FR-006). Verify with `alembic heads` (integration/skip-clean) and by the
  down_revision-graph unit check used in prior specs.
- New `revision` = a fresh 12-hex id.

## `upgrade()`

Order (create current-state tables first, then the partitioned parent + children, then RLS):

1. `op.create_table("variant_price_states", …)` — columns per `models-alerts.md`; single-col
   `PrimaryKeyConstraint("id", name="pk_variant_price_states")`;
   `UniqueConstraint("workspace_id", "product_variant_id",
   name="uq_variant_price_states_workspace_id_product_variant_id")`; workspace FK. Then
   `op.create_index("ix_variant_price_states_workspace_id", …, ["workspace_id"])`.
2. `op.create_table("variant_alert_states", …)` — same pattern; unique on
   `(workspace_id, product_variant_id)`; workspace FK; `ix_..._workspace_id`. Optional
   `ix_variant_alert_states_workspace_id_type` / `_severity` for the list filters.
3. `op.create_table("price_alert_events", …, postgresql_partition_by="RANGE (created_at)")`
   — columns per `models-alerts.md`; `PrimaryKeyConstraint("id", "created_at",
   name="pk_price_alert_events")`; workspace FK.
   `op.create_index("ix_price_alert_events_workspace_id", …, ["workspace_id"])` and
   `op.create_index("ix_price_alert_events_product_variant_id", …,
   ["product_variant_id"])`.
   Then create **current + next month** partitions via raw SQL (copy
   `_month_partition_bounds(now)` from `2db33dea5e14_observations_current_prices_tables.py`):

   ```python
   for suffix, start, end in _month_partition_bounds(now):
       op.execute(
           f"CREATE TABLE price_alert_events_{suffix} PARTITION OF price_alert_events "
           f"FOR VALUES FROM ('{start}') TO ('{end}');"
       )
   ```

4. RLS in the **same** migration (FR-005, §32, Principle II) — emit once per table; the
   partitioned parent's policy propagates to its partitions:

   ```python
   for table in ("variant_price_states", "variant_alert_states", "price_alert_events"):
       for statement in emit_rls_policy(table):
           op.execute(statement)
   ```

## `downgrade()`

Reverse order: drop the two current-state tables; drop each `price_alert_events_{suffix}`
partition (`DROP TABLE IF EXISTS …`) then `op.drop_table("price_alert_events")`.

## Type notes (match the SPEC-07 migration)

- Money → `sa.Numeric(precision=18, scale=4)`; enums → `sa.String(length=32)`;
  `currency` → `sa.CHAR(length=3)`; timestamps → `sa.DateTime(timezone=True)`;
  `details` → `postgresql.JSONB`; ids → `sa.Uuid(as_uuid=True)`.
- `created_at`/`updated_at` on the current-state tables are explicit
  `sa.DateTime(timezone=True), nullable=False` columns (TimestampMixin's columns rendered
  literally, as the SPEC-07 migration does for `match_current_prices`).

## Acceptance

- `alembic upgrade head` (skip-clean integration) creates all three tables + the two initial
  event partitions; `alembic heads` shows one head; `downgrade` is clean.
- RLS present + FORCED on all three; a query with no `app.workspace_id` GUC returns zero
  rows from each.
