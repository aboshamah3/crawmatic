"""observation attempt identity: idempotent persistence keys (F05, plan task B1)

Revision ID: a4e91c7d2b58
Revises: b6f1c40a97d2
Create Date: 2026-09-07

EPA F05 (plan task B1). Gives the persistence flush an identity it can
insert *twice* without writing the row twice.

Why
---
Plan task B1 makes the scrape-result flush replayable: every result is
written to a durable local spool before it enters the in-memory buffer,
and its spool row is deleted only once the persistence transaction has
COMMITTED. A kill between COMMIT and delete therefore replays a batch
that is already in the database -- which is only safe if the inserts are
idempotent. They were not: ``price_observations`` and ``request_attempts``
had no key a replay could collide on, so a replayed batch would have
written a second observation for the same fetch, double-counting a
customer's price history and the usage derived from it.

A5 already added ``request_attempts.attempt_uuid`` -- the PRODUCER-side
identity minted by the spider before the fetch -- but deliberately left
it unconstrained ("this column's job is correlation, not arbitration").
B1 is where arbitration is needed, so this revision:

1. adds the same identity to ``price_observations`` (``attempt_uuid``,
   nullable), so an observation and the attempt that produced it carry
   one shared key;
2. makes both unique **per workspace, including the partition key** --
   ``UNIQUE (workspace_id, attempt_uuid, created_at)`` on
   ``request_attempts`` and ``UNIQUE (workspace_id, attempt_uuid,
   scraped_at)`` on ``price_observations``. Postgres requires a unique
   index on a partitioned table to include every partition-key column;
   ``workspace_id`` leads because every read in this system is
   workspace-scoped and the index is useful for that too.

``scrape_core.pipelines._flush_batch`` then inserts both tables with
``ON CONFLICT DO NOTHING`` on exactly these keys, so a replayed batch is
a no-op rather than a duplicate.

Why the new column is NULLABLE
------------------------------
``NULL`` means "this observation has no producer-side attempt identity"
-- a pre-B1 row, or a writer that is not the scrape pipeline. That is the
honest value, and a NULL never participates in a unique index, so such
rows neither collide with each other nor block anything. Making it NOT
NULL would force every future writer to invent an identity it does not
have, which is how a shared sentinel gets introduced and how "every row
looks like the same attempt" happens (the mistake A5's docstring already
argues against).

Backfill via the ADD COLUMN default, never an UPDATE
----------------------------------------------------
Existing rows are given their OWN identity by attaching
``DEFAULT gen_random_uuid()`` to the ``ADD COLUMN`` and dropping the
default immediately afterwards -- exactly the mechanism A5 used. A
*volatile* default is not stored as metadata: PostgreSQL materialises a
distinct value per row, rewriting the table. That is the point here (a
shared sentinel would be worse than no identity), and it also avoids the
alternative, a bulk ``UPDATE``, which would not work at all: both tables
carry ``FORCE ROW LEVEL SECURITY``, so a migration-time UPDATE with no
``app.workspace_id`` set matches zero rows and would silently backfill
nothing. Dropping the default afterwards is what keeps "NULL means no
attempt identity" true for rows written later by anything other than the
pipeline.

**Operational note -- this rewrites ``price_observations``.** Same
trade-off A5 documented for ``request_attempts``: every existing monthly
partition is rewritten under an ``ACCESS EXCLUSIVE`` lock, bounded by the
retention window, run by the one-shot migration job
(``contracts/migration-job.md``) with no concurrent writer. Adding the
two unique indexes takes the same lock (plain ``CREATE UNIQUE INDEX``,
not ``CONCURRENTLY`` -- Alembic runs inside a transaction and a
partitioned parent's index cannot be built concurrently anyway). The
staged equivalent for a table too large for that lock is: add the column
NULL with no default, backfill per partition in batches, then create the
index -- same end state, no long lock.

``ALTER TABLE ... ADD COLUMN`` / ``ADD CONSTRAINT`` on a partitioned
**parent** propagates to every existing partition automatically (the
``0fc4c9c9c8b3`` / ``d5e8a3c164f2`` / ``b6f1c40a97d2`` precedent) -- no
per-partition ``op.execute``.

RLS: unaffected. A column addition and two unique indexes on already-
RLS'd tables need no new policy.

Reversible: ``downgrade`` drops both constraints and the new column. It
loses the idempotency keys, never a row.

Hand-authored (matches ``app_shared.models.observations`` exactly) --
this build environment has no live Postgres connection for autogenerate.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a4e91c7d2b58'
down_revision: Union[str, Sequence[str], None] = 'b6f1c40a97d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: `(table, constraint_name, columns)` for the two idempotency keys. The
#: partition key is the LAST column of each: Postgres requires it to be
#: present, and putting it last keeps the leading `(workspace_id,
#: attempt_uuid)` prefix usable on its own.
_IDENTITY_UNIQUES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "request_attempts",
        "uq_request_attempts_workspace_id_attempt_uuid_created_at",
        ("workspace_id", "attempt_uuid", "created_at"),
    ),
    (
        "price_observations",
        "uq_price_observations_workspace_id_attempt_uuid_scraped_at",
        ("workspace_id", "attempt_uuid", "scraped_at"),
    ),
)


def upgrade() -> None:
    """Upgrade schema: observation attempt identity + idempotency keys."""
    # Nullable, but with a volatile default *during the rewrite* so every
    # pre-existing row gets its own identity (see the module docstring on
    # why an UPDATE would silently do nothing under FORCE RLS).
    op.add_column(
        "price_observations",
        sa.Column(
            "attempt_uuid",
            sa.Uuid(as_uuid=True),
            nullable=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    op.alter_column("price_observations", "attempt_uuid", server_default=None)

    for table, name, columns in _IDENTITY_UNIQUES:
        op.create_unique_constraint(name, table, list(columns))


def downgrade() -> None:
    """Downgrade schema: drop the idempotency keys and the new column."""
    for table, name, _columns in reversed(_IDENTITY_UNIQUES):
        op.drop_constraint(name, table, type_="unique")

    op.drop_column("price_observations", "attempt_uuid")
