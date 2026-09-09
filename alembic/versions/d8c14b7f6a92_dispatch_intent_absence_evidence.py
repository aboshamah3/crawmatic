"""dispatch_intents absence ledger — absent_observations/first_absent_at/last_seen_at

Revision ID: d8c14b7f6a92
Revises: 4b06b233e0b8
Create Date: 2026-09-09 00:00:00.000000

R11 (2026-09-09): the schema half of "a single missing entry in one
``listjobs.json`` answer is not permission to run the batch again".

What the code needed that the table could not express
-----------------------------------------------------
``reconcile_inflight_intents`` used to move a ``POSTED`` intent to
``RECONCILED_MISSING`` — the ONE state that authorizes a re-POST, and
therefore a second Scrapyd execution and a second C3 cost reservation —
on the strength of one node answer that did not mention the job, at any
age. The protocol deliberately commits ``POSTED`` *before* the
``schedule.json`` POST is issued, so a sweep landing in that window asks
the node about a request that has not arrived yet and is told, quite
correctly, that the node does not have it.

The row carried nothing that could tell that apart from a real loss. It
had a ``state`` and two timestamps describing what *we* did, and nothing
describing what we had *observed*. These three columns are that missing
half:

* ``absent_observations`` — how many independent ``listjobs.json``
  answers have denied this run. Only answers taken after the in-flight
  lease are ever counted (``DispatchIntentStore.record_absence``
  discards earlier ones rather than recording them at a discount), so
  the counter means "denials that were about this request".
* ``first_absent_at`` — when the first of them was recorded, so the
  corroboration window is measured over real elapsed time rather than
  over however many times one pass happened to ask.
* ``last_seen_at`` — when a node last positively listed the run. Once
  set, a later absence is a *disappearance*, and a disappearance is
  never proof the work never happened: both deployed nodes run
  ``MemoryJobStorage`` with ``finished_to_keep = 100`` and lose the
  whole history on restart.

The new ``DispatchIntentState.RECONCILED_AMBIGUOUS`` member that reads
this ledger needs no DDL, for the same reason ``b6e5d1c94a72`` recorded
for ``RECONCILED_MISSING``: ``state`` is an app-validated ``VARCHAR(32)``
(``app_shared.enums.enum_column`` -> ``_AppValidatedEnumString``), never
a Postgres-native ``ENUM``. There is nothing to ``CREATE TYPE`` or
``ALTER TYPE``, and no CHECK constraint enumerates the members.

Why this is additive-only, and safe on a live table
---------------------------------------------------
All three are pure ``ADD COLUMN``. ``absent_observations`` is ``NOT
NULL`` with a constant ``server_default`` of ``0``, which Postgres 11+
records in the catalogue instead of rewriting the heap, so this takes an
``ACCESS EXCLUSIVE`` lock for a catalogue update and not for a scan of
2,000+ rows. The two timestamps are nullable with no default: NULL is
the honest value for every existing row ("nobody has recorded an
observation about this one"), and it is the value the reading code
already treats as "no evidence yet".

Note what is deliberately NOT done: no backfill. An existing ``POSTED``
row must start with an empty ledger, because we genuinely have not
observed anything about it — inventing observations to preserve the old
behaviour would re-create exactly the bug this migration exists to
support fixing. Those rows simply take one extra reconcile pass (300s)
to reach a verdict, which is the point.

The table carries ``FORCE ROW LEVEL SECURITY`` and the fail-closed
workspace policy, and the migration role is ``NOBYPASSRLS``; that is
irrelevant here only because nothing below is DML. (``b6e5d1c94a72``'s
docstring has the long version of why an ``UPDATE`` in an Alembic
revision against this table silently affects zero rows.)

Reversible: ``downgrade`` drops the three columns. Doing so loses every
recorded observation, so an intent mid-corroboration reverts to the
pre-R11 behaviour of the code that goes with it — a downgrade must only
be run on a deployment that has also reverted the code.

Ordering note (2026-09-09): this revision was authored against head
``f6b28c714a93`` and re-pointed at ``4b06b233e0b8``, the tenant-usage
view rebind that landed on the same branch in the same review pass, so
the branch keeps a SINGLE head. There is no data dependency between the
two — the re-point is purely to keep ``alembic upgrade head``
unambiguous — so if the other revision is dropped or renumbered before
merge, this one's ``down_revision`` should follow it back to whatever
the branch's real head then is.

Hand-authored (matches ``app_shared.models.dispatch`` exactly) — this
build environment has no live Postgres connection for autogenerate.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd8c14b7f6a92'
down_revision: Union[str, Sequence[str], None] = '4b06b233e0b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: the three absence-ledger columns."""
    op.add_column(
        "dispatch_intents",
        sa.Column(
            "absent_observations",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "dispatch_intents",
        sa.Column("first_absent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "dispatch_intents",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema: drop the absence ledger."""
    op.drop_column("dispatch_intents", "last_seen_at")
    op.drop_column("dispatch_intents", "first_absent_at")
    op.drop_column("dispatch_intents", "absent_observations")
