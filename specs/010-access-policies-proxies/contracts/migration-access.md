# Contract: Alembic migration `<newrev>_access_policies_proxies_tables.py`

Hand-authored (no live Postgres in this build env), reproducing the model column/constraint
shapes exactly. Chains onto the current head.

```
revision = "<newrev>"
down_revision = "e4a75b48360c"   # current head (alerts/price-states)
```

## upgrade()

1. `op.create_table("proxy_providers", ...)` — all columns per data-model.md; `PrimaryKey(id)`;
   `ForeignKeyConstraint(["workspace_id"],["workspaces.id"], name=...)`. Then:
   - `op.create_index("ix_proxy_providers_workspace_id", ...)`.
   - partial unique index `uq_proxy_providers_workspace_id_name` on `(workspace_id, name)`
     `postgresql_where=sa.text("workspace_id IS NOT NULL")`.
   - partial unique index `uq_proxy_providers_name_global` on `(name)`
     `postgresql_where=sa.text("workspace_id IS NULL")`.
2. `op.create_table("access_policies", ...)` — same dual-scope index pair
   (`uq_access_policies_workspace_id_name` partial NOT NULL, `uq_access_policies_name_global`
   partial NULL) + `ix_access_policies_workspace_id` + workspace FK. `provider_id` plain UUID
   (no FK).
3. `op.create_table("domain_access_rules", ...)` — workspace FK; `ix_domain_access_rules_workspace_id`;
   composite lookup index `ix_domain_access_rules_workspace_id_competitor_id_domain`;
   uniqueness on `(workspace_id, competitor_id, domain, COALESCE(url_pattern,''))` via an
   expression unique index (so exactly one domain-only rule per domain and one per distinct
   pattern).
4. RLS in the **same** migration:
   - `for stmt in emit_global_readable_rls_policy("proxy_providers"): op.execute(stmt)`
   - `for stmt in emit_global_readable_rls_policy("access_policies"): op.execute(stmt)`
   - `for stmt in emit_rls_policy("domain_access_rules"): op.execute(stmt)`

`request_attempts` is **not** touched — it already exists (SPEC-07). No `op.add_column`.

## downgrade()

`op.drop_table` in reverse creation order (`domain_access_rules`, `access_policies`,
`proxy_providers`). Indexes drop with their tables.

## Acceptance

- `alembic heads` (offline/metadata check) yields a single head after this revision.
- Rendered DDL contains `ENABLE`/`FORCE ROW LEVEL SECURITY` + the dual read/write policies for
  the two dual-scope tables and the single isolation policy for `domain_access_rules`
  (assert on the emitted statement strings, SPEC-06/09 precedent).
- Both partial-unique namespaces present on each dual-scope table.
