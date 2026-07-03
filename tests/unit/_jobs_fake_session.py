"""A tiny in-memory SQLAlchemy-``Session`` stand-in for SPEC-08 jobs unit tests.

Not a ``test_*.py`` module (pytest's default collection pattern skips
it) — a small shared support module for ``test_jobs_service.py``,
``test_jobs_dispatch_task.py``, and ``test_jobs_router.py``.

Unlike ``tests/unit/test_seed_bootstrap.py``'s hand-rolled
``FakeSession`` (which deliberately does *not* evaluate ``WHERE``
clauses — existence is controlled purely by what the fake is
pre-populated with), the jobs router/service/dispatch-task tests need
real negative cases (cross-workspace / missing id -> 404, `status ==
PENDING` filtering, `id.in_(...)`) that a non-evaluating fake can't
distinguish. This module implements a small, generic evaluator over the
exact ``Select`` shapes this codebase issues via
``app_shared.repository.scoped_select``/``scoped_get`` — chained
``.where(...)`` calls combining ``==``/``.in_()`` via ``AND`` — without
running any real SQL engine.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select
from sqlalchemy.sql import operators as sa_operators
from sqlalchemy.sql.elements import Null


def _resolve_bind_value(node: Any) -> Any:
    if hasattr(node, "value"):
        return node.value
    return node


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
    column_name = clause.left.name
    actual = getattr(obj, column_name)

    if op is sa_operators.in_op:
        return actual in _resolve_bind_value(clause.right)
    if op is sa_operators.eq:
        return actual == _resolve_bind_value(clause.right)
    if op is sa_operators.is_:
        if isinstance(clause.right, Null):
            return actual is None
        return actual is _resolve_bind_value(clause.right)
    raise NotImplementedError(f"unsupported operator {op!r}")


class _FakeScalars:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return list(self._items)

    def first(self) -> Any | None:
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


class FakeOrmSession:
    """Minimal ORM-shaped ``Session`` double: real ``WHERE`` evaluation, no DB.

    Rows are stored per-model-class; ``execute(select(Model)...)``
    filters the seeded/added rows for that class against the
    statement's ``whereclause`` (``AND``/``OR`` of ``==``/``.in_()``).
    ``add``/``flush`` assign a UUID id (mirroring the real
    ``app_shared.models.base.Base`` default) when one isn't already set.
    """

    def __init__(self) -> None:
        self._rows: dict[type, list[Any]] = {}
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.flush_count = 0
        self.committed = False

    def seed(self, *objs: Any) -> None:
        for obj in objs:
            self._rows.setdefault(type(obj), []).append(obj)

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        self._rows.setdefault(type(obj), []).append(obj)

    def flush(self) -> None:
        self.flush_count += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    def commit(self) -> None:
        self.flush()
        self.committed = True

    def execute(self, stmt: Select) -> _FakeExecResult:
        descriptions = stmt.column_descriptions
        entity = descriptions[0]["entity"]
        rows = list(self._rows.get(entity, []))
        where = stmt.whereclause
        if where is not None:
            rows = [row for row in rows if _eval_clause(where, row)]

        # Minimal ``GROUP BY <col>`` + ``COUNT(*)`` support — the only
        # aggregate shape this codebase issues
        # (``app_shared.jobs.targets.aggregate_counts``:
        # ``select(Model.col, func.count()).where(...).group_by(Model.col)``).
        group_by_clauses = list(getattr(stmt, "_group_by_clauses", ()) or ())
        if group_by_clauses:
            group_col_name = group_by_clauses[0].name
            counts: dict[Any, int] = {}
            for row in rows:
                key = getattr(row, group_col_name)
                counts[key] = counts.get(key, 0) + 1
            return _FakeExecResult(list(counts.items()))

        # A plain multi-column projection (e.g. ``select(Model.id,
        # Model.workspace_id)``, the ``_scan_job_refs`` maintenance-scan
        # shape) -- as opposed to a full-entity ``select(Model)`` -- is
        # distinguished by its column description ``expr`` no longer
        # being the entity class itself. Project tuples of attribute
        # values by column name instead of returning whole ORM rows.
        is_full_entity_select = len(descriptions) == 1 and (
            descriptions[0]["expr"] is descriptions[0]["entity"]
        )
        if not is_full_entity_select:
            column_names = [description["name"] for description in descriptions]
            rows = [tuple(getattr(row, name) for name in column_names) for row in rows]

        return _FakeExecResult(rows)
