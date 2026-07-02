# Phase 1 Data Model: Database Foundation

This feature introduces **no domain tables**. The "entities" here are the reusable ORM building blocks (base classes, mixins, types) every later table inherits, plus one tiny non-domain demonstration/smoke table used only to prove the machinery. All items live in `libs/shared/app_shared`.

Source authority: `PROJECT_SPEC.md` §19/§21/§22/§32; constitution Principles II/VII/VIII; spec FR-001…FR-015.

---

## Shared metadata & naming convention

**`app_shared/models/base.py` → `NAMING_CONVENTION` + `metadata`**

```text
metadata = MetaData(naming_convention=NAMING_CONVENTION)
NAMING_CONVENTION = {
  ix:  ix_%(table_name)s_%(column_0_N_name)s
  uq:  uq_%(table_name)s_%(column_0_N_name)s
  ck:  ck_%(table_name)s_%(constraint_name)s
  fk:  fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s
  pk:  pk_%(table_name)s
}
```

Rules:
- `ix`/`uq`/`fk` names incorporate **all** constrained columns (`column_0_N_name`), so `uq(workspace_id, external_id)` and `uq(workspace_id, sku)` on one table get distinct names (FR-002, SC-003). *Verified in this environment.*
- `ck` requires an explicit `name=` on each `CheckConstraint` (forcing function; unnamed check constraints fail to render).
- Single shared `metadata` object → Alembic `target_metadata` autogenerates using these names (FR-014).

---

## Entity: Base (declarative base)

**`app_shared/models/base.py` → `Base`**

- SQLAlchemy 2.0 `DeclarativeBase` bound to the shared `metadata` above.
- Carries a **UUIDv7 primary key** via a mixin/`mapped_column`:
  - `id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)`
  - `new_uuid7` from `app_shared/ids.py` (see ID entity). Postgres type `UUID`.
- All tables extend `Base` → inherit metadata, naming convention, and (via the PK mixin) the UUIDv7 id.

**Validation / invariants**
- PK is always a valid, application-generated, version-7, time-ordered UUID (FR-003, SC-002, AS-US2-1).
- PK is a normal `PrimaryKeyConstraint`; a later partitioned-table subclass may extend it to include the partition key (`(id, scraped_at)`) — the base does not preclude this (§22 partitioned rule, deferred).

---

## Entity: TimestampMixin

**`app_shared/models/base.py` → `TimestampMixin`**

Fields (both non-null, timezone-aware):
- `created_at: Mapped[datetime]` — `TZDateTime` (over `DateTime(timezone=True)` → `TIMESTAMPTZ`), default `now(tz=UTC)`.
- `updated_at: Mapped[datetime]` — same type, default `now(tz=UTC)`, `onupdate now(tz=UTC)`.

**Validation / invariants**
- Column type renders `TIMESTAMPTZ` (FR-004, SC-002).
- `TZDateTime.process_bind_param` raises `ValueError` on any `datetime` with `tzinfo is None` → **naive timestamps rejected at the boundary** (FR-004, AS-US2-2, spec Edge Case). Naive columns are unreachable through the mixin.

---

## Entity: WorkspaceScopedBase (RLS-ready mixin)

**`app_shared/models/base.py` → `WorkspaceScopedBase`**

- Adds `workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)`.
- No FK to `workspaces` yet (that table is SPEC-03); plain indexed UUID column. SPEC-03 adds the FK.
- Intended to be combined with `Base` for workspace-owned tables. **No real workspace-owned table is created in SPEC-02** (first use SPEC-03).

**Validation / invariants**
- Every workspace-owned table (later) carries `workspace_id` by construction (FR-007, Principle II, AS-US2-4).
- Pairs with `emit_rls_policy()` (below) in the same migration that creates a real workspace table (SPEC-03+).

---

## Entity: UUIDv7 id helper

**`app_shared/ids.py`**

- `new_uuid7() -> uuid.UUID` — returns `uuid6.uuid7()` (stdlib-`UUID`-compatible, version 7, time-ordered).
- Optional `uuid7_pk()` column factory returning the standard PK `mapped_column`.

**Validation / invariants**
- `new_uuid7().version == 7`; sequential calls are monotonic non-decreasing by string order (FR-003, AS-US2-1). *Verified in this environment.*
- Returns a `uuid.UUID` (`isinstance` true) → stored via SQLAlchemy `Uuid` in a Postgres `UUID` column.

---

## Entity: Money type

**`app_shared/money.py` → `Money(TypeDecorator)`**

- `impl = Numeric(precision=18, scale=4, asdecimal=True)` → Postgres `NUMERIC(18,4)`. `cache_ok = True`.

**Validation / invariants (`process_bind_param`)**
- `None` → passes (nullable columns allowed).
- `float` input → **rejected** (`TypeError`/`ValueError`) — never float (§19, FR-005).
- `Decimal`/`int`/(exact `str`) → coerced to `Decimal`.
- Non-finite (`NaN`, `Infinity`, `-Infinity`) → **rejected** (`ValueError`) (FR-005, AS-US3-1, SC-004).
- Over-scale (>4 fractional digits) → **rejected** (`ValueError`), never silently rounded (FR-005, AS-US3-2, SC-004).
- In-scale finite `Decimal` → stored and returned as `Decimal`; round-trips exactly, never a float (AS-US3-3, SC-004). `process_result_value` yields `Decimal`.

**Notes**: not a currency-conversion concept — currency code storage / cross-currency comparison is out of scope (§19, spec Key Entities). All DB-independent, unit-tested here.

---

## Entity: Core enum support

**`app_shared/enums.py`**

- `StrEnum`-based base (`class Foo(str, Enum)` / `enum.StrEnum`) for string-backed, app-validated enumerations.
- A column helper (e.g. `enum_column(EnumType)`) mapping to a `String`/`Enum(..., native_enum=False)` column that stores the enum's string value and validates membership in the application (no Postgres-native `ENUM` type).

**Validation / invariants**
- Stored as a string; invalid values rejected in the application (FR-006, §22 "enum-like = string-backed, app-validated").
- "Core enums" = the minimal foundational set needed as shared building blocks; the exact members are a light implementation detail (e.g. a generic `RecordStatus { ACTIVE, ARCHIVED }` used by the demo). Domain enums arrive with their tables in later specs (spec Assumptions).

---

## Helper: RLS policy DDL emitter

**`app_shared/models/rls.py` → `emit_rls_policy(table_name, *, workspace_column="workspace_id", policy_name=None) -> tuple[str, ...]`**

Returns DDL statement strings (for `op.execute(...)` inside an Alembic migration):
1. `ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;`
2. `ALTER TABLE {table} FORCE ROW LEVEL SECURITY;`
3. `CREATE POLICY {policy} ON {table} USING ({col} = NULLIF(current_setting('app.workspace_id', true), '')::uuid);`  ← `NULLIF(…, '')` so BOTH absent and empty context → NULL → zero rows (never raises `''::uuid`)

**Validation / invariants**
- Fail-closed: unset/empty `app.workspace_id` → `current_setting(..., true)` = `NULL` → predicate matches zero rows (FR-007, §32, spec Edge Case).
- Transaction-scoped context via `SET LOCAL app.workspace_id = '<uuid>'` (PgBouncer-safe).
- Unit-validated by asserting the rendered strings contain `ENABLE ROW LEVEL SECURITY`, `FORCE`, and the fail-closed `NULLIF(current_setting('app.workspace_id', true), '')::uuid` predicate. No real table receives this policy in SPEC-02.

---

## Entity: Demonstration / smoke table (NON-domain)

**Test-support model + first migration (`alembic/versions/<rev>_smoke_foundation.py`)**

A tiny, permanent, non-domain bookkeeping table that exercises the machinery end-to-end. Illustrative shape:

```text
_smoke_foundation
- id           UUID   pk        (UUIDv7 default, from Base)
- group_key    UUID             (leading column shared by two uniques)
- code_a       TEXT             nullable
- code_b       TEXT             nullable
- amount       NUMERIC(18,4)    nullable  (Money type)
- status       TEXT             (string-backed enum: RecordStatus)
- created_at   TIMESTAMPTZ      (TimestampMixin)
- updated_at   TIMESTAMPTZ      (TimestampMixin)
constraints:
- uq(group_key, code_a)   -> uq__smoke_foundation_group_key_code_a
- uq(group_key, code_b)   -> uq__smoke_foundation_group_key_code_b
```

**Purpose / invariants**
- Proves: UUIDv7 PK, `TIMESTAMPTZ` columns, `NUMERIC(18,4)` money, string-backed enum, and **two multi-col uniques sharing a first column → two distinct names** (FR-013, SC-003).
- **Not** workspace-owned (keeps SPEC-02 free of real isolation surface); the RLS helper is validated separately by rendered-DDL unit test.
- DB-independent proof: metadata name assertions + offline `alembic upgrade head --sql` render. Live proof (table actually created): integration test marked for a Postgres host.

---

## Relationships

- `Base` ← (`TimestampMixin`, `WorkspaceScopedBase`) are mixins composed by concrete models.
- `Base.metadata` is the single `target_metadata` Alembic autogenerates against.
- `_smoke_foundation` extends `Base` + `TimestampMixin` (not `WorkspaceScopedBase`).
- No inter-table relationships/FKs are introduced (no domain tables). `WorkspaceScopedBase.workspace_id` gains its FK in SPEC-03 when `workspaces` exists.

## Requirements coverage map

| Requirement | Entity/Helper |
|---|---|
| FR-001 shared base + metadata | Base, metadata |
| FR-002 all-columns naming | NAMING_CONVENTION |
| FR-003 UUIDv7 PK | ids.new_uuid7, Base PK |
| FR-004 TIMESTAMPTZ + naive guard | TimestampMixin, TZDateTime |
| FR-005 money | Money |
| FR-006 string-backed enums | enums |
| FR-007 workspace base + RLS DDL | WorkspaceScopedBase, emit_rls_policy |
| FR-008/009/010 engine hygiene | (extends SPEC-01 database.py; see contracts/config.md) |
| FR-011/012 migration job + single head | Alembic env.py, migrate service, check_single_head.sh (see contracts/migration-job.md) |
| FR-013 demo/smoke | _smoke_foundation + first migration |
| FR-014 autogenerate honors names | env.py target_metadata |
| FR-015 connectivity check | database.check_connection (see contracts/config.md) |
