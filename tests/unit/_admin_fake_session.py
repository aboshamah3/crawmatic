"""Fake session returning canned aggregate rows for the usage export.

The export is a single hand-written aggregate; there is nothing for a
generic fake ORM session to interpret. The router's job is window
validation, cursor handling, and row mapping, so the seam we fake is
"the statement returned these tuples".
"""

from __future__ import annotations

from typing import Any


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeUsageSession:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[Any] = []

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _Result:
        self.executed.append(statement)
        return _Result(self.rows)

    def commit(self) -> None:
        return None

    def flush(self) -> None:
        return None
