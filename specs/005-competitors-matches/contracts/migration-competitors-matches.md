# Contract: Competitors/Matches Migration (`alembic/versions/<rev>_competitors_matches_tables.py`)

One hand-authored Alembic migration (no live Postgres to autogenerate against; column/constraint shapes reproduce `app_shared.models.competitors_matches` exactly, honoring the SPEC-02 `NAMING_CONVENTION` for `competitors` and the explicit `cpm` names for `competitor_product_matches`). Chains onto the current head.

## Revision chain
- `down_revision = "c2987b29555e"` (the SPEC-04 `catalog_tables` head).
- Single linear history — `scripts/check_single_head.sh` stays green (one head after this revision).

## `upgrade()`
1. `create_table("competitors", ...)` — §22 columns; `PrimaryKeyConstraint("id", name="pk_competitors")`; `UniqueConstraint("workspace_id","id", name="uq_competitors_workspace_id_id")`; `UniqueConstraint("workspace_id","domain", name="uq_competitors_workspace_id_domain")`; `ForeignKeyConstraint(["workspace_id"],["workspaces.id"], name="fk_competitors_workspace_id_workspaces")`. Then `create_index("ix_competitors_workspace_id", "competitors", ["workspace_id"])`.
2. `create_table("competitor_product_matches", ...)` — §22 columns (`url_pattern_version`/`consecutive_failures` as `Integer`; `success_rate_7d` as `Numeric(5,4)`; `current_price_id`/`scrape_profile_id`/`access_policy_id` as plain `Uuid` **no FK**; `competitor_variant_options` as `JSONB`; timestamps `DateTime(timezone=True)`); `PrimaryKeyConstraint("id", name="pk_competitor_product_matches")`; the 4-col `UniqueConstraint(... name="uq_cpm_ws_variant_competitor_norm_url")`; the three composite FKs + the workspace FK with the explicit `fk_cpm_*` names (contract: `models-competitors-matches.md`). Then `create_index("ix_competitor_product_matches_workspace_id", ...)`.
3. **RLS in the SAME migration** (FR-001, §32, Principle II) — for both tables:
   ```python
   for stmt in emit_rls_policy("competitors"): op.execute(stmt)
   for stmt in emit_rls_policy("competitor_product_matches"): op.execute(stmt)
   ```

## `downgrade()`
`op.drop_table("competitor_product_matches")` then `op.drop_table("competitors")` (FK-safe order: the match references the competitor).

## Constraint-name budget
Every emitted name is ≤63 bytes: `competitors` names are convention-generated (≤38 chars); `competitor_product_matches` uses the explicit `cpm` names (all verified ≤63). A unit test asserts this against the migration's rendered DDL and the ORM metadata.

## Unit tests (no DB — offline render)
- `alembic upgrade head --sql` (offline) renders `CREATE TABLE competitors` + `CREATE TABLE competitor_product_matches`, the two competitor uniques, the 4-col match unique, the three composite FKs, and — for **both** tables — `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` + `CREATE POLICY ... USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)`.
- Single head after this revision (`check_single_head.sh`).

## Live-DB (PG host)
`alembic upgrade head` creates both tables + RLS online; `alembic downgrade -1` drops them and returns to `c2987b29555e`.
