"""domain rules fleet limits

Revision ID: e7b34c0af219
Revises: d3f7a1b62c85
Create Date: 2026-09-07 23:59:00.000000

EPA B5 (F10): the FLEET-wide per-domain limits table backing
`app_shared.limiter.fleet.admit_fleet`. One row per bare domain,
overriding the `FLEET_HOST_CONCURRENCY_DEFAULT` /
`FLEET_HOST_RATE_PER_MINUTE_DEFAULT` settings that host admission is
otherwise granted against.

GLOBAL, no RLS -- the same shape and the same reasoning as
`domain_playbooks`/`fleet_cost_budgets`: a domain's fleet ceiling is a
property of the domain and of the operator's whole fleet, not of any
workspace. It carries NO `workspace_id`, has no tenant CRUD surface, and
is filed SYSTEM in `scripts/rls_table_manifest.txt`. Deliberately
distinct from the tenant `domain_access_rules` table (workspace-scoped,
tenant-writable, answers "how fast may THIS workspace hit this
domain?"): one tenant may not be shown, let alone set, the ceiling every
other tenant shares.

Every non-key column is NULLABLE with no server default, and NULL means
"use the setting", never "unlimited". That is what makes the table
additive: EPA C1's later `request_timeout_seconds` is a plain
`ALTER TABLE ... ADD COLUMN` with no default -- catalog-only in
PostgreSQL 11+, no table rewrite, no backfill, and every existing row
keeps behaving exactly as it did.

No seed rows: an empty table means "every domain uses the defaults",
which is precisely the intended starting state.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e7b34c0af219'
down_revision: Union[str, Sequence[str], None] = 'd3f7a1b62c85'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: create ``domain_rules`` (global, no RLS)."""
    op.create_table(
        "domain_rules",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("fleet_concurrency", sa.Integer(), nullable=True),
        sa.Column("fleet_rate_per_minute", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_domain_rules"),
    )
    op.create_index(
        "uq_domain_rules_domain",
        "domain_rules",
        ["domain"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema: drop ``domain_rules``."""
    op.drop_index("uq_domain_rules_domain", table_name="domain_rules")
    op.drop_table("domain_rules")
