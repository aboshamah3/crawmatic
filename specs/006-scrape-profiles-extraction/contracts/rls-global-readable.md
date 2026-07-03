# Contract: `emit_global_readable_rls_policy` (`app_shared/models/rls.py`, extend)

A second RLS emitter for **dual-scope** tables (global rows readable by all, writable by none via the tenant path). The existing `emit_rls_policy` is untouched.

## Signature

```python
def emit_global_readable_rls_policy(
    table_name: str,
    *,
    workspace_column: str = "workspace_id",
) -> tuple[str, ...]:
```

Returns exactly four DDL statements, in order:

1. `ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;`
2. `ALTER TABLE {t} FORCE ROW LEVEL SECURITY;`
3. `CREATE POLICY {t}_workspace_read ON {t} FOR SELECT USING ({col} IS NULL OR {col} = NULLIF(current_setting('app.workspace_id', true), '')::uuid);`
4. `CREATE POLICY {t}_workspace_write ON {t} FOR ALL USING ({col} = NULLIF(current_setting('app.workspace_id', true), '')::uuid) WITH CHECK ({col} = NULLIF(current_setting('app.workspace_id', true), '')::uuid);`

## Semantics

- **SELECT**: own rows (`{col} = ctx`) **or** any global row (`{col} IS NULL`). With no context set, `ctx` is NULL → own rows fail closed (0 rows) but global rows remain visible via the `IS NULL` disjunct (FR-013/FR-016 terminal fallback readable by all).
- **INSERT**: only the `FOR ALL` `WITH CHECK` applies → `workspace_id` must equal the context; a tenant cannot insert a global (`NULL`) row (FR-021).
- **UPDATE/DELETE**: `FOR ALL` `USING ({col} = ctx)` → a tenant can mutate only its own rows, never a global one (FR-021).
- Fail-closed `NULLIF(current_setting(..., true), '')::uuid` preserved from the SPEC-02 emitter (unset/empty GUC → NULL, never an error).

## Pure

No execution — callers `op.execute(stmt)` each returned statement in the same migration that creates the table.

## Tests (unit, no DB)

- Renders exactly four statements in order.
- Read policy contains `IS NULL OR` and `FOR SELECT`; write policy is `FOR ALL` with both `USING` and `WITH CHECK` on `= ctx` (no `IS NULL`).
- Fail-closed `NULLIF(..., '')::uuid` present in both policies.
- `workspace_column` override reflected in all four statements.
