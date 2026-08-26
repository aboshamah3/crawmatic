"""abuse limit counters

Revision ID: e92029e9902c
Revises: 42bb8b878fe9
Create Date: 2026-08-26 03:46:19.819539

EPA W5.5-L1 item 2. The fail-closed, Postgres-authoritative counter store
behind `apps/api/app/abuse_limit.py`.

The UNIQUE constraint on `(bucket_key, window_starts_at)` is LOAD-BEARING,
not decorative: the limiter's increment is a single
`INSERT ... ON CONFLICT DO UPDATE ... RETURNING`, and the database serialises
it on exactly this index. Drop the constraint and the limiter silently
degrades into the read-then-write race it exists to prevent — two callers
both counting 119 and both being admitted past a limit of 120.

`count` starts at 1, not 0: the attempt that creates the row is itself an
attempt, and starting at 0 would hand every caller one free request per
window.

The GRANT is ROLE-CONDITIONAL, and that is load-bearing
-------------------------------------------------------
A bare ``GRANT ... TO crawmatic_app`` aborts the whole ``alembic upgrade``
on any database where that role does not exist yet — a fresh CI database,
a throwaway scratch container, a developer's first bootstrap. The chain
then stops HERE, leaving every later revision unapplied, which is how a
grant statement turns into "the migration is broken" rather than "one
privilege is missing".

Wrapping it in a ``DO $$ ... IF EXISTS (SELECT FROM pg_roles ...)`` block
keeps the behaviour IDENTICAL where the role exists (the same GRANT runs,
in the same transaction) and degrades to a ``NOTICE`` where it does not.
Role provisioning (``scripts/provision_db_roles.py``) stays the operator's
step; this migration simply stops being the thing that fails when it has
not happened yet.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e92029e9902c'
down_revision: Union[str, Sequence[str], None] = '42bb8b878fe9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "api_abuse_limit_counters",
        sa.Column("bucket_key", sa.Text(), nullable=False),
        sa.Column("window_starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint(
            "bucket_key", "window_starts_at", name="pk_api_abuse_limit_counters"
        ),
    )
    # The retention sweep's access path (see the module this table backs,
    # `apps/api/app/abuse_limit.py`, for the sweep statement itself).
    op.create_index(
        "ix_api_abuse_limit_counters_window",
        "api_abuse_limit_counters",
        ["window_starts_at"],
    )
    # The API process runs as the ordinary app role. Without these grants the
    # limiter's very first write raises, and because it fails CLOSED that is a
    # 429 on every limited surface — so the grants are part of the migration,
    # not an operator step.
    #
    # Role-conditional (see the module docstring): identical behaviour where
    # `crawmatic_app` exists, a NOTICE instead of an aborted upgrade where it
    # does not. A role-less database must still be able to reach head.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'crawmatic_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON api_abuse_limit_counters
                TO crawmatic_app;
            ELSE
                RAISE NOTICE 'role crawmatic_app absent - skipping GRANT on '
                             'api_abuse_limit_counters (run '
                             'scripts/provision_db_roles.py before serving traffic)';
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_api_abuse_limit_counters_window", table_name="api_abuse_limit_counters"
    )
    op.drop_table("api_abuse_limit_counters")
