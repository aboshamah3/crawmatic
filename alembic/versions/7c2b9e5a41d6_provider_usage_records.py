"""provider_usage_records — raw provider usage evidence (EPA C5)

Revision ID: 7c2b9e5a41d6
Revises: 05dfda7cdbb7
Create Date: 2026-08-26 01:00:00.000000

EPA C5 (2026-08-26, READY-005 part 2): the provider side of network-cost
reconciliation. C1 built the physical ledger (``network_operations`` /
``network_operation_allocations`` / ``network_operation_settlements``);
C4 populates it from the fleet's own transport observations. Neither
knows what the *provider* (DataImpulse today) says it billed. This
migration adds the durable, immutable landing zone for a provider's own
usage export — ``provider_usage_records`` — imported by
``app_shared.netledger.reconcile.import_provider_usage`` /
``scripts/import_dataimpulse_usage.py`` and read back by
``app_shared.netledger.reconcile.reconcile_window``, which apportions
reconciled cost across ``network_operations`` as APPENDED
``network_operation_settlements`` versions (never a mutation).

Shape
-----
One row per provider-usage line, verbatim (``raw_row`` JSONB keeps the
entire original row, untouched by column mapping). ``window_id`` groups
every row of one import; ``source_hash`` (a ``sha256:`` digest over the
exact bytes of the source export) + ``row_ordinal`` are UNIQUE together,
which is what makes re-importing the SAME file a no-op while a
genuinely different file (a correction, a wider pull) always lands as
new rows under its own hash — the provider's window/invoice is a fact,
never merged away. See ``app_shared.models.provider_usage`` for the full
rationale.

Append-only, enforced by trigger (``PROVIDER_USAGE_RECORDS_APPEND_ONLY_SQL``):
UPDATE and DELETE are both rejected, the identical pattern
``network_operation_settlements`` uses.

RLS: NONE, filed **GAP** (not SYSTEM) in ``scripts/rls_table_manifest.txt``.
The table is fleet-owned (no ``workspace_id`` at all — a provider bills a
provider account, not a workspace) but carries ``target_host``, a
domain-shaped column, which is exactly the kind of tenant-linked
evidence that got ``network_operations`` filed GAP rather than SYSTEM in
that same manifest (contrast ``fleet_cost_budgets``/``domain_playbooks``,
which carry NO tenant-identifying column at all and are correctly
SYSTEM). See the manifest entry for the full justification this mirrors.

``network_operation_settlements.provider_usage_record_id`` — DELIBERATELY
NO FOREIGN KEY added here. C1's model docstring for that column
(``app_shared/models/network_operations.py``, ``NetworkOperationSettlement``)
describes it only as "the provider's own identifier for the usage record
this settlement was derived from — the thread back to the invoice"; it
gives no indication the FK was deferred to C5, so this migration reads
C1's silence as "no owed constraint", not as an instruction to add one,
per the run's stated ambiguity rule (add the FK only if C1's docs say it
was deferred here). Three further reasons make a hard FK actively wrong
for this column, beyond that reading:

1. The column is sized and documented for the PROVIDER's own native
   identifier (a per-request log line's id, when the provider's export
   supplies one) — a different identifier space than this migration's
   own ``provider_usage_records.id`` surrogate key, so a single FK
   cannot honestly claim to cover both.
2. A ``PRO_RATA_BYTES`` settlement (the common case — a provider bills a
   PERIOD, not a request) is apportioned across a whole window of
   provider rows, not derived from any single row; ``reconcile_window``
   therefore writes ``"window:<window_id>"`` there in the apportioned
   case, which a single-row FK cannot represent at all.
3. Leaving it a loose, undocumented-in-schema reference is the
   REVERSIBLE choice — a FK can always be added in a later migration
   once real provider data proves a tighter, single-row-shaped
   correlation is the common case; removing a wrongly-added FK is the
   more disruptive direction.

This decision is documented here, in ``app_shared.models.provider_usage``,
and in ``app_shared.netledger.reconcile`` (at the exact call site that
populates the column), per the run's requirement to record the choice
in more than one place a future reader might look.

Hand-authored (matches ``app_shared.models.provider_usage`` exactly) —
this build environment has no live Postgres connection for autogenerate,
per the repo's established migration-authoring convention (see
``c4b19e7a2f08``).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app_shared.models.provider_usage import (
    PROVIDER_USAGE_RECORDS_APPEND_ONLY_SQL,
    PROVIDER_USAGE_RECORDS_DROP_SQL,
)

# revision identifiers, used by Alembic.
revision: str = '7c2b9e5a41d6'
down_revision: Union[str, Sequence[str], None] = '05dfda7cdbb7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: provider_usage_records + its append-only trigger."""
    op.create_table(
        "provider_usage_records",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("window_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("row_ordinal", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_account", sa.Text(), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granularity", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("target_host", sa.Text(), nullable=True),
        sa.Column("bytes_up", sa.BigInteger(), nullable=True),
        sa.Column("bytes_down", sa.BigInteger(), nullable=True),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "request_count", sa.BigInteger(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("sub_user", sa.Text(), nullable=True),
        sa.Column("pool_identifier", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("billing_line_item", sa.Text(), nullable=True),
        sa.Column("raw_row", postgresql.JSONB(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provider_usage_records"),
        sa.UniqueConstraint(
            "source_hash", "row_ordinal", name="uq_pur_source_hash_row_ordinal"
        ),
        sa.CheckConstraint(
            "bytes_up IS NULL OR bytes_up >= 0",
            name="ck_provider_usage_records_pur_bytes_up_non_negative",
        ),
        sa.CheckConstraint(
            "bytes_down IS NULL OR bytes_down >= 0",
            name="ck_provider_usage_records_pur_bytes_down_non_negative",
        ),
        sa.CheckConstraint(
            "total_bytes >= 0",
            name="ck_provider_usage_records_pur_total_bytes_non_negative",
        ),
        sa.CheckConstraint(
            "request_count >= 0",
            name="ck_provider_usage_records_pur_request_count_non_negative",
        ),
    )
    op.create_index(
        "ix_pur_provider_account_window_start",
        "provider_usage_records",
        ["provider", "provider_account", "window_start"],
    )
    op.create_index(
        "ix_pur_window_id", "provider_usage_records", ["window_id"]
    )
    op.create_index(
        "ix_pur_occurred_at", "provider_usage_records", ["occurred_at"]
    )

    # No RLS: fleet-owned, filed GAP in scripts/rls_table_manifest.txt —
    # see this migration's module docstring for the full justification.
    op.execute(PROVIDER_USAGE_RECORDS_APPEND_ONLY_SQL)


def downgrade() -> None:
    """Downgrade schema: drop the trigger/function, then the table."""
    op.execute(PROVIDER_USAGE_RECORDS_DROP_SQL)
    op.drop_index("ix_pur_occurred_at", table_name="provider_usage_records")
    op.drop_index("ix_pur_window_id", table_name="provider_usage_records")
    op.drop_index(
        "ix_pur_provider_account_window_start", table_name="provider_usage_records"
    )
    op.drop_table("provider_usage_records")
