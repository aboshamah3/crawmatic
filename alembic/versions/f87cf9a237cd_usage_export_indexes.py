"""Covering indexes for the SaaS usage export (PLAN §7.2, risk P2).

Both tables are RANGE-partitioned (`request_attempts` on `created_at`,
`price_observations` on `scraped_at`), so an index created on the parent
propagates to every existing and future partition — this is why the
export can bound its window on the partition key and still get an
index-backed scan per pruned partition.

`(workspace_id, <time>)` and not `(<time>, workspace_id)`: the export
groups by workspace after pruning to the window's partitions, so
workspace is the selective leading column within a partition.

Revision ID: f87cf9a237cd
Revises: 03dec3037c8f
Create Date: 2026-08-11 00:52:44.085495

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f87cf9a237cd"
down_revision: Union[str, Sequence[str], None] = "03dec3037c8f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_request_attempts_workspace_id_created_at",
        "request_attempts",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_price_observations_workspace_id_scraped_at",
        "price_observations",
        ["workspace_id", "scraped_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_price_observations_workspace_id_scraped_at",
        table_name="price_observations",
    )
    op.drop_index(
        "ix_request_attempts_workspace_id_created_at", table_name="request_attempts"
    )
