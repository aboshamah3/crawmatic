"""network operation resource summaries (EPA C9)

Revision ID: d1f7a3c9e284
Revises: b3f0c95a7d21
Create Date: 2026-09-08 18:10:00.000000

EPA C9 (F14), plan §9.1, part 1 of 2. One table:
``network_operation_resource_summaries`` — the compressed form a browser
navigation's subresource children take when the
``network_operation_children`` retention family ages them out.

WHY A TABLE AND NOT A DELETE
----------------------------
A navigation on an asset-heavy page produces hundreds of child
``network_operations`` rows, and they dominate the ledger's row count
within days. After thirty days the only question those rows still answer
is "what did that navigation cost on the wire", and that answer is four
numbers. ``app_shared.maintenance.ledger_summaries`` writes those four
numbers here and deletes the children **in the same transaction**, so
the ledger keeps the fact and loses only the bulk.

The parent's own totals are untouched: the parent
``network_operations`` row is immutable by trigger and stays exactly as
it was closed. This table is additive.

SHAPE
-----
``parent_operation_id`` is the parent's ``network_request_id``, and
carries NO foreign key. Two independent reasons: the next revision
partitions ``network_operations`` (its only UNIQUE constraint then
includes ``created_at``, so a single-column reference is not expressible
at all), and a summary must outlive a parent whose 730-day partition is
eventually dropped — a cascade would delete the compressed evidence at
the moment it becomes the only evidence left.

``UNIQUE (parent_operation_id)`` is what makes summarisation idempotent:
a re-run of an already-summarised parent conflicts rather than
double-counting, and since the children were deleted in the same
transaction as the insert, "summary exists" and "children gone" cannot
disagree.

``bytes_by_host_class`` is JSONB keyed by a CLOSED set of host classes
(``first_party`` / ``third_party`` / ``unknown``), never by raw hostname
— an open-ended map would reintroduce the unbounded cardinality this
whole family exists to remove.

FLEET-OWNED, NO RLS
-------------------
No ``workspace_id`` and no ``emit_rls_policy``, exactly like the
``network_operations`` parent it describes. Classified ``GAP`` in
``scripts/rls_table_manifest.txt`` (tenant-LINKED evidence with no
tenant column, the same filing as ``network_operations`` itself);
explicit grants for all three application roles are in
``scripts/sql/grants_expected.yaml`` and applied by
``scripts/provision_db_roles.sql``.

NOT SEEDED
----------
No backfill. A parent with no summary row simply has not been summarised
yet, which is also the state of every parent whose children are still
present — the two are indistinguishable on purpose, because they are the
same fact.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd1f7a3c9e284'
down_revision: Union[str, Sequence[str], None] = 'b3f0c95a7d21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "network_operation_resource_summaries",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("parent_operation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("child_count", sa.Integer(), nullable=False),
        sa.Column(
            "bytes_by_host_class",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("bytes_total", sa.BigInteger(), nullable=False),
        sa.Column("duration_ms_sum", sa.BigInteger(), nullable=False),
        sa.Column("parent_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "child_count > 0",
            name="ck_network_operation_resource_summaries_count_positive",
        ),
        sa.CheckConstraint(
            "bytes_total >= 0",
            name="ck_network_operation_resource_summaries_bytes_nonneg",
        ),
        sa.CheckConstraint(
            "duration_ms_sum >= 0",
            name="ck_network_operation_resource_summaries_duration_nonneg",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_network_operation_resource_summaries"),
        sa.UniqueConstraint("parent_operation_id", name="uq_nors_parent_operation_id"),
    )
    # The summariser sweeps by the PARENT's clock, not by its own insert
    # time, so this is the index the sweep actually uses.
    op.create_index(
        "ix_nors_parent_created_at",
        "network_operation_resource_summaries",
        ["parent_created_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_nors_parent_created_at",
        table_name="network_operation_resource_summaries",
    )
    op.drop_table("network_operation_resource_summaries")
