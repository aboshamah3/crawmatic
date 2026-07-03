"""A tiny in-memory SQLAlchemy-``Session`` stand-in for SPEC-09 alerts unit tests.

Not a ``test_*.py`` module (pytest's default collection pattern skips
it) — shared support for ``test_price_analysis_task.py``. Extends the
SPEC-08 ``_jobs_fake_session.FakeOrmSession`` evaluator (``Select``
``WHERE`` clauses over ``==``/``.in_()``/``.is_()``) with the two extra
Core statement shapes ``recompute_variant`` issues that SPEC-08 never
needed: a plain ``update(Model).where(...).values(...)`` (the
currency-mismatch write-back) and a Postgres
``insert(...).on_conflict_do_update(index_elements=[...], set_={...})``
upsert (the ``variant_price_states``/``variant_alert_states`` upserts).
No real SQL engine — a small, generic evaluator over exactly the
statement shapes this codebase issues.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Select, Update
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.sql import operators as sa_operators
from sqlalchemy.sql.elements import False_, Null, True_
from sqlalchemy.sql.functions import Function


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
        if isinstance(clause.right, True_):
            return actual is True
        if isinstance(clause.right, False_):
            return actual is False
        return actual is _resolve_bind_value(clause.right)
    raise NotImplementedError(f"unsupported operator {op!r}")


def _is_excluded_column(value: Any) -> bool:
    table = getattr(value, "table", None)
    return table is not None and getattr(table, "name", None) == "excluded"


def _resolve_set_value(value: Any, insert_values: dict[str, Any]) -> Any:
    """Resolve one ``on_conflict_do_update(set_={...})`` RHS to a concrete value.

    Handles the three shapes this codebase's upserts use: a literal
    Python value, ``stmt.excluded.<col>`` (use the row's own insert
    value), and ``func.now()`` (stamp the fake session's current time).
    """
    if _is_excluded_column(value):
        return insert_values.get(value.name)
    if isinstance(value, Function) and value.name == "now":
        return datetime.now(timezone.utc)
    return value


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


class FakeAlertsSession:
    """Minimal ORM-shaped ``Session`` double for the ``recompute_variant`` task.

    Rows are stored per-model-class, keyed also by their mapped
    ``Table`` (so a Core ``Update``/``Insert`` statement — which only
    carries a ``Table``, never the ORM class — can find the right
    bucket). ``add``/``flush`` assign a UUID id (mirroring the real
    ``app_shared.models.base.Base`` default) when one isn't already set.
    """

    def __init__(self) -> None:
        self._rows: dict[type, list[Any]] = {}
        self._model_by_table: dict[Any, type] = {}
        self.added: list[Any] = []
        self.flush_count = 0
        self.committed = False

    def seed(self, *objs: Any) -> None:
        for obj in objs:
            model = type(obj)
            self._rows.setdefault(model, []).append(obj)
            self._model_by_table.setdefault(model.__table__, model)

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        model = type(obj)
        self._rows.setdefault(model, []).append(obj)
        self._model_by_table.setdefault(model.__table__, model)

    def flush(self) -> None:
        self.flush_count += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    def commit(self) -> None:
        self.flush()
        self.committed = True

    def _model_for_table(self, table: Any) -> type:
        model = self._model_by_table.get(table)
        if model is not None:
            return model
        # Fall back to the real ORM registry — a Core Update/Insert
        # statement only carries a `Table`, and this table may never
        # have had a row `seed`ed/`add`ed (e.g. the very first
        # `variant_price_states` upsert insert for a variant), so the
        # `seed`/`add`-populated `_model_by_table` cache alone isn't
        # enough to resolve it.
        from app_shared.models.base import Base

        # `stmt.table` on a Core Insert/Update is often an
        # `AnnotatedTable` wrapper (a distinct object from
        # `Model.__table__`), so compare by name — unique across this
        # codebase's single shared `metadata` — rather than identity.
        for mapper in Base.registry.mappers:
            if mapper.local_table.name == table.name:
                self._model_by_table[table] = mapper.class_
                return mapper.class_
        raise AssertionError(f"FakeAlertsSession: no ORM class maps to table {table.name!r}")

    def execute(self, stmt: Any) -> _FakeExecResult:
        if isinstance(stmt, Select):
            return self._execute_select(stmt)
        if isinstance(stmt, Insert):
            return self._execute_upsert(stmt)
        if isinstance(stmt, Update):
            return self._execute_update(stmt)
        raise NotImplementedError(f"FakeAlertsSession: unsupported statement {type(stmt)!r}")

    def _execute_select(self, stmt: Select) -> _FakeExecResult:
        descriptions = stmt.column_descriptions
        entity = descriptions[0]["entity"]
        rows = list(self._rows.get(entity, []))
        where = stmt.whereclause
        if where is not None:
            rows = [row for row in rows if _eval_clause(where, row)]
        return _FakeExecResult(rows)

    def _execute_update(self, stmt: Update) -> _FakeExecResult:
        model = self._model_for_table(stmt.table)
        rows = list(self._rows.get(model, []))
        where = stmt.whereclause
        matched = [row for row in rows if (where is None or _eval_clause(where, row))]

        values = {
            col.name: (bind.value if hasattr(bind, "value") else bind)
            for col, bind in stmt._values.items()
        }
        for row in matched:
            for field, value in values.items():
                setattr(row, field, value)
        return _FakeExecResult(matched)

    def _upsert_one_row(
        self,
        model: type,
        rows: list[Any],
        insert_values: dict[str, Any],
        conflict: Any,
    ) -> Any:
        key_fields = list(conflict.inferred_target_elements) if conflict is not None else []

        existing = None
        if key_fields:
            existing = next(
                (
                    row
                    for row in rows
                    if all(getattr(row, field) == insert_values.get(field) for field in key_fields)
                ),
                None,
            )

        if existing is None:
            obj = model(**insert_values)
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            if hasattr(obj, "created_at") and getattr(obj, "created_at", None) is None:
                obj.created_at = now
            if hasattr(obj, "updated_at") and getattr(obj, "updated_at", None) is None:
                obj.updated_at = now
            rows.append(obj)
            return obj

        if conflict is not None:
            for col_name, raw_value in conflict.update_values_to_set:
                setattr(existing, col_name, _resolve_set_value(raw_value, insert_values))
        return existing

    def _execute_upsert(self, stmt: Insert) -> _FakeExecResult:
        model = self._model_for_table(stmt.table)
        rows = self._rows.setdefault(model, [])
        conflict = stmt._post_values_clause

        # Single-row `.values({...})` -> `stmt._values` (a dict of
        # Column -> BindParameter). Multi-row `.values([{...}, {...}])`
        # (e.g. `app_shared.catalog.upsert.build_variants_upsert`, always
        # called with a list even for one row) -> `stmt._values` is
        # `None` and `stmt._multi_values` holds a one-tuple wrapping the
        # list of per-row dicts of Column -> raw value (no
        # `BindParameter` wrapping in that shape).
        if stmt._values is not None:
            insert_values = {
                col.name: (bind.value if hasattr(bind, "value") else bind)
                for col, bind in stmt._values.items()
            }
            obj = self._upsert_one_row(model, rows, insert_values, conflict)
            return _FakeExecResult([obj])

        row_dicts = stmt._multi_values[0] if stmt._multi_values else []
        results = []
        for row_dict in row_dicts:
            insert_values = {
                col.name: (bind.value if hasattr(bind, "value") else bind)
                for col, bind in row_dict.items()
            }
            results.append(self._upsert_one_row(model, rows, insert_values, conflict))
        return _FakeExecResult(results)
