"""rebind workspace_usage_v after the network_operations partition swap (review R13)

Revision ID: 4b06b233e0b8
Revises: f6b28c714a93
Create Date: 2026-09-09 22:31:44.248716

THE DEFECT
----------
``193ac27f0dc2`` creates the tenant-scoped view ``workspace_usage_v``
over ``network_operations``. ``a5e0c74b13d9`` later partitions that table
by build-copy-swap: it renames the original relation to
``network_operations_pre_partition`` and gives the new partitioned table
the old NAME.

Postgres binds a view to the relation's **OID**, resolved once at
``CREATE VIEW`` time — never to the name. A rename therefore does not
follow: after the swap, ``workspace_usage_v`` is still reading the legacy
copy. Two consequences, and the reason this was worth a migration of its
own is that NEITHER of them raises anything:

* every merchant's usage read answers from a table that stopped receiving
  rows the instant the swap committed — stale, not broken, so nothing
  alerts and the numbers merely drift;
* the legacy copy can never be reclaimed. ``DROP TABLE
  network_operations_pre_partition`` errors on the dependent view, and
  that copy is a full duplicate of the largest table in the system — the
  disk the partitioning exists to reclaim in the first place.

THE FIX
-------
Recreate the view. Its definition names ``network_operations``, so a
fresh ``CREATE VIEW`` binds to whichever relation holds that name now,
which after the swap is the partitioned one. Then re-issue the grant: a
recreated view is a NEW object and carries none of the old one's
privileges, so skipping the grant would take ``crawmatic_app``'s usage
read down as surely as the stale binding did.

``CREATE OR REPLACE VIEW`` is NOT usable here. It preserves the existing
view's dependencies rather than re-resolving them, which is precisely the
thing that has to change. Hence drop-then-create.

WHY A NEW REVISION AND NOT AN EDIT
----------------------------------
The obvious-looking fix is to append the recreation to
``a5e0c74b13d9.upgrade()``, right after its swap. Both that revision and
``193ac27f0dc2`` have already been applied to databases, and an applied
revision is history: editing one means two environments reporting the
same ``alembic_version`` no longer have the same schema, and nothing
in the system would ever tell you. So the correction runs FORWARD, from
here, and it is idempotent-by-construction (drop-if-exists, then create)
so that an environment which somehow already has a correctly bound view
lands in the same state as one that does not.

Chained after ``f6b28c714a93`` (the current head) rather than immediately
after the swap, for the same reason: inserting a revision into the middle
of a chain rewrites history that has already run.

SAFETY
------
Cheap and non-blocking in the sense that matters: no table is read,
copied or rewritten. The window between the ``DROP VIEW`` and the
``CREATE VIEW`` is inside one transaction (alembic runs each migration in
one), so a concurrent reader either sees the old view or waits — never a
missing one.

WHAT THIS REVISION DELIBERATELY DOES NOT DO
-------------------------------------------
It does not drop ``network_operations_pre_partition``. It only makes that
drop *possible*, which is a separate, irreversible operator decision (the
legacy copy is also the only thing that makes ``a5e0c74b13d9``'s
downgrade work — see its ``DOWNGRADE_SQL``, which refuses without it).
Reclaiming that disk belongs to the deploy runbook, not to a migration
that runs automatically at container start.
"""
from typing import Sequence, Union

from alembic import op

from app_shared.models.workspace_usage_view import (
    REBIND_WORKSPACE_USAGE_VIEW_TO_LEGACY_SQL,
    RECREATE_WORKSPACE_USAGE_VIEW_SQL,
)


# revision identifiers, used by Alembic.
revision: str = '4b06b233e0b8'
down_revision: Union[str, Sequence[str], None] = 'f6b28c714a93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rebind `workspace_usage_v` onto the post-swap `network_operations`."""
    # Drop, create, grant -- in that order, from
    # `app_shared.models.workspace_usage_view` rather than re-typed here, so
    # the next change to the view's shape cannot leave this migration
    # recreating an older one (`PARTITION_RLS_INHERITANCE_SQL` is the same
    # pattern).
    for statement in RECREATE_WORKSPACE_USAGE_VIEW_SQL:
        op.execute(statement)


def downgrade() -> None:
    """Put the view back where `a5e0c74b13d9` left it: on the legacy copy.

    A downgrade has to reproduce the schema the revision below this one
    produced, and that schema is the buggy one -- the view bound to
    `network_operations_pre_partition`. Restoring it faithfully is not
    pedantry here: `a5e0c74b13d9.downgrade()` does `DROP TABLE
    network_operations`, which FAILS while this view depends on that
    table. Leaving the view bound to the live table would turn a
    reversible migration into a one-way door.

    Guarded on the legacy table still existing -- see
    `REBIND_WORKSPACE_USAGE_VIEW_TO_LEGACY_SQL` for what happens when an
    operator has already reclaimed it (nothing, loudly).
    """
    op.execute(REBIND_WORKSPACE_USAGE_VIEW_TO_LEGACY_SQL)
