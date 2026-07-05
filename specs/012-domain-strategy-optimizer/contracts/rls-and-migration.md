# Contract: RLS emitter + Alembic migration (FR-026, FR-027, FR-028, SC-005)

## New RLS emitter — `app_shared/models/rls.py::emit_fk_transitive_rls_policy`

```python
def emit_fk_transitive_rls_policy(
    table_name: str,
    *,
    parent_table: str,
    fk_column: str,
    parent_pk: str = "id",
    workspace_column: str = "workspace_id",
    policy_name: str | None = None,
) -> tuple[str, ...]:
    ...
```

Returns exactly three statements (mirrors `emit_rls_policy`'s ENABLE/FORCE/CREATE shape):

1. `ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;`
2. `ALTER TABLE {table} FORCE ROW LEVEL SECURITY;`
3. ```sql
   CREATE POLICY {policy} ON {table}
     USING (EXISTS (SELECT 1 FROM {parent_table} p
                     WHERE p.{parent_pk} = {table}.{fk_column}
                       AND p.{workspace_column}
                           = NULLIF(current_setting('app.workspace_id', true), '')::uuid));
   ```

**Fail-closed** (same `NULLIF(current_setting('app.workspace_id', true), '')::uuid` guard as
`emit_rls_policy`): with no workspace context the inner predicate is never true → **zero rows**, never an
error, never all rows (SC-005). This is the isolation mechanism for `strategy_attempt_stats`, which has
**no** `workspace_id` column of its own (§22) and is isolated transitively via its
`domain_strategy_profile_id` FK to the workspace-scoped parent (FR-026, research D3). Delivered with
rendered-DDL string unit tests (the SPEC-02 `test_rls_policy.py` precedent) plus a skip-clean live
zero-rows integration test.

## Migration — `alembic/versions/<rev>_domain_strategy_optimizer_tables.py`

- `down_revision = "851220acab90"` (current single head — SPEC-10; SPEC-11 added no migration).
  Keeps `scripts/check_single_head.sh` green (one head, SC / Constitution workflow).
- Hand-authored (no live Postgres in this build env — SPEC-05..10 precedent), reproducing the three ORM
  shapes from `app_shared/models/strategy.py` exactly (columns/types/nullability/defaults per data-model
  §2–§4).
- Creates, in order: `domain_strategy_profiles`, `strategy_attempt_stats`, `strategy_discovery_runs`.
- Real FKs: `workspace_id → workspaces.id` on the two workspace-owned tables; composite
  `(workspace_id, competitor_id) → competitors(workspace_id, id)` on profiles and runs;
  `domain_strategy_profile_id → domain_strategy_profiles.id` on stats. Explicit short constraint names
  (≤63 bytes) where the convention would overflow (the `cpm`/`dsp`/`sas` shorthand precedent).
- Unique: `uq_dsp_ws_competitor_domain_pattern` on profiles; `uq_sas_profile_method_type_name` on stats.
- Indexes per data-model (workspace_id; the version-guarded consumption lookup index; FK indexes).
- **RLS in the SAME migration** (§32, every prior spec's precedent):
  ```python
  for s in emit_rls_policy("domain_strategy_profiles"): op.execute(s)
  for s in emit_fk_transitive_rls_policy(
      "strategy_attempt_stats", parent_table="domain_strategy_profiles",
      fk_column="domain_strategy_profile_id"): op.execute(s)
  for s in emit_rls_policy("strategy_discovery_runs"): op.execute(s)
  ```
- `downgrade()` drops the three tables in reverse creation order.
- **Not partitioned** — these are the rolled-up learned layer, not append-heavy audit (§29, spec
  Assumptions); no `postgresql_partition_by`, no partition-key-in-PK.

## Registry wiring

- `domain_strategy_profiles` and `strategy_discovery_runs` → added to
  `app_shared.repository.WORKSPACE_OWNED_MODELS` (scoped querying + the `check_workspace_scoping.py` CI
  guard).
- `strategy_attempt_stats` → **excluded** (no `workspace_id` column); queried only via
  `app_shared/strategy/repository.py` joined to its scoped parent profile (the SPEC-10 dual-scope
  exclusion precedent, documented inline).
