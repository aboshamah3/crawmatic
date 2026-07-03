# Contract: Cursor Pagination (`app_shared/pagination.py`)

Pure, framework-agnostic. Opaque **keyset** cursor over `(created_at, id)`. Satisfies FR-015, SC-009, §24. FastAPI wiring lives in the routers (`apps/api`).

## Constants
- `DEFAULT_LIMIT = 50`, `MAX_LIMIT = 500`.

## Functions
- `clamp_limit(requested: int | None) -> int` — `min(requested or DEFAULT_LIMIT, MAX_LIMIT)`; also floors at 1.
- `encode_cursor(created_at: datetime, id: uuid.UUID) -> str` — `base64url(json({"c": created_at.isoformat(), "id": str(id)}))`. Opaque token.
- `decode_cursor(token: str) -> tuple[datetime, uuid.UUID]` — validates shape; raises `InvalidCursor` (typed) on malformed/garbage input (→ router maps to `422`).
- `keyset_predicate(model, after: tuple[datetime, uuid.UUID])` — builds the SQLAlchemy tuple comparison `tuple_(model.created_at, model.id) > tuple_(c, id)` for the seek.

## List-endpoint algorithm (router)
1. `limit = clamp_limit(query.limit)`.
2. If `cursor`: `after = decode_cursor(cursor)`; add `keyset_predicate(...)`.
3. `scoped_select(Model, ws).order_by(created_at, id).limit(limit + 1)`.
4. If `len(rows) > limit`: `next_cursor = encode_cursor(rows[limit-1].created_at, rows[limit-1].id)`, trim to `limit`; else `next_cursor = None`.
5. Return `{items, next_cursor}`.

## Unit tests (no DB)
- `encode`→`decode` round-trip preserves `(created_at, id)`.
- Malformed / non-base64 / wrong-shape token → `InvalidCursor`.
- `clamp_limit`: `None→50`, `10→10`, `9999→500`, `0→1`.
- `keyset_predicate` renders a `(created_at, id) > (:p, :p)` tuple comparison.
