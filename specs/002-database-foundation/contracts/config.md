# Contract: Config & engine hygiene extensions

Modules: `app_shared/config.py` (extend), `app_shared/database.py` (extend).

## Config: MIGRATION_DATABASE_URL (new setting)

- New **optional** field on `Settings`: `MIGRATION_DATABASE_URL: str | None = None`.
- Direct-to-Postgres URL (e.g. `postgresql+psycopg://user:pass@postgres:5432/db`) used **only** by the migration job / Alembic `env.py`.
- App services continue to use `DATABASE_URL` (→ `pgbouncer:6432`, transaction pooling). The two never converge.
- Optional (not required) so app services that never migrate don't need it set; the migration job requires it (env.py errors clearly if absent when running online).
- `.env.example` documents both, with the "apps use pooler / migration job uses direct" split.

## Engine hygiene (unchanged from SPEC-01, formalized here)

Guarantees carried forward and covered by this spec's acceptance:
- **No eager engine**: importing `app_shared.database` creates no engine/connection (FR-008, SC-005). *Verifiable here.*
- **One lazy engine per process**: `get_engine()` builds exactly one engine on first use, reused (FR-008).
- **Fork-safe**: `dispose_engine()` (Celery `worker_process_init` hook) disposes any inherited engine before first use (FR-009).
- **Pooler-safe config**: `pool_pre_ping=True`, `connect_args={"prepare_threshold": None}` (server-side prepared statements off); only `SET LOCAL` / `pg_advisory_xact_lock` relied upon (FR-010).

## Connectivity check (new helper)

- `app_shared/database.check_connection() -> None` (or `-> bool`): opens a session and executes `SELECT 1` (FR-015, SC-007). Trivial, no schema dependency.
- Live-DB behavior is verified on a Postgres-capable host; the function itself is import-safe and unit-checkable (no engine created until called).

## Tests

- `tests/unit/test_import_boundaries.py` (extended) — new `app_shared` submodules (`models`, `ids`, `money`, `enums`) import no scrapy/twisted/playwright and no `scrape_core`.
- Existing no-eager-engine / one-per-process / fork-safety tests continue to pass.
- `tests/integration/test_db_connectivity.py` (marked live-DB) — `check_connection()` executes `SELECT 1`.
