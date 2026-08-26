"""domain lifecycle transitions and audit

Revision ID: 9f24d748ba13
Revises: f1a7c02de5b4
Create Date: 2026-08-26 02:03:15.332742

EPA W4.1 (report §6, builds on C2's minimum): completes the domain
certification lifecycle C2 left as the minimum authorization-facing
slice (state enum + deny rules, migration ``d9a46612bcc8``). This
migration is purely additive over that one:

1. Four new ``domain_playbooks`` columns carrying the "versioned
   certification profile" fields that have no existing home (see
   ``app_shared.models.domain_playbooks.DomainPlaybook``'s inline
   docstring for the full mapping of which required fields already
   live on ``method_templates``/``scrape_profile_name``/
   ``access_policy_name`` and are therefore NOT duplicated here):
   ``profile_version`` (int, default 1), ``profile_owner`` (nullable
   text), ``last_canary_at`` (nullable timestamptz), ``profile_fields``
   (JSONB, default ``{}``). All four are backward-compatible additions
   with defaults — no backfill needed, every existing row (including
   C2's three seeded ``ACTIVE`` rows) gets a valid default.

2. ``domain_lifecycle_audit`` — a new, append-only table (enforced by
   a ``BEFORE UPDATE OR DELETE`` trigger, same pattern as
   ``network_operation_settlements`` in migration ``c4b19e7a2f08``)
   recording every ``app_shared.domains.lifecycle.transition`` call:
   the state edge, evidence, approver, and the profile version it
   certifies. FK'd to ``domain_playbooks.domain`` (the existing unique
   index). No RLS: fleet-wide like its parent (see
   ``scripts/rls_table_manifest.txt``'s new ``SYSTEM`` entry).

Reversible: ``downgrade`` drops the trigger + function, the table, then
the three ``domain_playbooks`` columns, in dependency order.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app_shared.models.domain_playbooks import DOMAIN_LIFECYCLE_AUDIT_APPEND_ONLY_SQL

# revision identifiers, used by Alembic.
revision: str = '9f24d748ba13'
down_revision: Union[str, Sequence[str], None] = 'f1a7c02de5b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: domain_playbooks profile columns + domain_lifecycle_audit."""
    # --- 1. domain_playbooks: the versioned-profile columns --------------
    op.add_column(
        "domain_playbooks",
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "domain_playbooks",
        sa.Column("profile_owner", sa.Text(), nullable=True),
    )
    op.add_column(
        "domain_playbooks",
        sa.Column("last_canary_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "domain_playbooks",
        sa.Column(
            "profile_fields",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
    )

    # --- 2. domain_lifecycle_audit ----------------------------------------
    op.create_table(
        "domain_lifecycle_audit",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=False),
        sa.Column("to_state", sa.String(length=32), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("approver", sa.Text(), nullable=True),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_domain_lifecycle_audit"),
        sa.ForeignKeyConstraint(
            ["domain"],
            ["domain_playbooks.domain"],
            name="fk_domain_lifecycle_audit_domain_domain_playbooks",
        ),
    )
    op.create_index(
        "ix_domain_lifecycle_audit_domain", "domain_lifecycle_audit", ["domain"]
    )

    # --- 3. Append-only enforcement trigger --------------------------------
    op.execute(DOMAIN_LIFECYCLE_AUDIT_APPEND_ONLY_SQL)


def downgrade() -> None:
    """Downgrade schema: reverse of upgrade, in dependency order."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_domain_lifecycle_audit_append_only "
        "ON domain_lifecycle_audit"
    )
    op.execute("DROP FUNCTION IF EXISTS domain_lifecycle_audit_reject_mutation()")
    op.drop_index("ix_domain_lifecycle_audit_domain", table_name="domain_lifecycle_audit")
    op.drop_table("domain_lifecycle_audit")
    op.drop_column("domain_playbooks", "profile_fields")
    op.drop_column("domain_playbooks", "last_canary_at")
    op.drop_column("domain_playbooks", "profile_owner")
    op.drop_column("domain_playbooks", "profile_version")
