"""refresh rule occurrences

Revision ID: d3f7a1b62c85
Revises: 096ff6d343b5
Create Date: 2026-09-07 23:41:02.118374

EPA B3 (F07): the scheduler's due-time identity ledger. One row per
occurrence a `refresh_rules` row was actually fired for, keyed by the
occurrence itself -- `(rule_id, scheduled_for)`, where `scheduled_for`
is the rule's `next_run_at` at claim time truncated to whole seconds.

`fire_refresh_rule` INSERTs this row BEFORE it creates the job, so the
primary key is what decides whether an occurrence is claimed: a second
scheduler replica firing the same occurrence gets an `IntegrityError`,
rolls back, and creates nothing. `FOR UPDATE SKIP LOCKED` plus the
`next_run_at <= now` recheck already narrows that race to microseconds;
this closes it durably, across processes and restarts, because Postgres
is the only participant that can see both claimants.

GLOBAL, no RLS -- the same shape as `maintenance_cadences`/
`rollup_watermarks`/`strategy_discovery_state`: scheduler bookkeeping
written only by the cross-tenant claim on the BYPASSRLS system session,
with no tenant CRUD surface (`scripts/rls_table_manifest.txt` files it
SYSTEM). `rule_id` is deliberately NOT a foreign key: this is an audit
of what the scheduler DID, and deleting a rule must not delete the
evidence of its firings.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd3f7a1b62c85'
down_revision: Union[str, Sequence[str], None] = '096ff6d343b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: create ``refresh_rule_occurrences`` (global, no RLS)."""
    op.create_table(
        "refresh_rule_occurrences",
        sa.Column("rule_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scrape_job_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "rule_id", "scheduled_for", name="pk_refresh_rule_occurrences"
        ),
    )
    op.create_index(
        "ix_refresh_rule_occurrences_fired_at",
        "refresh_rule_occurrences",
        ["fired_at"],
    )


def downgrade() -> None:
    """Downgrade schema: drop ``refresh_rule_occurrences``."""
    op.drop_index(
        "ix_refresh_rule_occurrences_fired_at",
        table_name="refresh_rule_occurrences",
    )
    op.drop_table("refresh_rule_occurrences")
