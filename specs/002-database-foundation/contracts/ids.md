# Contract: UUIDv7 identifiers

Module: `app_shared/ids.py`.

## Exposed symbols

```python
from app_shared.ids import new_uuid7  # () -> uuid.UUID
# optional: uuid7_pk() -> Mapped[uuid.UUID] column factory for the PK
```

## Guarantees

- `new_uuid7()` returns a `uuid.UUID` (`isinstance(x, uuid.UUID) is True`) with `.version == 7`.
- Values are **time-ordered**: for `a` generated before `b`, `str(a) <= str(b)` (monotonic non-decreasing). Good B-tree insert locality for insert-heavy tables (§21).
- Application-generated (available before flush). Stored via SQLAlchemy `Uuid` → Postgres native `UUID`.
- Backed by `uuid6.uuid7()` (`uuid6>=2025.0.1,<2026`, pure Python).

## Trade-off (documented, accepted — §21)

- UUIDv7 embeds creation time; public IDs disclose row-creation time. Acceptable for this product.

## Consumers

- `Base` uses `new_uuid7` as the PK `default`. Later models reuse it for any UUID defaults.

## Tests

- `tests/unit/test_ids.py` — version == 7, stdlib-UUID instance, time-ordering across sequential calls.
