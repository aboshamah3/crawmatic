# Contract: migration `<rev>_scrape_profiles_table.py`

One hand-authored Alembic migration. `down_revision = 'f4c8a391d5c9'` (current single head: `023a24e5717d → 55da7d6d939d → c2987b29555e → f4c8a391d5c9`). Single head preserved (`scripts/check_single_head.sh`).

## `upgrade()`

1. `op.create_table("scrape_profiles", ...)` — the §22 columns (exact shapes reproducing `app_shared.models.scrape_profiles`): `id` PK, **nullable** `workspace_id`, `name`, `mode`/`adapter_key` `String(32)`, three `*_enabled` `Boolean`, the nullable `*_selector`/`*_xpath`/`*_regex` + `title_*` `Text`, `variant_strategy` `String(32)`, JSONB `variant_selector_config`/`price_transform_rules`/`validation_rules`/`confidence_rules`/`headers`/`cookies`, `wait_for_selector` `Text`, `request_timeout_ms` `Integer`, `browser_timeout_ms` `Integer` nullable, `created_at`/`updated_at` `DateTime(timezone=True)`; `PrimaryKeyConstraint("id", name="pk_scrape_profiles")`; `ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_scrape_profiles_workspace_id_workspaces")`.
2. `op.create_index("ix_scrape_profiles_workspace_id", ...)`.
3. Two **partial** unique indexes: `uq_scrape_profiles_workspace_id_name` on `(workspace_id, name)` `postgresql_where=sa.text("workspace_id IS NOT NULL")`; `uq_scrape_profiles_name_global` on `(name)` `postgresql_where=sa.text("workspace_id IS NULL")`.
4. `for stmt in emit_global_readable_rls_policy("scrape_profiles"): op.execute(stmt)` — ENABLE+FORCE + read(own|global) + write(own-only) policies (FR-021, §32).
5. Promote the three assignment columns to FKs `ON DELETE SET NULL`:
   - `op.create_foreign_key("fk_competitors_default_scrape_profile_id_scrape_profiles", "competitors", "scrape_profiles", ["default_scrape_profile_id"], ["id"], ondelete="SET NULL")`
   - `op.create_foreign_key("fk_cpm_scrape_profile_id_scrape_profiles", "competitor_product_matches", "scrape_profiles", ["scrape_profile_id"], ["id"], ondelete="SET NULL")`
   - `op.create_foreign_key("fk_workspaces_default_scrape_profile_id_scrape_profiles", "workspaces", "scrape_profiles", ["default_scrape_profile_id"], ["id"], ondelete="SET NULL")`
   - (FK names ≤63 bytes — the `cpm` shorthand reused for the match, per SPEC-05 precedent.)

## `downgrade()`

Drop the three FKs, then `op.drop_table("scrape_profiles")` (the partial indexes + RLS drop with the table).

## Notes

- Hand-authored (no live Postgres for autogenerate); shapes reproduce the ORM model exactly.
- RLS is emitted in the **same** migration that creates the table (SPEC-04/05 precedent).
- No global-profile seeding (research D11 — out-of-band privileged path).

## Tests

- **Unit (offline)**: `alembic upgrade head --sql` renders `CREATE TABLE scrape_profiles`, both partial unique indexes with the exact predicates, the four RLS statements (read own|global; write own-only), and the three `ADD CONSTRAINT ... FOREIGN KEY ... ON DELETE SET NULL`; single head (`check_single_head.sh` green).
- **Live (marked)**: `alembic upgrade head` then `downgrade -1` runs clean on a PG host.
