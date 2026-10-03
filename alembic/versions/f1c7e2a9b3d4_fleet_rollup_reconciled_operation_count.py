"""fleet_network_cost_rollups.reconciled_operation_count (E7.5, 2026-09-29)

Revision ID: f1c7e2a9b3d4
Revises: e6a1c0d4f2b9
Create Date: 2026-09-29 13:00:00.000000

The ops snapshot's reconciliation coverage summed a bucket's whole
`operation_count` as soon as ONE of its operations had a settlement
(`app_shared.opsmetrics.snapshot._collect_cost_rollups`), overstating
coverage. The rollup now records the exact number of settled operations per
bucket (`app_shared.netledger.rollups`), which needs this column.

Live-safe: `ADD COLUMN ... NOT NULL DEFAULT 0` with a constant default is a
catalog-only change on PostgreSQL >= 11 (no table rewrite, a brief ACCESS
EXCLUSIVE for the catalog update only); `fleet_network_cost_rollups` is small
(top-N buckets per day) and not partitioned. Existing rows read 0 until the
next rollup of their day re-computes them; the cadence re-rolls recent days
whose settlements arrive late (E7.3). No RLS change: the table is fleet-owned.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1c7e2a9b3d4"
down_revision: Union[str, Sequence[str], None] = "e6a1c0d4f2b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "fleet_network_cost_rollups",
        sa.Column(
            "reconciled_operation_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("fleet_network_cost_rollups", "reconciled_operation_count")
