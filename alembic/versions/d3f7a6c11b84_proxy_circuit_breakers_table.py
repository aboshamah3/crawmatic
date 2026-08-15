"""proxy_circuit_breakers_table

Revision ID: d3f7a6c11b84
Revises: b7d02a41c9e3
Create Date: 2026-08-15 00:00:00.000000

2026-08-15 production-readiness audit risk H3: the durable half of the
independent proxy-spend circuit breaker. Every pre-existing cost brake
is a Redis counter, which structurally cannot protect against Redis
failure -- the incident that blinds the ledger also removes the ceiling.
This table is the authoritative, non-Redis breaker state that
``app_shared.access.breaker`` reads on the scrape path and writes from
its leased evaluator.

Deliberately NO workspace column and NO RLS -- the same shape as
``domain_playbooks``. This is a platform-wide financial kill switch over
one operator-owned proxy account (spend is shared, so one workspace's
runaway loop spends every workspace's budget), with no tenant CRUD
surface. ``scope_key`` carries granularity instead, so a later
per-provider or per-domain breaker needs no migration.

Hand-authored (no live Postgres in this build environment); column
shapes reproduce ``app_shared.models.proxy_breaker`` exactly.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d3f7a6c11b84"
down_revision: Union[str, Sequence[str], None] = "b7d02a41c9e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: create ``proxy_circuit_breakers`` (global, no RLS)."""
    op.create_table(
        "proxy_circuit_breakers",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("trip_reason", sa.String(length=32), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("observed", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tripped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trip_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_proxy_circuit_breakers"),
    )
    op.create_index(
        "uq_proxy_circuit_breakers_scope_key",
        "proxy_circuit_breakers",
        ["scope_key"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema: drop ``proxy_circuit_breakers``."""
    op.drop_index(
        "uq_proxy_circuit_breakers_scope_key", table_name="proxy_circuit_breakers"
    )
    op.drop_table("proxy_circuit_breakers")
