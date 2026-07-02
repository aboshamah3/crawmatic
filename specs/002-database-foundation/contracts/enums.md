# Contract: String-backed enums

Module: `app_shared/enums.py`.

## Exposed symbols

- A string-backed enum base (`enum.StrEnum` / `class X(str, Enum)`).
- A column helper, e.g. `enum_column(EnumType, **kw)`, mapping to a `String`-backed column (SQLAlchemy `Enum(..., native_enum=False)` or plain `String` with app validation).
- A minimal "core enum" for the foundation/demo, e.g. `RecordStatus { ACTIVE, ARCHIVED }`.

## Guarantees

- Enum values are stored as their **string** value; **no** Postgres-native `ENUM` type is created (§22).
- Membership is validated in the application (invalid strings rejected before/at write).

## Scope

- "Core enums" = the small foundational set used as shared building blocks. Domain-specific enums (roles, statuses, access methods, …) are introduced with their tables in later specs.

## Consumers

- Demo table `status` column. Later domain models reuse the helper for their string-backed enums.

## Tests

- `tests/unit/` — enum stores/reads its string value; invalid value rejected; column renders as a non-native (string) type.
