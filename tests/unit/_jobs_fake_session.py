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
        entity = stmt.column_descriptions[0]["entity"]
        rows = list(self._rows.get(entity, []))
        where = stmt.whereclause
        if where is not None:
            rows = [row for row in rows if _eval_clause(where, row)]
        return _FakeExecResult(rows)
