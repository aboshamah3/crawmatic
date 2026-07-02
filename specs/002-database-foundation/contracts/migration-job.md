# Contract: One-shot migration job & Alembic wiring

Files: `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/<rev>_smoke_foundation.py`, `apps/migrate/Dockerfile`, compose `migrate` service, `scripts/check_single_head.sh`.

## Alembic env (`alembic/env.py`) — guarantees

- `target_metadata = app_shared.models.Base.metadata` (single shared metadata → autogenerate honors the naming convention, FR-014).
- `compare_type = True` (detects `TIMESTAMPTZ` vs `TIMESTAMP`, `NUMERIC` scale drift).
- Online engine is built from **`MIGRATION_DATABASE_URL`** (direct-to-Postgres), or a CLI `-x db_url=<url>` override. It does **not** call `app_shared.database.get_engine()` (that targets the pooler).
- Offline mode (`--sql`) renders DDL without any DB connection.

## Migration job — guarantees

- `apps/migrate/Dockerfile`: python base image; installs from the **root uv lockfile** (migrations run under the exact pinned versions — §5); entrypoint runs `alembic upgrade head`.
- Compose `migrate` service: **one-shot** (`restart: "no"`), `depends_on: postgres (service_healthy)`, connects to `postgres:5432` **directly** via `MIGRATION_DATABASE_URL` — never `pgbouncer:6432`.
- Application services (`api`/`scheduler`/`worker`) do **NOT** run migrations at startup (FR-011, §6); they only `depends_on` pgbouncer.
- Rationale for direct connection: Alembic's session-scoped version-table advisory lock and `CREATE INDEX CONCURRENTLY` are unsafe/broken through transaction pooling (§4/§6).

## Single linear history — guarantee

- `scripts/check_single_head.sh`: runs `alembic heads` and exits non-zero unless exactly **one** head is reported (FR-012, SC-006). DB-independent (reads migration files). Runs in CI.

## Cannot run here

- No Docker daemon / live Postgres in this build env. The Dockerfile, compose service, and `alembic upgrade head` are **authored and marked** for execution on a Postgres-capable host (spec Assumptions). DB-independent proof (offline `--sql` render, single-head check) runs here.

## Tests

- `tests/unit/test_migration_offline.py` — `alembic upgrade head --sql` renders text containing `CREATE TABLE`, `TIMESTAMPTZ`, `NUMERIC(18, 4)`, `uuid`, and the two distinct unique names; single head.
- `tests/integration/test_migration_job.py` (marked live-DB) — `alembic upgrade head` against a real DB creates the demo table (SC-001).
