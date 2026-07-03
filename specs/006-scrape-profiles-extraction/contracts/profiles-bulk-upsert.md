# Contract: set-based profile bulk-upsert (`app_shared/profiles/upsert.py`, pure)

Set-based reject-and-report bulk-upsert keyed by `(workspace_id, name)` (FR-020, SC-008), mirroring the SPEC-04/05 pattern. Pure — compiles SQLAlchemy Core statements; never executes, never opens a session. Tenant-only: every row carries the caller's `workspace_id` (never NULL) so the statement can never write a global row.

## `prepare_profiles(rows, *, workspace_id) -> (valid, rejected)`

Per row: run `validate_profile` (enums, regex compile+ReDoS, cookie deny, `validation_rules`, `confidence_rules`). A `ProfileValidationError` moves the row to `rejected` (with `index`, `name`, `field`, `code`, `reason`); a valid row (with `workspace_id` stamped) moves to `valid`. Never aborts the batch (reject-and-report). Then `dedup_last_wins(valid, key=(workspace_id, name))` (reused from `app_shared.catalog.upsert`) collapses same-key rows keeping the last.

## `build_profiles_upsert(rows) -> Insert`

One statement:

```python
stmt = pg_insert(ScrapeProfile).values(list(rows))
stmt.on_conflict_do_update(
    index_elements=["workspace_id", "name"],
    index_where=text("workspace_id IS NOT NULL"),   # matches the tenant partial unique exactly
    set_={col: stmt.excluded[col] for col in _PROFILE_UPDATABLE_COLUMNS} | {"updated_at": func.now()},
)
```

`_PROFILE_UPDATABLE_COLUMNS` = every column except `id`, `workspace_id`, `created_at` (immutable identity/audit). `updated_at` refreshed via `func.now()` (Core upsert doesn't fire the ORM `onupdate`).

## Rules

- Bounded (SC-008): exactly **one** `INSERT ... ON CONFLICT DO UPDATE` for all valid rows — no per-row loop.
- The `index_where` predicate must match `uq_scrape_profiles_workspace_id_name`'s `WHERE workspace_id IS NOT NULL` **exactly** or Postgres can't infer the arbiter (SPEC-04 inference rule).
- Global rows are never produced here (tenant-only); the global-namespace partial unique is never the arbiter on this path.

## Tests (unit, no DB)

- `build_profiles_upsert` compiles (postgresql dialect) to `ON CONFLICT (workspace_id, name) WHERE workspace_id IS NOT NULL DO UPDATE SET ...` — one statement.
- Updatable set excludes `id`/`workspace_id`/`created_at`; includes `updated_at = now()`.
- `prepare_profiles`: a mix of valid + invalid rows → all valid in `valid`, every invalid in `rejected` with its field/code/reason; last-wins dedup on `(workspace_id, name)`.
