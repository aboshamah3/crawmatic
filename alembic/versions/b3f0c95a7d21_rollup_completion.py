"""rollup completion checkpoint (EPA C7)

Revision ID: b3f0c95a7d21
Revises: e2a4f80c6b71
Create Date: 2026-09-08 15:40:00.000000

EPA C7 (F12), plan §11. One table: ``rollup_completion``, one row per
rolled-up UTC day.

WHY
---
Before C7 a daily rollup was one indivisible unit of work: a Python loop
issuing ``1 + 3N`` statements for N (workspace, variant) pairs, inside a
single Celery invocation. Two things follow from that, and this table
fixes both.

1. **No durable partial progress.** A run killed at the 1,800 s
   ``time_limit`` had upserted some rows, but nothing recorded *which*,
   so the next invocation started the whole day again — and at fleet
   scale it would be killed at the same place forever, never finishing
   the day.
2. **No way to ask "is day D finished?"** Retention drops a
   ``price_observations`` partition once its days are rolled up. The
   watermark answers "how far has the cadence swept", which is a
   statement about the sweep, not about a particular day: a day the
   sweep passed while failing part-way through looks identical to one it
   completed. C8's retention gate reads ``complete`` here instead.

SHAPE
-----
``date`` (DATE) is the PRIMARY KEY — the natural key IS the identity, so
there is no surrogate ``id`` column at all (the ``rollup_watermarks``
precedent, ``05dfda7cdbb7``).

``last_key_workspace_id``/``last_key_variant_id`` (UUID, NULLABLE) are
the keyset cursor: the highest ``(workspace_id, product_variant_id)``
pair the last COMMITTED batch of that day covered. ``NULL`` means "start
from the beginning" — the day has not begun, or a deliberate recompute
reset it. Soft references (no FK): a cursor is a position in a scan, not
a relationship, and deleting a workspace must not delete the record of
how far a past day's rollup got.

``complete`` (BOOLEAN NOT NULL DEFAULT FALSE) is the column C8 reads.
The default is FALSE so a row cannot be born accidentally claiming a day
is finished — the only thing that may set it TRUE is a batch that
actually ran off the end of the day.

GLOBAL, NO RLS
--------------
No ``workspace_id`` and no ``emit_rls_policy`` — one rollup day is one
cross-tenant window for the whole deployment, the same shape and
rationale as ``rollup_watermarks``/``maintenance_cadences``. Classified
SYSTEM in ``scripts/rls_table_manifest.txt``; explicit grants for all
three application roles are in ``scripts/sql/grants_expected.yaml`` and
applied by ``scripts/provision_db_roles.sql`` (a new table missing from
those three files is exactly the defect the B10 rehearsal found: it
ships unreadable and unwritable).

NOT SEEDED
----------
No backfill of historical days. An absent row and an incomplete row are
both "do not assume this day is finished", which is the safe reading for
a retention gate; inventing ``complete=TRUE`` rows for days this code
never verified would be a lie that authorises a partition drop.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f0c95a7d21'
down_revision: Union[str, Sequence[str], None] = 'e2a4f80c6b71'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "rollup_completion",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("last_key_workspace_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("last_key_variant_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "complete", sa.Boolean(), nullable=False, server_default=sa.text("false")
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
        sa.PrimaryKeyConstraint("date", name="pk_rollup_completion"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("rollup_completion")
