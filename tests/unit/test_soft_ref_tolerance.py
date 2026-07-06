"""Unit tests for `app_shared.maintenance.soft_refs` (SPEC-15 T032, US4,
contracts/soft-reference-tolerance.md, FR-022).

Pure, DB-independent (mirrors `test_retention_eligibility.py`'s
compiled-SQL-text + fake-session pattern):

* The rendered `count_tolerated_dangling_refs` statement text/shape --
  asserted via `_compiled`, no live DB.
* `count_tolerated_dangling_refs` against a minimal fake `Session`
  (no engine/connection/live DB): a non-zero count is returned as an
  `int` and treated purely informationally (no exception raised for a
  non-zero "dangling" count -- FR-022 never treats this as corruption).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.dialects import postgresql

from app_shared.maintenance.soft_refs import (
    _count_tolerated_dangling_refs_stmt,
    count_tolerated_dangling_refs,
)


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --- rendered statement shape ------------------------------------------------


def test_stmt_counts_match_current_prices_rows_with_no_resolvable_observation() -> None:
    sql = _compiled(_count_tolerated_dangling_refs_stmt())
    assert "COUNT(*)" in sql.upper()
    assert "FROM match_current_prices" in sql
    assert "observation_id IS NOT NULL" in sql
    assert "observation_id NOT IN" in sql
    assert "SELECT id FROM price_observations" in sql


# --- count_tolerated_dangling_refs against a fake session -------------------


@dataclass
class _FakeScalarResult:
    value: int

    def scalar(self):
        return self.value


class _FakeSoftRefSession:
    """Answers the dangling-count query with a canned count -- no
    engine/connection/live DB (mirrors `_FakeCoverageSession`)."""

    def __init__(self, count: int) -> None:
        self._count = count
        self.executed: list = []

    def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        self.executed.append(stmt)
        return _FakeScalarResult(self._count)


def test_returns_zero_when_no_dangling_refs_exist() -> None:
    session = _FakeSoftRefSession(0)
    assert count_tolerated_dangling_refs(session) == 0


def test_returns_positive_count_as_tolerated_not_an_error() -> None:
    # A non-zero count is the expected steady state once retention has
    # dropped a partition a winning observation pointed into (FR-022) --
    # returned as a plain int, never raised as an exception.
    session = _FakeSoftRefSession(7)
    assert count_tolerated_dangling_refs(session) == 7


def test_issues_exactly_one_query() -> None:
    session = _FakeSoftRefSession(3)
    count_tolerated_dangling_refs(session)
    assert len(session.executed) == 1
