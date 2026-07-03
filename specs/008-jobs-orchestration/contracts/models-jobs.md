# Contract: Jobs ORM models (`app_shared.models.jobs`)

Two workspace-owned tables, exact §22 shapes (see data-model.md for the full column tables). Both `Base` + `WorkspaceScopedBase`, registered in `app_shared.repository.WORKSPACE_OWNED_MODELS`, re-exported from `app_shared.models.__init__`, RLS emitted in the creating migration (not here). `created_at` only (no `updated_at`, §22) — explicit `created_at` column, not `TimestampMixin`.

## `ScrapeJob` (`scrape_jobs`)

- Columns: `id`, `workspace_id`, `type` (`ScrapeJobType`), `scope` (`ScrapeScope`), nullable soft refs `product_id`/`product_variant_id`/`product_group_id`/`competitor_id`/`match_id`, `status` (`ScrapeJobStatus`), `priority` (`MatchPriority`), `total_targets`/`success_count`/`failure_count`/`skipped_count` (INT), `requested_by` (nullable UUID), `source` (`ScrapeJobSource`), `started_at`/`completed_at` (nullable TZDateTime), `created_at`.
- `__table_args__`: `UniqueConstraint("workspace_id", "id")` (composite-FK target for targets) + `ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_scrape_jobs_workspace_id_workspaces")`.
- Counters are only ever overwritten by `aggregate_counts`, never per-target incremented.

## `ScrapeJobTarget` (`scrape_job_targets`)

- Columns: `id`, `workspace_id`, `scrape_job_id`, `match_id`, `status` (`ScrapeTargetStatus`), `locked_at`/`started_at`/`completed_at` (nullable TZDateTime), `error_code` (`ScrapeErrorCode`, nullable), `created_at`.
- `__table_args__`:
  - `UniqueConstraint("scrape_job_id", "match_id", name="uq_scrape_job_targets_scrape_job_id_match_id")` (§22).
  - `ForeignKeyConstraint(["workspace_id", "scrape_job_id"], ["scrape_jobs.workspace_id", "scrape_jobs.id"], name="fk_scrape_job_targets_workspace_scrape_job_scrape_jobs")` (workspace-local → parent job).
  - `ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_scrape_job_targets_workspace_id_workspaces")` (RLS anchor).
  - `match_id`: plain indexed UUID, **no** FK (soft ref, §22 / SPEC-07 precedent).

## Tests (`test_jobs_models.py`)

- Table/column names + nullability; both enum columns render `VARCHAR` (not DB enum) via `enum_column`; `created_at` present, `updated_at` absent.
- `unique(scrape_job_id, match_id)` on targets; `unique(workspace_id, id)` on jobs; the composite FK target→job; RLS-anchor FK on both.
- Both models are in `WORKSPACE_OWNED_MODELS` and re-exported from `app_shared.models`.
