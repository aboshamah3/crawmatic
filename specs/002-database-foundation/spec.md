# Feature Specification: Database Foundation

**Feature Branch**: `002-database-foundation`

**Created**: 2026-07-02

**Status**: Draft

**Input**: SPEC-02 from PROJECT_SPEC.md §35 — create the DB foundation and migration system (reusable patterns only; no domain tables).

## Clarifications

### Session 2026-07-02

All items below were resolved directly from the master specification (`PROJECT_SPEC.md`) and the existing SPEC-01 skeleton; no open ambiguities required a stakeholder decision.

- Q: Sync or async SQLAlchemy, and which driver? → A: Synchronous SQLAlchemy 2.0 over psycopg 3 — the engine/session pattern already established in the SPEC-01 skeleton (`app_shared/database.py`); this spec formalizes it (source: doc §3 stack, SPEC-01 plan). Reactor-safe async/deferToThread writes are a scrape-core concern for a later spec.
- Q: How are UUIDv7 IDs produced? → A: Application-generated UUIDv7 as the primary-key default; the specific generation library is a plan-level choice (source: doc §21).
- Q: Money storage and validation? → A: `NUMERIC(18,4)`, `Decimal` in Python, finite-only (reject NaN/Infinity), reject values exceeding scale (no silent rounding), never float (source: doc §19).
- Q: Enum representation? → A: String-backed columns validated in the application; no database-native enum types (source: doc §22).
- Q: Migration routing and history? → A: Migrations run only as the dedicated one-shot job connected directly to Postgres (not via PgBouncer); application services never migrate at startup; single linear history with a CI guard that fails on multiple heads (source: doc §22, §4, §6).
- Q: RLS delivery at the foundation stage? → A: Deliver an RLS-ready workspace-scoped base (adds `workspace_id`) plus a reusable helper that emits RLS policy DDL (enable RLS; fail closed when `app.workspace_id` is absent/empty; `SET LOCAL` transaction-scoped for pooler-safety). The first real workspace-owned table and its policy are SPEC-03 (source: doc §32).
- Q: What proves the machinery without real domain tables? → A: A demonstration/smoke model + migration exercising UUIDv7 PK, `TIMESTAMPTZ`, and the two-uniques-sharing-a-first-column naming case; the exact form (bookkeeping smoke table vs. metadata-only unit demonstration), the Alembic config specifics, the UUIDv7 library, and the precise set of "core enums" are plan-level implementation details (source: doc §22 conventions + §35 scope; deferred to `/speckit-plan`).
- Q: How are live-Postgres acceptance items handled here? → A: Authored and unit/statically validated in this environment (no running container engine); the migration-job run and connectivity check execute on a Postgres-capable host. DB-independent behavior (UUIDv7, money validation, naming-convention name generation, no-eager-engine) is fully verifiable here (source: project constraint — no Docker daemon in build env).

## User Scenarios & Testing *(mandatory)*

The "users" of this feature are the developers who will build every workspace-owned table in later specs and the operators who run schema migrations in each environment. This feature delivers the reusable database machinery — models base, ID/timestamp/money/enum conventions, an RLS-ready workspace base, pooler-safe sessions, and the one-shot migration system — so that every later table is correct-by-construction. It introduces **no domain tables and no business logic**.

### User Story 1 - Apply schema changes through the one-shot migration job (Priority: P1)

An operator applies pending schema changes by running a single dedicated migration job that connects directly to the database (bypassing the connection pooler), and application services never migrate themselves at startup, so schema evolution is deterministic and safe under pooled connections.

**Why this priority**: Every later spec adds tables via migrations; without a working, correctly-wired migration system nothing downstream can ship. This is the foundational MVP slice.

**Independent Test**: Run the migration job against a database and confirm it upgrades to the latest revision, creates the demonstration table(s), and connects directly (not through the pooler); confirm no application service performs migrations on boot.

**Acceptance Scenarios**:

1. **Given** a database at an older (or empty) revision, **When** the dedicated migration job runs, **Then** it applies all pending migrations to head and the target tables exist.
2. **Given** the migration job configuration, **When** it connects, **Then** it connects directly to the database, not through the connection pooler.
3. **Given** any application service starts, **When** it boots, **Then** it does not run migrations.
4. **Given** the migration history, **When** validated in continuous integration, **Then** a single linear history is enforced and multiple heads cause a failure.

### User Story 2 - Define new tables that are correct-by-construction (Priority: P1)

A developer defines a new table by extending the shared model base, and automatically gets a UUIDv7 primary key, UTC timestamp columns, deterministic constraint/index names that account for every constrained column, and (for workspace-owned tables) a `workspace_id` column plus row-level isolation — so the non-negotiable conventions cannot be forgotten per table.

**Why this priority**: The value of a "foundation" is that correctness is inherited, not re-implemented. Getting the base patterns right here prevents whole classes of defects (naive timestamps, colliding constraint names, missing isolation) across all later specs.

**Independent Test**: Define a demonstration model with two multi-column unique constraints that share the same leading column; confirm each gets a distinct generated name, its primary key is a UUIDv7, and its timestamp columns are timezone-aware.

**Acceptance Scenarios**:

1. **Given** a model using the shared base, **When** a row is created, **Then** its primary key is a valid, time-ordered UUIDv7 generated by the application.
2. **Given** a model with timestamp columns, **When** the schema is created, **Then** every timestamp column is timezone-aware (UTC); naive timestamp columns are rejected/forbidden at the base level.
3. **Given** a table with two multi-column unique constraints sharing the same first column (e.g. `(workspace_id, external_id)` and `(workspace_id, sku)`), **When** names are generated, **Then** the two constraints receive distinct, deterministic names.
4. **Given** a workspace-owned model, **When** it is defined via the workspace-scoped base, **Then** it carries a `workspace_id` and the machinery to enable row-level isolation exists.

### User Story 3 - Reject invalid monetary values at the boundary (Priority: P2)

A developer stores a monetary amount and the system guarantees it is a finite, correctly-scaled decimal — non-finite values (NaN/Infinity) and over-precise values are refused rather than silently stored or rounded — so a wrong price can never enter the database.

**Why this priority**: Monetary correctness is a core principle, but it builds on the base-model machinery rather than preceding it.

**Independent Test**: Attempt to persist NaN, Infinity, and an over-scale decimal through the money type; confirm each is rejected, and a valid finite decimal within scale is accepted and round-trips exactly.

**Acceptance Scenarios**:

1. **Given** the money type, **When** a value of NaN or Infinity is assigned, **Then** it is rejected at the type boundary.
2. **Given** the money type with a fixed scale, **When** a value with more decimal places than the scale is assigned, **Then** it is rejected (not silently rounded).
3. **Given** a valid finite decimal within scale, **When** stored and retrieved, **Then** it round-trips exactly as a decimal (never a floating-point value).

### User Story 4 - Use database sessions safely under pooling and forking (Priority: P2)

A developer obtains a database session/engine and it behaves correctly under transaction pooling and process forking — one engine per process created lazily on first use, disposed after a worker forks, and free of pooler-incompatible features — so connection handling never leaks or breaks in the pooled, multi-process deployment.

**Why this priority**: Correct session handling is required for any real DB access, but it extends the engine stub already established in the skeleton.

**Independent Test**: Confirm importing the shared package creates no engine; confirm the engine is created only on first use and is one-per-process; confirm a fork-time disposal hook exists; confirm pooler-incompatible features (e.g. server-side prepared-statement caching, session-scoped state) are disabled/avoided.

**Acceptance Scenarios**:

1. **Given** the shared database module, **When** it is imported, **Then** no engine or connection is created.
2. **Given** the first use of a session, **When** the engine is needed, **Then** exactly one engine is created for the process and reused thereafter.
3. **Given** a worker process forks, **When** it initializes, **Then** any inherited engine is disposed before first use.
4. **Given** transaction-pooling operation, **When** the engine is configured, **Then** pooler-incompatible features are disabled and only transaction-scoped constructs (e.g. `SET LOCAL`, transaction-scoped advisory locks) are used.

### Edge Cases

- What happens when two multi-column unique constraints share the same leading column? Both must receive distinct, deterministic names (a first-column-only naming scheme would collide).
- What happens when a developer adds a naive (timezone-less) timestamp column? It must be prevented at the base-model level, not left to per-table discipline.
- What happens when migrations produce two heads (e.g. two branches merged)? CI must fail on multiple heads to preserve a single linear history.
- What happens when an application service is (mis)configured to migrate at startup? The design must make migrations the exclusive responsibility of the one-shot job.
- What happens when a query runs against a workspace-owned table without a workspace context set? The isolation design must fail closed (match zero rows) once workspace-owned tables exist — the base and helper delivered here must support that.
- What happens when the migration job connects through the pooler by mistake? Non-transactional DDL and session-scoped advisory locks are unsafe there — the job must be wired to connect directly.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a shared SQLAlchemy declarative base (single shared metadata) that all models extend, carrying a deterministic constraint/index naming convention.
- **FR-002**: The naming convention MUST generate constraint/index names that incorporate **all** constrained columns (not only the first), so two multi-column unique constraints sharing the same leading column receive distinct names.
- **FR-003**: The system MUST provide an application-generated UUIDv7 identifier helper, and the base model MUST use it for primary keys (time-ordered, globally unique, generated in the application).
- **FR-004**: The base model MUST provide timezone-aware (UTC, `TIMESTAMPTZ`) timestamp columns (at minimum created/updated), and MUST forbid naive timestamp columns at the base level so per-table code cannot introduce them.
- **FR-005**: The system MUST provide a money type that stores values as fixed-scale decimals (`NUMERIC(18,4)`), rejecting non-finite values (NaN, Infinity) and values exceeding the defined scale, and never representing money as a floating-point value.
- **FR-006**: The system MUST provide string-backed enumeration support for the core enums, validated in the application (no database-native enum types).
- **FR-007**: The system MUST provide a workspace-scoped model base that adds a `workspace_id` column and is RLS-ready, plus a reusable helper that emits row-level-security policy DDL (enabling RLS, fail-closed on absent/empty workspace context) for use in the migration that creates a workspace-owned table.
- **FR-008**: The system MUST provide database session/engine handling that creates exactly one engine per process, lazily on first use (never at import time, never per request), reused across the process.
- **FR-009**: The session/engine handling MUST be fork-safe: a hook disposes any engine inherited across a process fork before first use.
- **FR-010**: The engine MUST be configured for transaction-pooling operation: pooler-incompatible features (server-side prepared-statement caching, session-scoped state that must survive across statements) are disabled or avoided; only transaction-scoped constructs (`SET LOCAL`, transaction-scoped advisory locks) are relied upon.
- **FR-011**: The system MUST provide an Alembic-based migration system whose migrations run **only** as a dedicated one-shot job that connects **directly** to the database (not through the connection pooler); application services MUST NOT run migrations at startup.
- **FR-012**: The migration system MUST maintain a single linear migration history, and continuous integration MUST fail when multiple heads exist.
- **FR-013**: The system MUST include at least one demonstration/smoke migration and model that exercises the foundation (UUIDv7 primary key, timezone-aware timestamps, and the two-unique-constraints-sharing-a-first-column naming case) so the machinery is provably correct, WITHOUT introducing real domain tables or business logic.
- **FR-014**: The migration environment MUST autogenerate/target the shared metadata and honor the naming convention, so autogenerated migrations reflect the deterministic names.
- **FR-015**: The system MUST provide a basic database connectivity check that verifies a session can connect and execute a trivial statement.

### Key Entities *(include if feature involves data)*

- **Model base**: The shared declarative foundation carrying metadata, the naming convention, the UUIDv7 primary key, and UTC timestamp columns that every table inherits.
- **Workspace-scoped model base**: A specialization of the model base adding `workspace_id` and RLS-readiness for workspace-owned tables (first concrete use in SPEC-03).
- **Money value**: A finite, fixed-scale decimal amount (`NUMERIC(18,4)`) with boundary validation; not a currency-conversion concept (no cross-currency comparison in v1).
- **Migration revision**: A node in the single linear migration history applied exclusively by the one-shot migration job.
- **Core enum**: A string-backed, application-validated set of allowed values.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Running the dedicated migration job against a database brings it to the latest revision and creates the demonstration table(s) with zero manual steps beyond invoking the job.
- **SC-002**: 100% of tables defined via the shared base receive a UUIDv7 primary key and timezone-aware timestamp columns; 0 naive timestamp columns are possible without an explicit override that the base rejects.
- **SC-003**: For a table with two multi-column unique constraints sharing the same leading column, 2 distinct constraint names are generated (0 collisions).
- **SC-004**: 100% of NaN, Infinity, and over-scale monetary inputs are rejected at the type boundary; valid in-scale decimals round-trip exactly with no floating-point representation.
- **SC-005**: Importing the shared database module creates 0 engines/connections; the engine is created exactly once per process on first use.
- **SC-006**: A migration history with more than one head fails the continuous-integration check 100% of the time.
- **SC-007**: The basic connectivity check succeeds against a running database, and no application service performs migrations at startup.

## Assumptions

- The database is PostgreSQL, reached by application services through the connection pooler; the one-shot migration job is the single component that connects directly to PostgreSQL (per PROJECT_SPEC §4/§6). Only the foundation and its wiring are in scope here.
- This spec delivers reusable patterns and a demonstration/smoke table only; the first real domain tables (workspaces, users, products, …) and any RLS policies on real tables are SPEC-03+.
- The money scale is `NUMERIC(18,4)` per PROJECT_SPEC §19; currency handling beyond "store the code, never compare across currencies in v1" is out of scope.
- "Core enums" means the small set of foundational, string-backed status/type enumerations needed as shared building blocks; domain-specific enums are introduced with their tables in later specs.
- The build/CI environment may not have a live PostgreSQL available (no running container engine here); acceptance items that require a live database (running the migration job, the connectivity check) are authored and unit/statically validated in this environment and executed on a PostgreSQL-capable host. DB-independent behavior (UUIDv7 generation, money validation, naming-convention name generation, no-eager-engine) is fully verifiable here.
- The engine hygiene and fork-safety hook established in the SPEC-01 skeleton are extended/formalized here, not rebuilt from scratch.
