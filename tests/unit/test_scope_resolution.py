"""Unit tests for `app_shared.jobs.scopes.resolve_scope_matches` (SPEC-13
US2 T021, FR-010, `contracts/job-service-seam.md`).

No live DB: asserts the correct predicate branch is selected per
`ScrapeScope` member, and that `status == MatchStatus.ACTIVE` is
**always** present, by structurally walking the compiled
`sqlalchemy.Select` object's `whereclause` -- never executing against a
real engine/session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import BinaryExpression, Select
from sqlalchemy.sql import operators as sa_operators
from sqlalchemy.sql.elements import BindParameter, ColumnClause, Grouping
from sqlalchemy.sql.selectable import Exists, ScalarSelect

from app_shared.enums import MatchStatus, ScrapeScope
from app_shared.jobs.scopes import resolve_scope_matches
from app_shared.models.catalog import ProductGroupItem
from app_shared.models.competitors_matches import CompetitorProductMatch


@dataclass
class _CapturingSession:
    """A minimal session double that only records the `Select` it was
    asked to `execute` -- resolve_scope_matches's predicate-selection
    logic is fully exercised by the time `execute` is called, so this
    test never needs to evaluate rows."""

    captured: list[Select] = field(default_factory=list)

    def execute(self, stmt: Select) -> Any:
        self.captured.append(stmt)
        return _EmptyResult()


class _EmptyResult:
    def scalars(self) -> "_EmptyScalars":
        return _EmptyScalars()


class _EmptyScalars:
    def all(self) -> list[Any]:
        return []


def _leaf_comparisons(clause: Any) -> list[Any]:
    """Flatten a top-level AND `BooleanClauseList` down to its immediate
    conjuncts -- an `OR` sub-clause is deliberately kept intact as a
    single leaf (not flattened further) so its two-armed structure can
    still be inspected by the caller.

    `exists(...)` gets parenthesized into a `Grouping` wrapper when
    combined with other predicates via `.where(...)` — unwrap it so an
    `Exists` leaf is still recognized as such, not hidden inside an
    opaque `Grouping`.
    """
    if isinstance(clause, Grouping):
        return _leaf_comparisons(clause.element)
    clauses = getattr(clause, "clauses", None)
    if clauses is not None and getattr(clause, "operator", None) is sa_operators.and_:
        leaves: list[Any] = []
        for sub in clauses:
            leaves.extend(_leaf_comparisons(sub))
        return leaves
    return [clause]


def _column_name(side: Any) -> str | None:
    if isinstance(side, ColumnClause):
        return side.name
    return None


def _bind_value(side: Any) -> Any:
    if isinstance(side, BindParameter):
        return side.value
    return None


def _find_comparison(leaves: list[BinaryExpression], column_name: str) -> BinaryExpression:
    for leaf in leaves:
        if isinstance(leaf, BinaryExpression) and _column_name(leaf.left) == column_name:
            return leaf
    raise AssertionError(f"no comparison found on column {column_name!r} in {leaves!r}")


def _resolve(scope: ScrapeScope, target_id: uuid.UUID | None) -> tuple[_CapturingSession, Select]:
    session = _CapturingSession()
    workspace_id = uuid.uuid4()
    result = resolve_scope_matches(
        session, workspace_id=workspace_id, scope=scope, target_id=target_id
    )
    assert result == []  # the fake session always returns no rows
    assert len(session.captured) == 1
    return session, session.captured[0]


def _assert_status_active_always_applied(stmt: Select) -> None:
    leaves = _leaf_comparisons(stmt.whereclause)
    status_leaf = _find_comparison(leaves, "status")
    assert _bind_value(status_leaf.right) == MatchStatus.ACTIVE


def _assert_workspace_scoped(stmt: Select) -> None:
    leaves = _leaf_comparisons(stmt.whereclause)
    ws_leaf = _find_comparison(leaves, "workspace_id")
    assert ws_leaf.left.table.name == CompetitorProductMatch.__tablename__


def test_workspace_scope_is_base_only_no_extra_target_predicate() -> None:
    _session, stmt = _resolve(ScrapeScope.WORKSPACE, None)
    _assert_status_active_always_applied(stmt)
    _assert_workspace_scoped(stmt)

    leaves = _leaf_comparisons(stmt.whereclause)
    column_names = {
        _column_name(leaf.left) for leaf in leaves if isinstance(leaf, BinaryExpression)
    }
    # Only workspace_id (from scoped_select) + status (ACTIVE) — no scope-target predicate.
    assert column_names == {"workspace_id", "status"}


@pytest.mark.parametrize(
    "scope,column_name",
    [
        (ScrapeScope.COMPETITOR, "competitor_id"),
        (ScrapeScope.PRODUCT, "product_id"),
        (ScrapeScope.VARIANT, "product_variant_id"),
        (ScrapeScope.MATCH, "id"),
    ],
)
def test_id_equality_scopes_select_correct_column(
    scope: ScrapeScope, column_name: str
) -> None:
    target_id = uuid.uuid4()
    _session, stmt = _resolve(scope, target_id)
    _assert_status_active_always_applied(stmt)
    _assert_workspace_scoped(stmt)

    leaves = _leaf_comparisons(stmt.whereclause)
    target_leaf = _find_comparison(leaves, column_name)
    assert target_leaf.left.table.name == CompetitorProductMatch.__tablename__
    assert _bind_value(target_leaf.right) == target_id


def test_product_group_scope_uses_exists_with_both_membership_arms() -> None:
    target_id = uuid.uuid4()
    _session, stmt = _resolve(ScrapeScope.PRODUCT_GROUP, target_id)
    _assert_status_active_always_applied(stmt)
    _assert_workspace_scoped(stmt)

    leaves = _leaf_comparisons(stmt.whereclause)
    exists_clauses = [leaf for leaf in leaves if isinstance(leaf, Exists)]
    assert len(exists_clauses) == 1, f"expected exactly one EXISTS clause, got {leaves!r}"

    inner_select = exists_clauses[0].element
    assert isinstance(inner_select, ScalarSelect)
    inner_where = inner_select.element.whereclause
    inner_leaves = _leaf_comparisons(inner_where)

    # product_group_items.workspace_id == <workspace_id> (defense-in-depth scoping)
    ws_leaf = _find_comparison(
        [leaf for leaf in inner_leaves if isinstance(leaf, BinaryExpression)], "workspace_id"
    )
    assert ws_leaf.left.table.name == ProductGroupItem.__tablename__

    # product_group_items.product_group_id == target_id
    group_leaf = _find_comparison(
        [leaf for leaf in inner_leaves if isinstance(leaf, BinaryExpression)],
        "product_group_id",
    )
    assert group_leaf.left.table.name == ProductGroupItem.__tablename__
    assert _bind_value(group_leaf.right) == target_id

    # The OR of the two membership arms is present as a BooleanClauseList
    # (product_id match OR product_variant_id match), each guarded by an
    # IS NOT NULL check on the product_group_items side.
    or_clauses = [
        leaf
        for leaf in inner_leaves
        if getattr(leaf, "operator", None) is sa_operators.or_
    ]
    assert len(or_clauses) == 1, f"expected exactly one OR clause, got {inner_leaves!r}"

    or_clause = or_clauses[0]
    and_arms = list(or_clause.clauses)
    assert len(and_arms) == 2

    referenced_pairs: set[tuple[str, str]] = set()
    for and_arm in and_arms:
        for cmp in and_arm.clauses:
            if isinstance(cmp, BinaryExpression) and cmp.operator is sa_operators.eq:
                left_table = getattr(cmp.left, "table", None)
                right_table = getattr(cmp.right, "table", None)
                if left_table is not None and right_table is not None:
                    referenced_pairs.add((cmp.left.name, cmp.right.name))

    assert ("product_id", "product_id") in referenced_pairs
    assert ("product_variant_id", "product_variant_id") in referenced_pairs


def test_unsupported_scope_raises() -> None:
    session = _CapturingSession()
    with pytest.raises(ValueError):
        resolve_scope_matches(
            session, workspace_id=uuid.uuid4(), scope="NOT_A_SCOPE", target_id=None  # type: ignore[arg-type]
        )
