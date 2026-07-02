# Contract: Model base, mixins & naming convention

Module: `app_shared/models/` (`base.py`, re-exported from `__init__.py`).

## Exposed symbols

```python
from app_shared.models import (
    Base,                 # DeclarativeBase bound to the shared metadata
    metadata,             # MetaData(naming_convention=NAMING_CONVENTION)
    NAMING_CONVENTION,    # dict[str, str], all 5 keys
    TimestampMixin,       # created_at / updated_at (TIMESTAMPTZ, naive-guarded)
    TZDateTime,           # TypeDecorator over DateTime(timezone=True)
    WorkspaceScopedBase,  # mixin adding workspace_id (RLS-ready)
)
```

## Naming convention (guarantee)

```python
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

- **Guarantee**: two multi-column unique constraints that share a leading column receive **distinct** deterministic names. For a table `t` with `uq(a, b)` and `uq(a, c)`: `uq_t_a_b` and `uq_t_a_c`. (Verified against SQLAlchemy in this environment.)
- Check constraints MUST be given an explicit `name=`.

## Base (guarantee)

- `Base` is a SQLAlchemy 2.0 `DeclarativeBase` using `metadata`.
- Every subclass gets an `id` primary key defaulting to a UUIDv7 (`app_shared.ids.new_uuid7`), stored as Postgres `UUID`.
- `pk` name is `pk_<table>`.

## TimestampMixin (guarantee)

- `created_at`, `updated_at`: `TIMESTAMPTZ`, non-null, UTC-aware defaults; `updated_at` has `onupdate`.
- Assigning a naive `datetime` (`tzinfo is None`) to a `TZDateTime` column raises `ValueError` at bind time. There is no path to a naive timestamp column through the mixin.

## WorkspaceScopedBase (guarantee)

- Adds `workspace_id: UUID NOT NULL` (indexed), no FK yet (SPEC-03 adds it).
- Combined with `Base` to define workspace-owned tables (first real use SPEC-03). Pairs with `emit_rls_policy()` (see `rls.md`).

## Consumers

- Every later ORM model imports `Base` (+ mixins). Alembic uses `Base.metadata` as `target_metadata`.

## Tests

- `tests/unit/test_naming_convention.py` — asserts the two-shared-first-column uniques get distinct names.
- `tests/unit/test_base_model.py` — asserts the naive-datetime guard raises and the demo model has a UUIDv7 pk + tz-aware timestamp columns.
