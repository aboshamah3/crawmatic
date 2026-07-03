# Contract: migration — partitioned observations + current prices

One repo-root Alembic migration `<rev>_observations_current_prices_tables.py`, `down_revision = a4f205e8d7de` (current head), single linear head preserved (CI head guard). FR-012, §22/§29, research D3.

## Upgrade

1. **`price_observations`** — create the parent as `PARTITION BY RANGE (scraped_at)` with composite `PRIMARY KEY (id, scraped_at)` and the §22 columns (SQLAlchemy emits the `PARTITION BY` from the model's `postgresql_partition_by`, or via explicit `op.execute` if the op builder needs it).
2. **`request_attempts`** — same, `PARTITION BY RANGE (created_at)`, `PRIMARY KEY (id, created_at)`.
3. **Initial partitions** — `op.execute` `CREATE TABLE <t>_YYYY_MM PARTITION OF <t> FOR VALUES FROM ('YYYY-MM-01') TO ('<next-month>-01')` for at least **current + next** month on **both** tables (a small in-migration helper computes month bounds). Naming `price_observations_2026_07`, etc. (< 63 bytes).
4. **`match_current_prices`** — create the current-state table with `unique(workspace_id, match_id)` (`uq_match_current_prices_workspace_id_match_id`).
5. **RLS** — `op.execute` each statement from `emit_rls_policy(...)` for **all three** tables (ENABLE + FORCE + fail-closed policy). Applying RLS to a partitioned **parent** propagates to its partitions; new partitions inherit it.

## Downgrade

Drop `match_current_prices`; drop each partition then the two partitioned parents (or `DROP TABLE ... CASCADE` on the parents, which removes partitions). Reverse order of creation.

## Tests

- **Offline render** (`alembic upgrade head --sql`, no DB): asserts `PARTITION BY RANGE (scraped_at)` / `(created_at)`, composite PKs, the initial `PARTITION OF` statements for current+next month, `unique(workspace_id, match_id)`, and the RLS DDL (ENABLE/FORCE/CREATE POLICY) for all three tables; single head.
- **Live** (skip without Postgres): apply/rollback; insert routes to the correct monthly partition; RLS denies a cross-workspace / no-context read.

## Convention established (first partitioned tables in the repo)

- Parent declared partitioned in the ORM via `__table_args__` `postgresql_partition_by`; partition key is a `primary_key=True` column so the composite PK satisfies Postgres's partitioned-table rule.
- Monthly partitions created in the migration for current + next month; ongoing partition creation + retention-by-drop is a later ops/retention spec (§29).
- Soft references (`observation_id`, `current_price_id`) carry no FK so dropped partitions dangle harmlessly.
