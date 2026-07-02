---
description: "Dependency-ordered, executable task list for the Database Foundation feature (SPEC-02)"
---

# Tasks: Database Foundation

**Input**: Design documents from `/srv/crawmatic/crawmatic/specs/002-database-foundation/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/{models-base,ids,money,enums,rls,migration-job,config}.md, quickstart.md

**Tests**: Included and REQUIRED. The spec, plan, quickstart and every contract explicitly enumerate the test surface (naming convention, UUIDv7, Money, TZ guard, RLS render, offline migration render, single-head, connectivity). DB-independent tests run in THIS environment; live-Postgres tests are authored and marked DEFERRED.

**Organization**: Tasks are grouped into dependency-ordered phases. User-story phases carry `[US#]` labels matching the spec's user stories (US1 = one-shot migration job, US2 = correct-by-construction tables, US3 = money boundary, US4 = pooler/fork-safe sessions). Phases are sequenced by *hard dependency*, not raw priority: the US1 demonstration migration composes the US2 base + US3 Money type + core enum, so it is authored last even though it is P1. See Dependencies.

## Format: `[ID] [P?] [Story?] Description with exact file path`

- **[P]**: May run in parallel (different file, no dependency on an incomplete task)
- **[US#]**: Owning user story (user-story phases only; Setup/Foundational/Polish carry no story label)
- **⏸ DEFERRED (needs live Postgres)**: authored here but NOT runnable in this build env (no Docker daemon / no live PG). Left as `- [ ]` (never `[X]`) so the orchestrator tracks it honestly and runs it on a Postgres-capable host.

## Scope Boundary (READ FIRST — hard constraints)

This feature delivers **reusable database machinery + ONE tiny non-domain demonstration/smoke table only**. The following are explicitly **OUT OF SCOPE** and MUST NOT be created by any task below:

- ❌ **No real domain tables** — no `workspaces`, `users`, `products`, `product_variants`, `matches`, `runs`, etc. (SPEC-03+).
- ❌ **No auth / authorization** logic, no roles, no sessions-as-identity.
- ❌ **No RLS policy applied to any real table** — `emit_rls_policy()` and `WorkspaceScopedBase` are delivered and validated by *rendered-DDL string assertion* only. The demo table is **not** workspace-owned; no live isolation surface is created here (first concrete use is SPEC-03).
- ❌ **No business logic** — no pricing, matching, scraping, scheduling, or config tables.
- ❌ **No Postgres-native ENUM types** — enums are string-backed and app-validated (§22).
- ❌ **No FK from `workspace_id`** — plain indexed UUID column until `workspaces` exists (SPEC-03).
- ✅ **In scope**: the `app_shared` primitives (`ids`, `money`, `enums`, `models/{base,rls,__init__,_smoke}`), the config/connectivity extensions, Alembic wiring, the first migration creating the single `_smoke_foundation` demo table, the one-shot migrate job image + compose service, the CI single-head guard, and their tests.

The only table any migration may create in this spec is `_smoke_foundation` (the deliberately non-domain bookkeeping/demonstration table from data-model.md).

---

## Phase 1: Setup & Dependencies

**Purpose**: Add the two new dependencies (`uuid6`, `alembic`) and refresh the lockfile so every later task can import them.

- [X] T001 [P] Add `uuid6>=2025.0.1,<2026` (pinned per research.md D1) to the `dependencies` list in `libs/shared/pyproject.toml` (the `app_shared` package pins it because `ids.py` imports it).
- [X] T002 [P] Add `alembic>=1.13,<2` to the `dev` dependency group in the root `/srv/crawmatic/crawmatic/pyproject.toml` so the `alembic` CLI (offline `--sql` render + `alembic heads` single-head guard) is runnable here; note it will also become the `apps/migrate` runtime dep in T029.
- [X] T003 Refresh the lockfile: run `uv lock` from repo root so `uv.lock` records `uuid6` and `alembic` (depends on T001, T002). Verify `uv sync` succeeds.

**Checkpoint**: `uv run python -c "import uuid6, alembic"` succeeds.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Config surface that BOTH the migration job (US1) and connectivity (US4) depend on. No story label (cross-cutting).

**⚠️ CRITICAL**: `MIGRATION_DATABASE_URL` must exist before `alembic/env.py` (T025) can build its direct engine.

- [X] T004 Extend `Settings` in `libs/shared/app_shared/config.py`: add optional field `MIGRATION_DATABASE_URL: str | None = None` (direct-to-Postgres, used only by the migration job / Alembic `env.py`; apps keep `DATABASE_URL` → pooler). Keep every existing field unchanged. Per contracts/config.md.
- [X] T005 [P] Update `/srv/crawmatic/crawmatic/.env.example`: add `MIGRATION_DATABASE_URL=postgresql+psycopg://crawmatic:crawmatic@postgres:5432/crawmatic` with a comment documenting the split — **apps use `DATABASE_URL` (pgbouncer:6432, pooler); the migration job uses `MIGRATION_DATABASE_URL` (postgres:5432, direct)**. Per contracts/config.md / research.md D6.

**Checkpoint**: `Settings` constructs with and without `MIGRATION_DATABASE_URL` set; existing `tests/unit/test_config.py` still passes.

---

## Phase 3: User Story 2 — Define tables that are correct-by-construction (Priority: P1) 🎯 core primitives

**Goal**: A shared declarative `Base` (single metadata + deterministic all-columns naming convention), a UUIDv7 PK, a `TIMESTAMPTZ`-only `TimestampMixin` that forbids naive datetimes, string-backed enum support, and an RLS-ready `WorkspaceScopedBase` + `emit_rls_policy()` helper — so every later table inherits correctness.

**Independent Test**: `uv run pytest tests/unit/test_naming_convention.py tests/unit/test_ids.py tests/unit/test_base_model.py tests/unit/test_rls_policy.py tests/unit/test_enums.py -q` — a table with two multi-column uniques sharing a leading column gets two DISTINCT names; `new_uuid7()` is a version-7 time-ordered stdlib `UUID`; a naive datetime is rejected by `TZDateTime`; `emit_rls_policy()` renders fail-closed DDL; enums store/validate their string value. No database required.

### Implementation for User Story 2

- [X] T006 [P] [US2] Create `libs/shared/app_shared/ids.py`: `new_uuid7() -> uuid.UUID` wrapping `uuid6.uuid7()`; optional `uuid7_pk()` column factory returning the standard UUIDv7 PK `mapped_column`. Per contracts/ids.md.
- [X] T007 [US2] Create `libs/shared/app_shared/models/base.py`: the exact `NAMING_CONVENTION` dict from research.md D2 / contracts/models-base.md (`ix`/`uq`/`fk` = `column_0_N_name`, `ck` = `constraint_name`, `pk` = `table_name`); `metadata = MetaData(naming_convention=NAMING_CONVENTION)`; `Base(DeclarativeBase)` bound to that metadata with a UUIDv7 `id` PK (`default=new_uuid7`, `sqlalchemy.Uuid`); `TZDateTime(TypeDecorator)` over `DateTime(timezone=True)` whose `process_bind_param` raises `ValueError` when a `datetime` has `tzinfo is None` (value-level guard); `TimestampMixin` (`created_at`/`updated_at` as `TZDateTime`, non-null, UTC-aware `default`/`onupdate`); `WorkspaceScopedBase` mixin adding `workspace_id: Mapped[uuid.UUID]` (`Uuid`, `nullable=False`, `index=True`, **no FK**). **Structural naive-column guard (FR-004 "at the base level so per-table code cannot introduce them"):** register a SQLAlchemy `@event.listens_for(Base, "instrument_class")` (or a `mapper_configured` listener) that inspects each mapped column and raises `TypeError` if any column's type is a `DateTime`/`TIMESTAMP` with `timezone=False` (i.e. anything other than `TZDateTime`/tz-aware `DateTime(timezone=True)`), so a developer declaring a raw naive `DateTime()` column on a model that extends `Base` fails at class/mapper configuration time — not silently at runtime. Imports `new_uuid7` from T006. Per contracts/models-base.md + data-model.md. [analyze I1]
- [X] T008 [P] [US2] Create `libs/shared/app_shared/models/rls.py`: `emit_rls_policy(table_name, *, workspace_column="workspace_id", policy_name=None) -> tuple[str, ...]` returning the three DDL strings — `ALTER TABLE … ENABLE ROW LEVEL SECURITY;`, `ALTER TABLE … FORCE ROW LEVEL SECURITY;`, `CREATE POLICY {policy} ON … USING ({col} = NULLIF(current_setting('app.workspace_id', true), '')::uuid);` (fail-closed on BOTH absent and empty context — the `NULLIF(..., '')` wrapper is REQUIRED so an empty `app.workspace_id` maps to NULL→zero rows instead of raising `''::uuid`; `policy_name` defaults to `f"{table_name}_workspace_isolation"`). Per contracts/rls.md. [analyze I2]
- [X] T009 [P] [US2] Create `libs/shared/app_shared/enums.py`: a string-backed enum base (`enum.StrEnum` / `class X(str, Enum)`); `enum_column(EnumType, **kw)` mapping to a **plain `String` column (never a Postgres-native ENUM)** with **application-side validation** — the column stores the enum's string value and a bind/validator coerces-and-validates the value against `EnumType` in the application (raising on out-of-set values), per doc §22 "enum-like values are string-backed columns validated in the application". (Chosen over `Enum(native_enum=False)` so rejection is deterministically application-layer, not a DB CHECK — [analyze A2].) Include the minimal core enum `RecordStatus { ACTIVE, ARCHIVED }` used by the demo table. Per contracts/enums.md.
- [X] T010 [US2] Create `libs/shared/app_shared/models/__init__.py` re-exporting `Base`, `metadata`, `NAMING_CONVENTION`, `TimestampMixin`, `TZDateTime`, `WorkspaceScopedBase`, and `emit_rls_policy` (depends on T007, T008). This is the public `app_shared.models` surface Alembic's `target_metadata` and every later model import from.

### Tests for User Story 2

- [X] T011 [P] [US2] Create `tests/unit/test_naming_convention.py`: build an ad-hoc `Table` on a fresh `MetaData(naming_convention=NAMING_CONVENTION)` (or on `Base.metadata`) with `UniqueConstraint("group_key", "code_a")` and `UniqueConstraint("group_key", "code_b")`; assert the two generated names are DISTINCT and equal `uq_<t>_group_key_code_a` / `uq_<t>_group_key_code_b` (FR-002, SC-003).
- [X] T012 [P] [US2] Create `tests/unit/test_ids.py`: assert `new_uuid7()` returns a `uuid.UUID` (`isinstance`), `.version == 7`, and that a sequence of calls is time-ordered (`str(a) <= str(b)` for `a` generated before `b`) (FR-003, SC-002 id-part).
- [X] T013 [P] [US2] Create `tests/unit/test_base_model.py`: assert assigning a naive `datetime` (`tzinfo is None`) through `TZDateTime.process_bind_param` raises `ValueError`, and an aware datetime is accepted; assert a small example model using `Base` + `TimestampMixin` exposes a UUIDv7 `id` PK and `created_at`/`updated_at` columns whose type renders `TIMESTAMPTZ`; **assert the structural guard fires — defining a model subclassing `Base` with a naive `DateTime()` (timezone-unaware) column raises at class/mapper configuration time** (FR-004, SC-002 ts-part). [analyze I1]
- [X] T014 [P] [US2] Create `tests/unit/test_rls_policy.py`: assert `emit_rls_policy("some_table")` returns strings containing `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, and the fail-closed predicate `NULLIF(current_setting('app.workspace_id', true), '')::uuid` — explicitly assert the `NULLIF(` wrapper and `, '')` are present so an empty context cannot raise `''::uuid` (FR-007). Pure string assertion, no DB. [analyze I2]
- [X] T015 [P] [US2] Create `tests/unit/test_enums.py`: assert `RecordStatus` members carry their string value, `enum_column` renders a plain `String` column (not a PG-native ENUM and not a DB `Enum`), and an out-of-set value is rejected by the application-side validator (FR-006). [analyze A2]

**Checkpoint**: US2 primitives import cleanly and all five unit tests pass with no database.

---

## Phase 4: User Story 3 — Reject invalid monetary values at the boundary (Priority: P2)

**Goal**: A `Money` SQLAlchemy `TypeDecorator` over `NUMERIC(18,4)` that refuses `float`, `NaN`/`Infinity`, and over-scale values (no silent rounding), and round-trips valid in-scale `Decimal` exactly.

**Independent Test**: `uv run pytest tests/unit/test_money.py -q` — `float`, `Decimal("NaN")`, `Decimal("Infinity")`, and `Decimal("1.23456")` each raise; `Decimal("19.99")`, `Decimal("0.0001")`, and `int` are accepted and returned as `Decimal`. No database required.

### Implementation for User Story 3

- [X] T016 [P] [US3] Create `libs/shared/app_shared/money.py`: `Money(TypeDecorator)` with `impl = Numeric(precision=18, scale=4, asdecimal=True)`, `cache_ok = True`; `process_bind_param` lets `None` pass, **rejects `float`**, coerces `Decimal`/`int` (and exact `str`) to `Decimal`, rejects non-finite via `not value.is_finite()`, rejects over-scale via `-value.as_tuple().exponent > 4` (raise `ValueError`); `process_result_value` returns the `Decimal` unchanged. Per contracts/money.md + research.md D4.

### Tests for User Story 3

- [X] T017 [P] [US3] Create `tests/unit/test_money.py`: cover the full contract table — `None` passes; `1.1` (float) rejected; `Decimal("NaN")`/`Decimal("Infinity")`/`Decimal("-Infinity")` rejected; `Decimal("1.23456")` (>4dp) rejected (not rounded); `Decimal("19.99")`, `Decimal("0.0001")`, `int` accepted; a valid in-scale `Decimal` round-trips exactly as `Decimal` (never float) (FR-005, SC-004).

**Checkpoint**: `test_money.py` passes; every rejection path raises, valid path returns `Decimal`.

---

## Phase 5: User Story 4 — Use database sessions safely under pooling & forking (Priority: P2)

**Goal**: Formalize the SPEC-01 engine hygiene (one lazy engine per process, fork-safe disposal, pooler-safe config) and add the basic connectivity check, while keeping the import boundary green.

**Independent Test**: `uv run pytest tests/unit/test_import_boundaries.py -q` plus a no-eager-engine assertion — importing `app_shared` / `app_shared.models` / `app_shared.database` creates 0 engines and pulls in no scrapy/twisted/playwright/scrape_core. Live `SELECT 1` is DEFERRED.

### Implementation for User Story 4

- [X] T018 [US4] Extend `libs/shared/app_shared/database.py`: add `check_connection() -> None` (raises on failure, returns `None` on success — pin this signature per contracts/config.md, [analyze A1]) that opens a session via the existing `get_session()` and executes `SELECT 1` (trivial, no schema dependency). Keep the lazy/one-per-process/fork-safe/pooler-safe engine unchanged (FR-008/009/010 already satisfied by SPEC-01; formalized here). Ensure `dispose_engine()` exists and resets the per-process engine/sessionmaker singletons to `None` (used by the fork hook). Per contracts/config.md (FR-015).

### Tests for User Story 4

- [X] T019 [US4] Extend `tests/unit/test_import_boundaries.py`: add the new `app_shared` submodules to the subprocess import check — `import app_shared.ids`, `app_shared.money`, `app_shared.enums`, `app_shared.models`, `app_shared.models.base`, `app_shared.models.rls` — and assert none of them leak scrapy/twisted/playwright or `scrape_core` (Principle I/V).
- [X] T020 [P] [US4] Add engine-hygiene unit tests (extend `tests/unit/test_config.py` or new `tests/unit/test_engine_hygiene.py`), all DB-independent: (a) importing `app_shared.database` and `app_shared.models` in a fresh subprocess creates no engine (module-level `_engine is None` until `get_engine()`) (FR-008, SC-005); (b) **fork-disposal (FR-009):** after forcing the engine singleton to be created, calling `dispose_engine()` resets the singletons to `None` (mock/stub the engine so no live DB is needed) and assert the Celery `worker_process_init` hook is wired to call it — [analyze G1]; (c) **pooler-safe config (FR-010):** assert the engine's `connect_args` disables server-side prepared statements (`prepare_threshold` is `None`) so it is safe under PgBouncer transaction pooling — [analyze G2].
- [ ] T021 [US4] ⏸ DEFERRED (needs live Postgres) — Create `tests/integration/test_db_connectivity.py`, marked to skip without a reachable Postgres / `MIGRATION_DATABASE_URL`: `check_connection()` opens a session and executes `SELECT 1` (FR-015, SC-007 connectivity part). Author now; runs on a PG-capable host. Leave unchecked.

**Checkpoint**: import-boundary + no-eager-engine tests pass here; connectivity test authored and skipped.

---

## Phase 6: User Story 1 — Apply schema changes through the one-shot migration job (Priority: P1) 🎯 MVP capstone

**Goal**: Wire Alembic against the shared metadata via the DIRECT migration URL, author the FIRST migration creating the single `_smoke_foundation` demonstration table (exercising UUIDv7 PK + `TIMESTAMPTZ` + `NUMERIC(18,4)` Money + string enum + two shared-first-column uniques), package the one-shot migrate job (image + compose service, direct to `postgres:5432`), and add the CI single-head guard.

**Depends on**: US2 (Base/metadata/mixins/enum), US3 (Money) — the demo model composes all of them. This is why US1, though P1, is authored after US2/US3.

**Independent Test (DB-independent part, runs here)**: `uv run alembic upgrade head --sql` renders `CREATE TABLE`, `TIMESTAMPTZ`, `NUMERIC(18, 4)`, `uuid`, and the two distinct unique names with no DB connection; `bash scripts/check_single_head.sh` exits 0. **Live part is DEFERRED.**

### Implementation for User Story 1

- [ ] T022 [US1] Create the demonstration/smoke model `libs/shared/app_shared/models/_smoke.py`: `_SmokeFoundation` extending `Base` + `TimestampMixin` (NOT `WorkspaceScopedBase` — deliberately non-domain, no isolation surface) with `group_key: UUID`, `code_a`/`code_b` (nullable text), `amount` (`Money`, nullable), `status` (`RecordStatus` via `enum_column`), and TWO `UniqueConstraint`s sharing the leading column: `("group_key", "code_a")` and `("group_key", "code_b")`. Import it in `app_shared/models/__init__.py` so it registers on `Base.metadata` (autogenerate + offline render see it). Depends on T007, T009, T010, T016. Per data-model.md "Demonstration / smoke table".
- [ ] T023 [US1] Create `/srv/crawmatic/crawmatic/alembic.ini` (repo root): standard Alembic config with `script_location = alembic`, `prepend_sys_path`, and NO hard-coded `sqlalchemy.url` (the URL comes from `MIGRATION_DATABASE_URL` in `env.py`). Per contracts/migration-job.md.
- [ ] T024 [P] [US1] Create `/srv/crawmatic/crawmatic/alembic/script.py.mako`: the standard Alembic revision template.
- [ ] T025 [US1] Create `/srv/crawmatic/crawmatic/alembic/env.py`: import `app_shared.models` and set `target_metadata = app_shared.models.Base.metadata`; `compare_type = True`; online mode builds its OWN engine from `Settings.MIGRATION_DATABASE_URL` (or a CLI `-x db_url=` override) — it MUST NOT call `app_shared.database.get_engine()` (that targets the pooler); offline mode (`--sql`) configures the context from the URL with `literal_binds`/`dialect_opts` and emits DDL without connecting; error clearly if no URL is available when running online. Depends on T004, T010, T022. Per contracts/migration-job.md + research.md D6.
- [ ] T026 [US1] Create the first migration `alembic/versions/<rev>_smoke_foundation.py` (down_revision = None; it is the head): `op.create_table("_smoke_foundation", …)` reproducing the demo table with the convention-generated constraint names, `TIMESTAMPTZ` columns, `NUMERIC(18, 4)` amount, `uuid` PK, and the two distinct unique names; provide a matching `downgrade()` that drops it (FR-016 transactional + downgrade path). Include a comment showing how a *future* workspace-owned table would call `emit_rls_policy(...)` (not applied here — demo table is non-domain). Depends on T022, T025. Per data-model.md + research.md D7.
- [ ] T027 [US1] Create `tests/unit/test_migration_offline.py`: run `alembic upgrade head --sql` (offline, no DB) via subprocess and assert the rendered SQL contains `CREATE TABLE`, `TIMESTAMPTZ`, `NUMERIC(18, 4)`, `uuid`, and BOTH distinct unique names (`uq__smoke_foundation_group_key_code_a`, `uq__smoke_foundation_group_key_code_b`); assert `alembic heads` reports exactly one head (FR-013, FR-014, SC-003 end-to-end).
- [ ] T028 [P] [US1] Create `scripts/check_single_head.sh`: run `alembic heads`, count heads, exit non-zero unless exactly ONE head is reported (DB-independent — reads migration files). Make it executable. Add a note in the script header / plan that it plugs into CI (the CI workflow runs `bash scripts/check_single_head.sh`) (FR-012, SC-006).
- [ ] T029 [US1] Create `apps/migrate/pyproject.toml`: a uv-workspace member package depending on `app_shared` (workspace) + `alembic>=1.13,<2`, so the migrate image installs from the root lockfile with the exact pinned versions. Per contracts/migration-job.md (§5).
- [ ] T030 [US1] Create `apps/migrate/Dockerfile`: `FROM python:3.13.5-slim-bookworm` (matching the other app images), copy `uv`, build context = repo root, `uv sync --package migrate` (or equivalent from the root lockfile), `CMD ["uv", "run", "alembic", "upgrade", "head"]`; reads `MIGRATION_DATABASE_URL` from the environment (direct to `postgres:5432`). Depends on T029. ⏸ Image build/run requires a Docker daemon — authored here, built on a PG-capable host.
- [ ] T031 [US1] Extend `/srv/crawmatic/crawmatic/docker-compose.yml`: add a ONE-SHOT `migrate` service — `build` from `apps/migrate/Dockerfile` (context `.`), `restart: "no"`, `command: alembic upgrade head` (or image default), `environment: MIGRATION_DATABASE_URL` pointing at `postgres:5432` directly (NOT `pgbouncer:6432`), `depends_on: postgres: {condition: service_healthy}`. Do NOT add any migration command to `api`/`scheduler`/`worker` — they keep `depends_on: pgbouncer` only. Per contracts/migration-job.md (FR-011).
- [ ] T032 [US1] Create `tests/integration/test_no_startup_migrations.py` (DB-independent — parses `docker-compose.yml`): assert no `api`/`scheduler`/`worker` service runs `alembic`/`migrate`/`upgrade` in its command or entrypoint, and that the `migrate` service is `restart: "no"` and connects to `postgres:5432` (not `pgbouncer`) (FR-011, SC-007 no-startup-migration part). This half of SC-007 IS verifiable here by reading compose.
- [ ] T033 [US1] ⏸ DEFERRED (needs live Postgres) — Create `tests/integration/test_migration_job.py`, marked to skip without a reachable Postgres / `MIGRATION_DATABASE_URL`: `alembic upgrade head` (online) against a real database brings it to head and the `_smoke_foundation` table exists; **also exercise the downgrade round-trip — `alembic downgrade base` then `upgrade head` succeeds (FR-016 downgrade path verified live)** (FR-011, FR-016, SC-001, US1 AS-1). Author now; leave unchecked. [analyze G3]
- [ ] T034 [US1] ⏸ DEFERRED (needs live Postgres + Docker) — Document/validate the compose one-shot run `docker compose run --rm migrate`: exits 0 after `alembic upgrade head`, connects directly to `postgres:5432`, and no app service migrated at startup (US1 AS-2/AS-3, SC-001). Record as a runbook step in quickstart.md §B (already drafted) and verify on a Docker-capable host. Leave unchecked.

**Checkpoint**: offline render + single-head guard pass here; the migrate image, compose service, and online upgrade are authored and marked DEFERRED for a Postgres/Docker host.

---

## Phase 7: Polish & Cross-Cutting Validation

**Purpose**: Prove the whole DB-independent surface green here and hand off the live items.

- [ ] T035 Run the full DB-independent quickstart (quickstart.md §A) here: `uv run pytest tests/unit -q` (all green), `uv run alembic upgrade head --sql` (renders expected DDL), `bash scripts/check_single_head.sh` (exit 0). Fix any failure before marking done.
- [ ] T036 [P] ⏸ DEFERRED (needs live Postgres) — RLS fail-closed *behavioral* check (optional, marked): on a PG host, apply `emit_rls_policy()` to a throwaway table; with no `SET LOCAL app.workspace_id` a select returns ZERO rows, with `SET LOCAL app.workspace_id = '<uuid>'` only matching rows return (FR-007 live confirmation, spec Edge Case). Author as a marked integration test; leave unchecked.
- [ ] T037 [P] Final scope sweep: grep the diff to confirm NO real domain table (`workspaces`/`users`/`products`/…) was introduced, no Postgres-native ENUM, no RLS applied to a real table, and the only `create_table` target is `_smoke_foundation` (Scope Boundary section).

---

## Dependencies & Execution Order

### Phase dependencies

- **Phase 1 Setup (T001–T003)**: no dependencies — start immediately. T003 depends on T001+T002.
- **Phase 2 Foundational (T004–T005)**: after Setup. `MIGRATION_DATABASE_URL` (T004) BLOCKS the Alembic engine (T025).
- **Phase 3 US2 (T006–T015)**: after Setup (needs `uuid6` from T003). T007 depends on T006; T010 depends on T007+T008; tests depend on their targets.
- **Phase 4 US3 (T016–T017)**: after Setup. Independent of US2 except sharing SQLAlchemy — can run in parallel with US2.
- **Phase 5 US4 (T018–T021)**: after Setup. T019 depends on the new submodules existing (T006–T010, T016). Otherwise independent.
- **Phase 6 US1 (T022–T034)**: DEPENDS on US2 (T007/T009/T010) + US3 (T016) for the demo model (T022), and on T004 for `env.py`. This is the capstone — author last.
- **Phase 7 Polish (T035–T037)**: after all implementation phases.

### User-story dependency notes

- **US2 (P1)** and **US3 (P2)** are independent of each other and of US4 — parallelizable.
- **US4 (P2)** depends only on the submodules it imports for the boundary test.
- **US1 (P1)** is priority-P1 but dependency-last: its demonstration migration composes US2 + US3 + the core enum. MVP is therefore **Setup + Foundational + US2 + US3 + US1** (US4's live connectivity is deferred).

### Parallel opportunities

- Setup: T001 ∥ T002.
- Foundational: T005 ∥ T004.
- US2: T006 ∥ T008 ∥ T009 (different files); after T007+T010, tests T011 ∥ T012 ∥ T013 ∥ T014 ∥ T015.
- US3 (T016/T017) runs in parallel with the whole of US2.
- US4 T020 ∥ other US4 tasks.
- US1: T024 ∥ T028 (template ∥ CI script); most other US1 tasks are sequential (shared files / build order).

---

## Parallel Example: User Story 2 primitives + tests

```bash
# After T007 (base.py) and T010 (models/__init__) exist, run all US2 unit tests together:
uv run pytest tests/unit/test_naming_convention.py \
              tests/unit/test_ids.py \
              tests/unit/test_base_model.py \
              tests/unit/test_rls_policy.py \
              tests/unit/test_enums.py -q
```

---

## Implementation Strategy

### MVP (the working migration system)

1. Phase 1 Setup → Phase 2 Foundational.
2. Phase 3 US2 (base primitives) + Phase 4 US3 (Money) — the pieces the demo table needs.
3. Phase 6 US1 (Alembic + first migration + migrate job + single-head guard).
4. **STOP & VALIDATE (here)**: `uv run pytest tests/unit -q`, `uv run alembic upgrade head --sql`, `bash scripts/check_single_head.sh`.
5. On a Postgres host: run the DEFERRED tasks (T021, T033, T034, T036) to close SC-001 / SC-007-live.

### Incremental delivery

- Setup + Foundational → primitives (US2/US3) → session hygiene (US4) → migration system (US1) → polish. Each phase leaves the DB-independent suite green.

---

## DEFERRED tasks (⏸ needs live Postgres — orchestrator tracks; DO NOT mark [X] here)

| Task | What it needs a live DB for | Maps to |
|------|-----------------------------|---------|
| **T021** | `check_connection()` executes `SELECT 1` on a real DB | FR-015, SC-007 (connectivity) |
| **T033** | `alembic upgrade head` (online) creates `_smoke_foundation` | FR-011, SC-001, US1 AS-1 |
| **T034** | `docker compose run --rm migrate` one-shot, direct to postgres:5432, no app-service startup migration | SC-001, US1 AS-2/AS-3 |
| **T036** | RLS fail-closed behavior (0 rows unset / matching rows with `SET LOCAL`) on a real table | FR-007 (live confirmation) |

Everything else runs in THIS environment (no Docker daemon / no live Postgres required).

---

## Requirements Coverage (FR-001…FR-016)

| Requirement | Task(s) |
|-------------|---------|
| FR-001 shared declarative Base + single metadata + naming convention | T007, T010, T011 |
| FR-002 naming incorporates ALL constrained columns (distinct names) | T007, T011, T027 |
| FR-003 app-generated UUIDv7 PK helper + base uses it | T006, T007, T012 |
| FR-004 TIMESTAMPTZ timestamps + naive forbidden at base | T007, T013 |
| FR-005 Money NUMERIC(18,4): reject NaN/Inf/over-scale/float | T016, T017 |
| FR-006 string-backed enums (no native ENUM) | T009, T015 |
| FR-007 WorkspaceScopedBase + emit_rls_policy (fail-closed) | T007, T008, T014, T036 |
| FR-008 one lazy engine per process, no import-time engine | T018, T020 |
| FR-009 fork-safe engine disposal | T018 (SPEC-01 `dispose_engine`, formalized), T020 |
| FR-010 pooler-safe engine config (no prepared stmts, SET LOCAL only) | T018 (SPEC-01 config, formalized), T005 |
| FR-011 Alembic migrations only via one-shot job, direct connect, no startup migration | T025, T030, T031, T032, T033 |
| FR-012 single linear history; CI fails on multiple heads | T028, T027 (single-head assert) |
| FR-013 demonstration/smoke migration + model | T022, T026, T027 |
| FR-014 env targets shared metadata; autogenerate honors names | T025, T007 |
| FR-015 basic connectivity check | T018, T021 |
| FR-016 migrations transactional + downgrade path | T026 |

## Success Criteria Coverage (SC-001…SC-007)

| Success Criterion | Task(s) | Runs here? |
|-------------------|---------|-----------|
| SC-001 migration job brings DB to head, creates demo table | T033, T034 | ⏸ DEFERRED (live PG) |
| SC-002 100% base tables get UUIDv7 PK + tz-aware timestamps; 0 naive | T007, T012, T013 | ✅ here |
| SC-003 two shared-first-column uniques → 2 distinct names, 0 collisions | T011, T027 | ✅ here |
| SC-004 100% NaN/Inf/over-scale rejected; valid decimals round-trip exactly | T016, T017 | ✅ here |
| SC-005 importing DB module creates 0 engines; one per process on first use | T020 | ✅ here |
| SC-006 >1 head fails CI 100% of the time | T028, T027 | ✅ here |
| SC-007 connectivity succeeds; no app service migrates at startup | T032 (no-startup, here), T021 (connectivity, live) | ✅ partial here / ⏸ connectivity DEFERRED |

---

## Notes

- `[P]` = different file, no incomplete-task dependency.
- `[US#]` labels map every user-story-phase task back to spec.md's user stories for traceability.
- DEFERRED tasks stay `- [ ]` (unchecked) until run on a Postgres/Docker-capable host — never mark them `[X]` in this build env.
- Commit after each task or logical group (the orchestrator commits; do not commit inside these tasks).
- Hard scope rule (repeat): the ONLY table created anywhere in this feature is `_smoke_foundation`. No real domain tables, no auth, no RLS on real tables, no business logic.
