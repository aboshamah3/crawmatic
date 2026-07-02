"""Naming-convention disambiguation test (FR-002, SC-003).

The key proof for this feature: two multi-column unique constraints
sharing a leading column must get DISTINCT, deterministic names under
``NAMING_CONVENTION`` (the built-in ``column_0_N_name`` token expands to
ALL constrained columns, unlike the SQLAlchemy default which only uses
the first column and would collide).
"""

from __future__ import annotations

from sqlalchemy import Column, MetaData, String, Table, Uuid, UniqueConstraint

from app_shared.models import NAMING_CONVENTION


def test_two_shared_first_column_uniques_get_distinct_names() -> None:
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    table = Table(
        "widgets",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("group_key", Uuid, nullable=False),
        Column("code_a", String, nullable=True),
        Column("code_b", String, nullable=True),
        UniqueConstraint("group_key", "code_a"),
        UniqueConstraint("group_key", "code_b"),
    )

    unique_names = sorted(
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    )

    assert unique_names == [
        "uq_widgets_group_key_code_a",
        "uq_widgets_group_key_code_b",
    ]
    # Explicitly: distinct, not colliding on the shared leading column.
    assert len(set(unique_names)) == 2
