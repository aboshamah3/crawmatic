"""canonical competitor domains

Revision ID: b3d9e5a17c42
Revises: a7c41e9d2b56
Create Date: 2026-10-02 18:00:00.000000

Security E3/E4: ``competitors.domain`` is rewritten to the canonical form
(``app_shared.domains.canonical_domain``: IDNA, lowercase, no trailing dot,
one leading ``www.`` stripped) so ``Amazon.sa``, ``amazon.sa.`` and
``www.amazon.sa`` are one competitor and a match URL can be bound to it.

REFUSES (aborts, nothing changed) when two competitors of one workspace
would collapse onto the same canonical domain, or a stored domain cannot be
canonicalised: the message names the conflicting ids. There is NO automatic
merge; the owner decides merges/reassignments first. Run the read-only
``scripts/security/competitor_domain_dryrun.py`` beforehand.

``unique(workspace_id, domain)`` is untouched (it stays satisfied because
collisions are refused). Data-only; downgrade is a no-op because the
original spellings are not recoverable (and the canonical form is valid
under the old schema).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

from app_shared.domains import plan_competitor_domain_rewrite

revision: str = "b3d9e5a17c42"
down_revision: Union[str, Sequence[str], None] = "a7c41e9d2b56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if context.is_offline_mode():
        # Data-only: a `--sql` render has no rows to inspect. The online run
        # does the collision check and the rewrite.
        op.execute("-- canonical competitor domains: data-only, applied online")
        return
    bind = op.get_bind()
    rows = [
        (r[0], r[1], r[2])
        for r in bind.execute(sa.text("SELECT id, workspace_id, domain FROM competitors")).all()
    ]
    updates, collisions, invalid = plan_competitor_domain_rewrite(rows)
    problems: list[str] = []
    for workspace_id, canon, ids in collisions:
        problems.append(
            f"workspace {workspace_id}: competitors {sorted(str(i) for i in ids)} "
            f"all canonicalise to {canon!r}"
        )
    for row_id, domain in invalid:
        problems.append(f"competitor {row_id}: domain {domain!r} cannot be canonicalised")
    if problems:
        raise RuntimeError(
            "Refusing to canonicalise competitors.domain; resolve these first "
            "(merge/reassign/fix by hand, see scripts/security/competitor_domain_dryrun.py): "
            + "; ".join(problems)
        )
    for row_id, canon in updates:
        bind.execute(
            sa.text("UPDATE competitors SET domain = :d WHERE id = :i"),
            {"d": canon, "i": row_id},
        )


def downgrade() -> None:
    # Data-only canonicalisation; original spellings are not recoverable and
    # the canonical values are valid under the previous schema.
    pass
