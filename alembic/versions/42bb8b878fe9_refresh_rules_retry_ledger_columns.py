"""refresh_rules retry-ledger columns (EPA W4.2 owed revision)

Revision ID: 42bb8b878fe9
Revises: ccea48aab7b1
Create Date: 2026-08-26 03:40:00.000000

W4.2's `app_shared.scheduling.fair_queue.RetryLedger` keeps its
bounded-retry counters and dead-letter set IN-PROCESS by deliberate,
documented choice (that module's "Durability" docstring section) —
`refresh_rules` had no attempt counter to increment and that task added
no migration. This is the owed migration: four additive columns so a
future change can make the retry counter and dead-letter decision
survive a restart. Nothing reads or writes them yet — see
`app_shared.models.refresh_rules.RefreshRule`'s own docstring for the
full rationale; `fair_queue.py` itself is untouched by this change.

Purely additive (four nullable-or-defaulted columns + one partial
index) — no data migration, no existing column touched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '42bb8b878fe9'
down_revision: Union[str, Sequence[str], None] = 'ccea48aab7b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "refresh_rules",
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "refresh_rules",
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "refresh_rules",
        sa.Column("last_failure_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "refresh_rules",
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_refresh_rules_dead_lettered",
        "refresh_rules",
        ["dead_lettered_at"],
        postgresql_where=sa.text("dead_lettered_at IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_refresh_rules_dead_lettered", table_name="refresh_rules")
    op.drop_column("refresh_rules", "dead_lettered_at")
    op.drop_column("refresh_rules", "last_failure_error")
    op.drop_column("refresh_rules", "last_failure_at")
    op.drop_column("refresh_rules", "consecutive_failures")
