"""strategy discovery state

Revision ID: 096ff6d343b5
Revises: b6e5d1c94a72
Create Date: 2026-09-07 22:02:44.931118

EPA B4 (F09): the durable cursor behind `STRATEGY_DISCOVERY_SCAN`
(`app.workers.tasks_strategy.strategy_discovery_scan`) -- the fleet-wide,
chunked sweep over `domain_strategy_profiles` rows stuck at
`DISCOVERY_REQUIRED`. Bounded to `Settings.
STRATEGY_DISCOVERY_MAX_DOMAINS_PER_RUN` profiles per invocation; this
table records how far a still-in-progress pass has gotten so a task time
limit or a worker restart mid-scan never loses its place or re-enqueues
an already-forwarded profile ("resumable long maintenance").

GLOBAL, no RLS -- the same shape as `maintenance_cadences`/
`rollup_watermarks`: the scan is one cross-tenant sweep for the whole
deployment, not per-tenant state, and there is no tenant CRUD surface for
it. `key` is the table's own primary key (no surrogate `id`), the same
`rollup_watermarks` shape -- this is a small, deployment-wide KV store of
named cursors, not an entity with identity separate from what it names.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '096ff6d343b5'
down_revision: Union[str, Sequence[str], None] = 'b6e5d1c94a72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: create ``strategy_discovery_state`` (global, no RLS)."""
    op.create_table(
        "strategy_discovery_state",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("cursor_profile_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("last_advanced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "advance_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("key", name="pk_strategy_discovery_state"),
    )


def downgrade() -> None:
    """Downgrade schema: drop ``strategy_discovery_state``."""
    op.drop_table("strategy_discovery_state")
