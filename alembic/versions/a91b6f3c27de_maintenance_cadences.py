"""maintenance_cadences

Revision ID: a91b6f3c27de
Revises: d4f1c07ae2b8
Create Date: 2026-08-15 00:00:00.000000

2026-08-15 readiness cycle, defect "daily maintenance may never fire":
the scheduler drove `partition_create` / `daily_rollup` / `retention_drop`
off in-process float accumulators that reset to 0.0 on every process
start and were persisted nowhere. With all three intervals at 86400s, any
container restart inside 24h reset the countdown -- so on Railway (where
a deploy IS a restart) the daily maintenance tasks could go arbitrarily
long without firing. Observed in production: no `2026_09` partition on any
of the four partitioned tables (a dated total write outage) and zero rows
ever produced in `variant_price_daily_rollups`.

This table stores each daily cadence's *deadline* instead of elapsed
process time, so a restart cannot reset it and an overdue cadence runs on
the next poll. `next_due_at` doubles as the multi-replica claim: exactly
one scheduler wins the atomic
`UPDATE ... WHERE cadence_key = :k AND next_due_at <= :now RETURNING id`.

Deliberately NO workspace column and NO RLS -- the same shape as
`domain_playbooks` / `proxy_circuit_breakers`. These are platform-level
housekeeping deadlines, not tenant data.

Rows are seeded here (rather than lazily on first use only) with an epoch
`next_due_at`, so the first scheduler boot after this migration runs all
three overdue tasks immediately instead of waiting a full interval.

Hand-authored (no live Postgres in this build environment); column shapes
reproduce `app_shared.models.maintenance_cadence` exactly.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a91b6f3c27de"
down_revision: Union[str, Sequence[str], None] = "d4f1c07ae2b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Must stay in sync with
#: `app_shared.models.maintenance_cadence.DURABLE_CADENCE_KEYS`.
_CADENCE_KEYS = ("partition_create", "daily_rollup", "retention_drop")


def upgrade() -> None:
    """Upgrade schema: create + seed ``maintenance_cadences`` (global, no RLS)."""
    op.create_table(
        "maintenance_cadences",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("cadence_key", sa.Text(), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_claimed_by", sa.Text(), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_maintenance_cadences"),
    )
    op.create_index(
        "uq_maintenance_cadences_cadence_key",
        "maintenance_cadences",
        ["cadence_key"],
        unique=True,
    )

    # Seed one born-due row per cadence. `gen_random_uuid()` (pgcrypto /
    # built-in since PG13) is used rather than the app's UUIDv7 helper
    # because a migration must not import application code; the id is
    # opaque and never joined on. `ON CONFLICT DO NOTHING` keeps a re-run
    # (or a row the runtime get-or-create already created) a no-op.
    for key in _CADENCE_KEYS:
        op.execute(
            sa.text(
                """
                INSERT INTO maintenance_cadences
                    (id, cadence_key, next_due_at, run_count, created_at, updated_at)
                VALUES
                    (gen_random_uuid(), :cadence_key,
                     TIMESTAMPTZ '1970-01-01 00:00:00+00', 0, now(), now())
                ON CONFLICT (cadence_key) DO NOTHING
                """
            ).bindparams(cadence_key=key)
        )


def downgrade() -> None:
    """Downgrade schema: drop ``maintenance_cadences``."""
    op.drop_index(
        "uq_maintenance_cadences_cadence_key", table_name="maintenance_cadences"
    )
    op.drop_table("maintenance_cadences")
