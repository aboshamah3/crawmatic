"""A `Select`-only in-memory session double supporting `.order_by()` +
`.limit()` + a `tuple_(...) > tuple_(...)` keyset predicate.

Support for `tests/unit/test_alerts_router.py`'s SPEC-09 US2 T028 cases
(`GET /v1/alerts/current` + `GET /v1/alert-events`) — the two
cursor-paginated **list** endpoints. Neither of the two existing
SPEC-08/09 fakes covers this shape: `_jobs_fake_session.FakeOrmSession`
silently ignores `.order_by()`/`.limit()` (fine for the single-resource
lookups it was built for, wrong here); `_alerts_fake_session.
FakeAlertsSession` adds `Update`/`on_conflict_do_update` evaluation for
the `recompute_variant` task but is likewise `order_by`/`limit`-blind.
No prior SPEC unit-tests a cursor-paginated list route against a fake
session (verified before writing this), so this is new, purpose-built,
minimal support — not a rewrite of either existing fake.
"""

from __future__ import annotations

import operator
from typing import Any

from sqlalchemy import Select
from sqlalchemy.sql import operators as sa_operators
from sqlalchemy.sql.elements import False_, Null, True_


def _resolve_bind_value(node: Any) -> Any:
    return node.value if hasattr(node, "value") else node


def _eval_tuple_gt(clause: Any, obj: Any) -> bool:
    """Evaluate `tuple_(col_a, col_b) > tuple_(val_a, val_b)` (the keyset predicate)."""
    left_names = [c.name for c in clause.left.clauses]
    right_values = [_resolve_bind_value(v) for v in clause.right.clauses]
    actual = tuple(getattr(obj, name) for name in left_names)
    return actual > tuple(right_values)


def _eval_clause(clause: Any, obj: Any) -> bool:
    clauses = getattr(clause, "clauses", None)
    if clauses is not None:
        results = [_eval_clause(sub, obj) for sub in clauses]
        if clause.operator is sa_operators.and_:
            return all(results)
        if clause.operator is sa_operators.or_:
            return any(results)
        raise NotImplementedError(f"unsupported boolean operator {clause.operator!r}")

    op = clause.operator
    if op is operator.gt and hasattr(clause.left, "clauses"):
        return _eval_tuple_gt(clause, obj)

    column_name = clause.left.name
    actual = getattr(obj, column_name)

    if op is sa_operators.in_op:
        return actual in _resolve_bind_value(clause.right)
    if op is sa_operators.eq:
        return actual == _resolve_bind_value(clause.right)
    if op is sa_operators.is_:
        if isinstance(clause.right, Null):
            return actual is None
        if isinstance(clause.right, True_):
            return actual is True
        if isinstance(clause.right, False_):
            return actual is False
        return actual is _resolve_bind_value(clause.right)
    raise NotImplementedError(f"unsupported operator {op!r}")


class _FakeScalars:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return list(self._items)

    def first(self) -> Any | None:
        return self._items[0] if self._items else None

    def one_or_none(self) -> Any | None:
        if len(self._items) > 1:
            raise AssertionError("scalars().one_or_none(): multiple matching rows")
        return self._items[0] if self._items else None


class _FakeExecResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._items)

    def scalar_one_or_none(self) -> Any | None:
        if len(self._items) > 1:
            raise AssertionError("scalar_one_or_none: multiple matching rows")
        return self._items[0] if self._items else None

    def all(self) -> list[Any]:
        return list(self._items)


class FakeAlertsListSession:
    """Minimal `Select`-evaluating session double: `WHERE` + `ORDER BY` + `LIMIT`.

    Rows are stored per-model-class. `execute(select(Model)...)` filters
    by `whereclause` (`AND`/`OR` of `==`/`.in_()`/`.is_()`/the tuple-keyset
    `>`), sorts by `.order_by(...)` columns, then truncates to
    `.limit(...)`  — exactly the shape `list_current_alerts`/
    `list_alert_events` (`apps/api/app/routers/alerts.py`) issue via
    `app_shared.pagination`.
    """

    def __init__(self) -> None:
        self._rows: dict[type, list[Any]] = {}

    def seed(self, *objs: Any) -> None:
        for obj in objs:
            self._rows.setdefault(type(obj), []).append(obj)

    def execute(self, stmt: Select) -> _FakeExecResult:
        descriptions = stmt.column_descriptions
        entity = descriptions[0]["entity"]
        rows = list(self._rows.get(entity, []))

        where = stmt.whereclause
        if where is not None:
            rows = [row for row in rows if _eval_clause(where, row)]

        order_by = list(getattr(stmt, "_order_by_clauses", ()) or ())
        if order_by:
            keys = [col.name for col in order_by]
            rows.sort(key=lambda row: tuple(getattr(row, key) for key in keys))

        limit_clause = getattr(stmt, "_limit_clause", None)
        if limit_clause is not None:
            rows = rows[: _resolve_bind_value(limit_clause)]

        return _FakeExecResult(rows)
