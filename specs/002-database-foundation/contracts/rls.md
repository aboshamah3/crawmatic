# Contract: RLS policy DDL emitter

Module: `app_shared/models/rls.py`.

## Exposed symbol

```python
def emit_rls_policy(
    table_name: str,
    *,
    workspace_column: str = "workspace_id",
    policy_name: str | None = None,   # defaults to f"{table_name}_workspace_isolation"
) -> tuple[str, ...]:
    ...
```

Returns DDL statement strings for use inside an Alembic migration (`for stmt in emit_rls_policy(...): op.execute(stmt)`), in the **same migration that creates the workspace-owned table** (§32).

## Emitted DDL (guarantee)

1. `ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;`
2. `ALTER TABLE {table} FORCE ROW LEVEL SECURITY;`  ← policy applies even to the table owner.
3. `CREATE POLICY {policy} ON {table} USING ({col} = NULLIF(current_setting('app.workspace_id', true), '')::uuid);`

## Semantics (guarantee)

- **Fail-closed on absent AND empty**: `current_setting('app.workspace_id', true)` returns `NULL` when the GUC is unset and `''` when set to empty. `NULLIF(…, '')` maps **both** to `NULL`, so `''::uuid` never raises and `col = NULL` is `NULL` (not true) → **zero rows** match (§32 "absent or empty … matches zero rows"). Never "all rows", never an error. The `NULLIF` wrapper is required: a bare `current_setting(...)::uuid` would raise `invalid input syntax for type uuid: ""` on an empty context instead of failing closed.
- **Pooler-safe context**: callers set context per transaction with `SET LOCAL app.workspace_id = '<uuid>'` (transaction-scoped; survives PgBouncer transaction pooling).

## Scope in SPEC-02

- Helper + `WorkspaceScopedBase` delivered and validated **statically** (rendered-DDL assertions). **No real workspace-owned table** is created here; first concrete application is SPEC-03.

## Tests

- `tests/unit/test_rls_policy.py` — asserts the rendered strings contain `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, and the fail-closed predicate `NULLIF(current_setting('app.workspace_id', true), '')::uuid` (explicitly asserts the `NULLIF(..., '')` wrapper is present so empty context cannot raise).
- (Live-DB, marked for a PG host) applying the policy to a throwaway table and confirming zero rows with **no** context and with an **empty** context, correct rows with `SET LOCAL`.
