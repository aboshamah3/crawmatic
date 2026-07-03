# Contract: jobs migration (`alembic/versions/<rev>_scrape_jobs_targets_tables.py`)

Hand-authored (no live Postgres in this build env), reproducing `app_shared.models.jobs` exactly. `down_revision = '2db33dea5e14'` (current head, SPEC-07 observations); single linear head preserved.

## Upgrade

1. `op.create_table("scrape_jobs", ...)` — all §22 columns; `PrimaryKeyConstraint("id")`; `UniqueConstraint("workspace_id", "id")`; `ForeignKeyConstraint(["workspace_id"], ["workspaces.id"])`; index on `workspace_id`. Enum-like columns as `sa.String(length=32)` (matching `enum_column` DDL, per the SPEC-07 migration precedent). `created_at` only (no `updated_at`).
2. `op.create_table("scrape_job_targets", ...)` — all §22 columns; `PrimaryKeyConstraint("id")`; `UniqueConstraint("scrape_job_id", "match_id", name="uq_scrape_job_targets_scrape_job_id_match_id")`; composite `ForeignKeyConstraint(["workspace_id", "scrape_job_id"], ["scrape_jobs.workspace_id", "scrape_jobs.id"])`; RLS-anchor `ForeignKeyConstraint(["workspace_id"], ["workspaces.id"])`; indexes on `workspace_id` and `match_id`; `match_id` has no FK (soft ref). `created_at` only.
3. `for stmt in emit_rls_policy("scrape_jobs"): op.execute(stmt)` and likewise for `scrape_job_targets` — ENABLE + FORCE + fail-closed policy in the SAME migration (FR-004, §32, Principle II), matching the SPEC-04/05/06/07 precedent.

## Downgrade

- `op.drop_table("scrape_job_targets")` then `op.drop_table("scrape_jobs")` (child before parent; RLS policies drop with the tables).

## Tests (`test_migration_offline_jobs.py`)

- `alembic upgrade head --sql` (offline, no DB) renders both `CREATE TABLE`s, the two uniques, the composite FK, and the RLS statements for both tables.
- `alembic heads` yields a single head; `down_revision == 2db33dea5e14`.
