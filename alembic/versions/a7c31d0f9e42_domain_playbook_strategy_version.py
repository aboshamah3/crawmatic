"""domain playbook strategy version

Revision ID: a7c31d0f9e42
Revises: f4a19c7d2b80
Create Date: 2026-09-08 00:00:00.000000

EPA C4 (F08, plan §11 items 2 and 5): the *versioned domain strategy*.
Adds five columns to ``domain_playbooks``:

``strategy_version INT NOT NULL DEFAULT 1``
    The version of THIS domain's escalation strategy — the cheap/fallback
    shape below, not the certification profile. Deliberately a SECOND
    counter alongside ``profile_version`` (W4.1) rather than a reuse of
    it: ``profile_version`` is bumped by every
    ``app_shared.domains.lifecycle.transition`` (an evidence/approval
    event about whether the domain may be scraped at all), while this one
    moves only when the strategy SHAPE changes. Stamping attempts with a
    counter that also moves on an unrelated approval would make "which
    strategy produced this attempt" unanswerable — which is precisely the
    question ``request_attempts``/``network_operations`` need to answer
    when a canary regresses.

``cheap_path TEXT`` / ``fallback_path TEXT``
    ``AccessMethod`` values (e.g. ``DIRECT_HTTP`` / ``PLAYWRIGHT_PROXY``)
    naming the two rungs that matter to cost: the one that should be
    tried first because it is nearly free, and the expensive one whose
    use has to be rationed. TEXT, not an enum column: these are curated
    operator data on a fleet-wide reference table, and a bad value must
    degrade to "no cheap/fallback hint" rather than fail an INSERT at
    seed time (the ladder validates them against ``AccessMethod``
    itself — see ``app_shared.strategy.methods.PlaybookStrategy``).

``fallback_cap_per_refresh INT``
    How many times ONE target may take ``fallback_path`` in ONE refresh.
    ``NULL`` = uncapped (today's behaviour, so this migration changes
    nothing until an operator seeds a value); ``0`` = never.

``recovery_probe_fraction NUMERIC``
    Per-domain override of ``SCRAPE_RECOVERY_PROBE_FRACTION`` — the share
    of targets that ignore a DOMAIN-scope method suppression and probe
    anyway (C1, ``scrape_core.attempt_budget``). ``NULL`` = use the
    setting. A domain that recovers slowly wants a smaller fraction than
    the fleet default; one under active repair wants a larger one.

SHAPE. All five are ``ALTER TABLE ... ADD COLUMN`` with either no
default or a CONSTANT default, which PostgreSQL 11+ records in the
catalog without rewriting the table (the same property
``f4a19c7d2b80`` relies on). ``domain_playbooks`` has no ``workspace_id``
and is filed SYSTEM in ``scripts/rls_table_manifest.txt``; these columns
add no tenant surface.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7c31d0f9e42'
down_revision: Union[str, Sequence[str], None] = 'f4a19c7d2b80'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add the versioned-strategy columns."""
    op.add_column(
        "domain_playbooks",
        sa.Column(
            "strategy_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column("domain_playbooks", sa.Column("cheap_path", sa.Text(), nullable=True))
    op.add_column(
        "domain_playbooks", sa.Column("fallback_path", sa.Text(), nullable=True)
    )
    op.add_column(
        "domain_playbooks",
        sa.Column("fallback_cap_per_refresh", sa.Integer(), nullable=True),
    )
    op.add_column(
        "domain_playbooks",
        sa.Column("recovery_probe_fraction", sa.Numeric(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema: drop the versioned-strategy columns."""
    op.drop_column("domain_playbooks", "recovery_probe_fraction")
    op.drop_column("domain_playbooks", "fallback_cap_per_refresh")
    op.drop_column("domain_playbooks", "fallback_path")
    op.drop_column("domain_playbooks", "cheap_path")
    op.drop_column("domain_playbooks", "strategy_version")
