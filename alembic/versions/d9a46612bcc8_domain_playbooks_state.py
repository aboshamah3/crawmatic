"""domain_playbooks_state

Revision ID: d9a46612bcc8
Revises: 03e34c8406cd
Create Date: 2026-08-26 00:51:47.816091

EPA C2 (READY-004/READY-006 critical path): the MINIMUM domain-state
model the authorization service (C3) must consult before allowing paid
or expensive work against a competitor domain. The full lifecycle
machine, approval workflow, and audit UI remain W4.1 scope -- this
migration adds only the one enforced-state column and seeds it from the
Phase B5/B5b certification outcomes.

``domain_playbooks.state`` (``app_shared.models.domain_playbooks.
DomainState`` -- ``UNKNOWN``, ``DIRECT_CANARY``, ``PROFILE_CANARY``,
``ACTIVE``, ``DEGRADED``, ``QUARANTINED``, ``UNSUPPORTED``): per the
``app_shared.enums`` module contract, enum-like values are plain
app-validated ``VARCHAR`` columns, never a Postgres-native ``ENUM`` --
``op.add_column`` below renders exactly that (mirrors ``0fc4c9c9c8b3``'s
``request_attempts.origin`` precedent). ``server_default='UNKNOWN'``
backfills every existing row in the same DDL statement, matching the
column's own ORM-level default -- no separate data migration for the
default itself.

``domain_playbooks`` is NOT partitioned (unlike ``price_observations``/
``request_attempts``), so this is a single, ordinary ``ALTER TABLE``. No
new relation is created, so ``scripts/rls_table_manifest.txt`` needs no
new entry (same reasoning as ``03e34c8406cd``).

Seed (data-only ``UPDATE``, this migration's second half): promote the
Phase B5/B5b certified domains from the default ``UNKNOWN`` to
``ACTIVE``, keyed by the bare ``domain`` column exactly as
``domain_playbooks``/``competitors.domain`` store it (no scheme, no
``www.`` -- confirmed 2026-08-26 against the restored A4 backup's actual
``domain_playbooks`` rows):

* ``amazon.sa`` -- Amazon, CSS-certified 5/5.
* ``noon.com`` -- Noon, proxy-certified 21/21.
* ``stech.ink`` -- S-Tech.

Every other existing domain (``afaqalhasoob.com``, ``ahbarhd.com``,
``alshamel.sa``, ``amwajest.com``, ``extra.com``, ``fqtoners.com``,
``jarir.com``, ``pcpalace.com.sa``, ``rawand.com.sa``,
``rowadalahbar.com``) is left at the ``server_default`` ``UNKNOWN`` --
none of them carry a Phase B5/B5b certification outcome. A future insert
for one of the three certified domains would still need its own seed
(this ``UPDATE`` only touches rows that already exist at migration
time); that is out of scope for this minimum slice, same as the rest of
the lifecycle machine.

Downgrade drops the column outright (the seeded state values are not
preserved separately -- there is nowhere else in this minimum slice to
put them, and W4.1's lifecycle machine will define real state storage
before this matters for a rollback).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd9a46612bcc8'
down_revision: Union[str, Sequence[str], None] = '03e34c8406cd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Bare domains promoted to ACTIVE by this migration's seed step (Phase
#: B5/B5b certification outcomes), in the exact form
#: ``domain_playbooks.domain`` stores them.
_ACTIVE_SEED_DOMAINS: tuple[str, ...] = ("amazon.sa", "noon.com", "stech.ink")


def upgrade() -> None:
    """Add ``domain_playbooks.state`` (NOT NULL, default 'UNKNOWN'), then
    seed the Phase B5/B5b certified domains to 'ACTIVE'."""
    op.add_column(
        "domain_playbooks",
        sa.Column(
            "state",
            sa.String(length=32),
            nullable=False,
            server_default="UNKNOWN",
        ),
    )
    domain_playbooks = sa.table(
        "domain_playbooks",
        sa.column("domain", sa.Text),
        sa.column("state", sa.String(length=32)),
    )
    op.execute(
        domain_playbooks.update()
        .where(domain_playbooks.c.domain.in_(_ACTIVE_SEED_DOMAINS))
        .values(state="ACTIVE")
    )


def downgrade() -> None:
    """Downgrade schema: drop domain_playbooks.state."""
    op.drop_column("domain_playbooks", "state")
