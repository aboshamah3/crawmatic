# Implementation Plan: Database Foundation

**Branch**: `002-database-foundation` | **Date**: 2026-07-02 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/002-database-foundation/spec.md`

## Summary

Deliver the reusable database machinery that makes every later table correct-by-construction, plus the one-shot Alembic migration system — with **no domain tables and no business logic**. Concretely, this feature extends the SPEC-01 `app_shared` package with: a shared declarative `Base` carrying a deterministic naming convention (that disambiguates two multi-column uniques sharing a leading column), an application-generated **UUIDv7** primary-key default, a `TIMESTAMPTZ`-only `TimestampMixin` that forbids naive datetimes at the base level, a `Money` SQLAlchemy `TypeDecorator` (`NUMERIC(18,4)`, rejects NaN/Infinity/over-scale), string-backed enum support, a `WorkspaceScopedBase` mixin plus a reusable RLS-policy-DDL emitter (fail-closed), Alembic wiring (`alembic.ini` + `env.py` targeting the shared metadata via the **direct** migration URL), a demonstration/smoke model + first migration exercising the machinery, a CI single-head guard, and a basic connectivity check. DB-independent behaviour is fully unit-tested in this environment; live-Postgres items (running the migration job, connectivity) are authored and marked for a Postgres-capable host (no Docker daemon here).

## Technical Context

**Language/Version**: Python 3.13 (`requires-python = ">=3.13,<3.14"`; uv workspace).

**Primary Dependencies**: SQLAlchemy 2.0 (sync), psycopg 3 (`psycopg[binary]`), Alembic (new), `uuid6` (new — UUIDv7 generator), pydantic-settings (existing). `app_shared` MUST NOT import scrapy/twisted/playwright.

**Storage**: PostgreSQL 17. App services connect through PgBouncer (`pgbouncer:6432`, transaction pooling); the migration job connects **directly** to `postgres:5432`.

**Testing**: pytest (existing dev group). DB-independent unit tests run here; live-DB tests are authored and skipped when no reachable Postgres / `MIGRATION_DATABASE_URL` is present. (Single canonical gate variable: `MIGRATION_DATABASE_URL` — not a separate `TEST_DATABASE_URL`.)

**Target Platform**: Linux server / containers (compose locally, Railway-style platform in prod).

**Project Type**: Backend monorepo (uv workspace); this feature is library-level (`libs/shared/app_shared`) plus repo-root Alembic and a compose one-shot `migrate` service.

**Performance Goals**: Not a hot path. UUIDv7 generation is app-side and negligible; time-ordered PKs keep insert-heavy B-tree locality (§21). Engine is one-per-process, lazy, fork-safe (extends SPEC-01).

**Constraints**: Transaction-pooling-safe only — no server-side prepared statements (`prepare_threshold=None`, already set), only `SET LOCAL` / `pg_advisory_xact_lock`. No live Postgres or Docker daemon in this build env: DB-independent parts verified here; live parts authored + marked.

**Scale/Scope**: Foundation for 2,000 products / 10k–20k matches per workspace (§39). This spec adds ~7 new `app_shared` modules, Alembic setup, one demo migration, and their tests. Zero real domain tables.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Relevance | How this plan satisfies it |
|-----------|-----------|----------------------------|
| **I. API-First / Service boundaries** | `app_shared` import boundary | New modules (`models/`, `ids.py`, `money.py`, `enums.py`) live in `app_shared` and import only sqlalchemy/psycopg/uuid6/stdlib — never scrapy/twisted/playwright. The existing import-boundary test is extended to cover the new submodules. Alembic (`alembic/env.py`) lives at repo root, not inside `app_shared`, and imports `app_shared` one-way. PASS |
| **II. Workspace Isolation (NON-NEGOTIABLE)** | RLS-ready base | Deliver `WorkspaceScopedBase`/mixin (adds `workspace_id UUID NOT NULL`) + `emit_rls_policy()` helper that renders `ENABLE ROW LEVEL SECURITY` + `FORCE` + a fail-closed policy `USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)` (NULLIF so absent AND empty context → zero rows, never raising `''::uuid`). `SET LOCAL` transaction-scoped (PgBouncer-safe). No real workspace table created here (first use SPEC-03); helper is unit/statically validated (rendered DDL string asserted). PASS (RLS-ready) |
| **III. Variant-level pricing** | N/A this spec | No pricing/matching logic in the foundation. Base does not preclude it. PASS (N/A) |
| **IV. Database-driven config** | N/A this spec | No config tables here. PASS (N/A) |
| **V. Disciplined Scraping Runtime (NON-NEGOTIABLE)** | Import boundary only | No scraping code. `app_shared` stays scrapy-free (see I). PASS (N/A) |
| **VI. Internal-only / legal** | N/A this spec | No scraping/access code. PASS (N/A) |
| **VII. Monetary correctness (NON-NEGOTIABLE)** | Money type | `Money` `TypeDecorator` over `NUMERIC(18,4)`: bind-param validation rejects float inputs and non-finite (NaN/Infinity), rejects over-scale (>4 fractional digits) rather than rounding; result value is always `Decimal`. Unit-tested exhaustively (DB-independent). PASS |
| **VIII. Scale-safe data & concurrency (NON-NEGOTIABLE)** | UUIDv7 / TIMESTAMPTZ / pooler-safe / partition-ready | UUIDv7 PK default (time-ordered, insert-friendly); `TIMESTAMPTZ` everywhere with naive-datetime guard; engine pooler-safe (prepared statements off, `SET LOCAL` only); base does **not** preclude the later partitioned-table "PK includes partition key" rule (PK is a normal constraint a subclass can extend). Migration job connects directly (session advisory locks + `CREATE INDEX CONCURRENTLY` unsafe through the pooler). Single linear history, CI fails on multiple heads. PASS |

**Technology & Security Constraints**: Stack lock-in honored (SQLAlchemy + Alembic, PostgreSQL, psycopg). Identifiers are app-generated UUIDv7 (§21). No secrets introduced; `MIGRATION_DATABASE_URL` is an env var, never committed.

**Gate result**: PASS — no violations. Re-checked post-Phase-1 (see end of plan): still PASS. Complexity Tracking table left empty.

## Project Structure

### Documentation (this feature)

```text
specs/002-database-foundation/
├── plan.md              # This file
├── research.md          # Phase 0 — decisions (UUIDv7 lib, naming convention, money, RLS, Alembic, demo strategy)
├── data-model.md        # Phase 1 — Base/mixins/demo model entities + validation rules
├── quickstart.md        # Phase 1 — how to validate (unit here; migration/connectivity on a PG host)
├── contracts/           # Phase 1 — the interfaces app_shared exposes
│   ├── models-base.md
│   ├── ids.md
│   ├── money.md
│   ├── enums.md
│   ├── rls.md
│   ├── migration-job.md
│   └── config.md
└── tasks.md             # Phase 2 (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
libs/shared/app_shared/
├── config.py            # EXTEND: add MIGRATION_DATABASE_URL (optional; direct-to-Postgres)
├── database.py          # EXTEND (minor): add check_connection() connectivity helper
├── ids.py               # NEW: new_uuid7() -> uuid.UUID (uuid6.uuid7); UUIDv7 PK column factory
├── money.py             # NEW: Money(TypeDecorator) over NUMERIC(18,4); validation
├── enums.py             # NEW: StrEnum base + string-backed SA column helper for core enums
├── models/
│   ├── __init__.py      # NEW: re-export Base, TimestampMixin, WorkspaceScopedBase, metadata
│   ├── base.py          # NEW: MetaData(naming_convention); DeclarativeBase; UUIDv7 pk mixin; TimestampMixin (naive guard); WorkspaceScopedBase
│   ├── rls.py           # NEW: emit_rls_policy() -> DDL / Alembic-op helper (fail-closed)
│   └── _smoke.py        # NEW: _smoke_foundation demonstration model (non-domain; proves the machinery, T022)
├── task_names.py, __init__.py   # existing

alembic.ini                         # NEW (repo root)
alembic/
├── env.py                          # NEW: target_metadata = app_shared Base.metadata; direct URL; compare_type; TIMESTAMPTZ
├── script.py.mako                  # NEW: standard Alembic template
└── versions/
    └── <rev>_smoke_foundation.py   # NEW: first migration — demo table + RLS-helper render demo

apps/migrate/                        # NEW: one-shot migration job image (installs from root lockfile)
└── Dockerfile                       # NEW: python base; entrypoint `alembic upgrade head`; uses MIGRATION_DATABASE_URL

docker-compose.yml                   # EXTEND: add one-shot `migrate` service (direct postgres:5432; not long-running)
.env.example                         # EXTEND: add MIGRATION_DATABASE_URL (direct) + note apps use DATABASE_URL (pooler)

tests/unit/
├── test_import_boundaries.py        # EXTEND: cover new app_shared submodules (models, ids, money, enums)
├── test_naming_convention.py        # NEW: two-uniques-sharing-first-column → distinct names
├── test_ids.py                      # NEW: UUIDv7 version/time-ordering/stdlib-UUID
├── test_money.py                    # NEW: NaN/Infinity/over-scale rejected; in-scale round-trips as Decimal
├── test_base_model.py               # NEW: naive-datetime guard; demo model has UUIDv7 pk + tz-aware ts cols
├── test_rls_policy.py               # NEW: emit_rls_policy() renders fail-closed DDL (string assert)
└── test_migration_offline.py        # NEW: `alembic upgrade head --sql` renders (offline, no DB); single head
tests/integration/
├── test_migration_job.py            # NEW (marked live-DB): upgrade head against real PG; demo table exists
└── test_db_connectivity.py          # NEW (marked live-DB): check_connection() executes SELECT 1
scripts/
└── check_single_head.sh             # NEW: CI guard — `alembic heads` must show exactly one head
```

**Structure Decision**: Extend the existing SPEC-01 `libs/shared/app_shared` package rather than forking it — new modules `ids.py`, `money.py`, `enums.py` at the package top level and `models/` (base + mixins + rls) per the master §5 target tree. Alembic lives at repo root (`alembic.ini`, `alembic/`) as §5 shows, with `env.py` importing `app_shared` one-way. The migration job is a new `apps/migrate` one-shot image + a compose `migrate` service that connects directly to Postgres. `schemas/`, `security/`, `pagination.py`, `url_patterns.py` are explicitly **out of scope** (later specs).

## Phase 0 / Phase 1 outputs

- Phase 0 research: [research.md](./research.md)
- Phase 1 data model: [data-model.md](./data-model.md)
- Phase 1 contracts: [contracts/](./contracts/)
- Phase 1 quickstart: [quickstart.md](./quickstart.md)

## Post-Design Constitution Re-Check

Re-evaluated after Phase 1 artifacts: no new violations introduced. The naming convention (`column_0_N_name`), UUIDv7 default, `TIMESTAMPTZ`+guard, `Money` validation, RLS fail-closed helper, direct-URL migration job, and single-head CI guard collectively satisfy Principles II, VII, VIII and the §22/§32 conventions. **Gate: PASS.**

## Complexity Tracking

> No Constitution Check violations — table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |
