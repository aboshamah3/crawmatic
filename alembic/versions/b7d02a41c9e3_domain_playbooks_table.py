"""domain_playbooks_table

Revision ID: b7d02a41c9e3
Revises: 03dec3037c8f
Create Date: 2026-08-11 00:00:00.000000

2026-08-11 proxy-cost Fix 4 (PLAN_PROXY_COST_REDUCTION.md): the curated
fully-global per-domain scraping playbook. Deliberately NO workspace
column and NO RLS — operator-seeded reference data (the
``scripts/seed_domain_playbooks.sql`` companion), readable by every
workspace's resolution path, written by no tenant path (there is no
tenant CRUD surface for it at all).

Hand-authored (no live Postgres in this build environment); column
shapes reproduce ``app_shared.models.domain_playbooks`` exactly.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d02a41c9e3"
down_revision: Union[str, Sequence[str], None] = "03dec3037c8f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: create ``domain_playbooks`` (global, no RLS)."""
    op.create_table(
        "domain_playbooks",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("preferred_access_method", sa.String(length=32), nullable=False),
        sa.Column("scrape_profile_name", sa.Text(), nullable=True),
        sa.Column("access_policy_name", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_domain_playbooks"),
    )
    op.create_index(
        "uq_domain_playbooks_domain",
        "domain_playbooks",
        ["domain"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema: drop ``domain_playbooks``."""
    op.drop_index("uq_domain_playbooks_domain", table_name="domain_playbooks")
    op.drop_table("domain_playbooks")
