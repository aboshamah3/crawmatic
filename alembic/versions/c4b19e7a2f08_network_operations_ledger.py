"""network_operations physical ledger (operations / allocations / settlements)

Revision ID: c4b19e7a2f08
Revises: b6d94c2f1a70
Create Date: 2026-08-26 00:00:00.000000

EPA C1 (2026-08-25, READY-005, fixes P0.7): the physical half of network
accounting.

``request_attempts`` records a **logical** attempt — workspace-owned, one
row per attempted target. It is the wrong row to cost off: one browser
navigation can serve two workspaces watching the same competitor URL, a
retry is a second physical fetch of the same logical attempt, and a
browser subresource is a physical fetch with no logical attempt at all.
This migration adds the row that stands for one physical thing that
happened on the wire, as THREE separate append-only facts rather than one
mutable row (see ``app_shared.models.network_operations``):

1. ``network_operations`` — immutable intent written at open, immutable
   outcome written exactly once at close. **Fleet-owned: no
   ``workspace_id`` column**, because a physical fetch is not owned by a
   tenant; tenant ownership is a derived allocation of it. A ``BEFORE
   UPDATE`` trigger rejects every UPDATE of a row whose ``closed_at`` is
   already set, and every UPDATE that touches an intent column at all —
   so the only legal UPDATE in the table's life is the single close.
2. ``network_operation_allocations`` — the tenant-visible split, and the
   ONLY table here that gets row-level security (the standard
   ``emit_rls_policy``: ENABLE + FORCE + ``workspace_id = <ctx>``).
   Fractions are exact scaled integers (parts per billion); costs are
   integer minor units; a **DEFERRABLE INITIALLY DEFERRED** constraint
   trigger re-checks at COMMIT that the rows for one operation sum
   exactly to the operation's cost and their fractions to 1.0. Deferred,
   not immediate, because an allocation set is written a row at a time
   and is only meaningful complete.
3. ``network_operation_settlements`` — append-only provider
   reconciliation facts for C5. A correction APPENDS a higher
   ``settlement_version``; UPDATE and DELETE are both rejected by a
   trigger. "Current reconciled cost" is the latest version.

Money is **integer minor units + an ISO-4217 currency code** throughout —
the ``app_shared.money`` §19 "money is never a float" contract expressed
as scaled integers, so there is no Decimal-vs-float ambiguity at any
boundary. The one rate (``billing_rate_micro_units``) carries a further
1e6 scale so a sub-cent per-request proxy price stays exact instead of
rounding to zero.

``request_attempts.network_operation_id`` — a nullable FK to
``network_operations.network_request_id`` (the pre-dispatch identity, the
declared unique key). ``request_attempts`` is monthly-RANGE-partitioned;
``ALTER TABLE ... ADD COLUMN`` on the partitioned parent propagates to
every partition automatically (the ``d5e8a3c164f2``/``0fc4c9c9c8b3``
precedent), and Postgres supports a foreign key FROM a partitioned table
since 12. The constraint validates against an all-NULL new column, so it
scans but rejects nothing; it does take ACCESS EXCLUSIVE on the parent
and its partitions for the duration, which is why this migration belongs
in the maintenance window and not in a rolling deploy. **Nullable on
purpose**, and nullable is not a coverage claim: every pre-C1 attempt row
legitimately has no operation, and "every new attempt HAS one" is an
invariant C3/C4 must establish at the write sites.

RLS on ``network_operations`` and ``network_operation_settlements``: NONE,
deliberately. Neither has a ``workspace_id`` to scope by, and neither is
the transitive/``EXISTS``-via-parent shape either (``scrape_job_id`` is
nullable by design — a fleet-side probe has no job). Both are recorded as
annotated ``GAP`` entries in ``scripts/rls_table_manifest.txt`` so
``provision_db_roles.py --verify`` reads the decision out loud on every
run instead of letting it go quiet.

The allocation-total trigger runs as the INVOKER and must aggregate every
workspace's share of one operation, so an allocation set MUST be written
on the sanctioned BYPASSRLS system seam (``crawmatic_auth`` /
``get_system_session``). Under FORCE RLS a tenant connection sees only its
own row and would compute a spurious shortfall — the trigger failing
closed on a tenant attempting a cross-tenant cost split is the correct
outcome, not a defect.

**No DDL for the two new enums.** ``NetworkTransport`` and
``SettlementMethod`` are app-validated ``VARCHAR(32)`` columns
(``app_shared.enums.enum_column`` -> ``_AppValidatedEnumString``), never
Postgres-native ``ENUM``s — the convention every status column in this
repo follows (see ``a6b0234cd4ad`` and ``e7b21f3a8c94``). There is nothing
to ``CREATE TYPE``.

Reversible: ``downgrade`` drops the three triggers and their functions,
the ``request_attempts`` column and its FK, then the three tables in
dependency order (the allocations RLS policy goes with its table).
Downgrading discards the physical ledger, so it must only ever run on a
deployment that has also reverted the code.

Hand-authored (matches ``app_shared.models.network_operations`` and the
``RequestAttempt`` column exactly) — this build environment has no live
Postgres connection for autogenerate.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app_shared.models import emit_rls_policy
from app_shared.models.network_operations import (
    NETWORK_OPERATION_ALLOCATION_TOTAL_SQL,
    NETWORK_OPERATION_IMMUTABILITY_SQL,
    NETWORK_OPERATION_LEDGER_DROP_SQL,
    NETWORK_OPERATION_SETTLEMENTS_APPEND_ONLY_SQL,
)

# revision identifiers, used by Alembic.
revision: str = 'c4b19e7a2f08'
down_revision: Union[str, Sequence[str], None] = 'b6d94c2f1a70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: the three-table physical ledger + attempt FK."""
    # --- 1. network_operations (fleet-owned, no workspace_id) ------------
    op.create_table(
        "network_operations",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        # Immutable intent, written at open.
        sa.Column("network_request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("retry_parent_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("parent_operation_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("scrape_job_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("canonical_url_hash", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column(
            "http_method", sa.Text(), nullable=False, server_default=sa.text("'GET'")
        ),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_account", sa.Text(), nullable=True),
        sa.Column("transport", sa.String(length=32), nullable=False),
        sa.Column("authorization_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("entitlement_version", sa.Text(), nullable=True),
        sa.Column("budget_decision_version", sa.Text(), nullable=True),
        sa.Column("breaker_decision", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Immutable outcome, written exactly once at close.
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bytes_compressed", sa.BigInteger(), nullable=True),
        sa.Column("bytes_decompressed", sa.BigInteger(), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("extraction_result", sa.Text(), nullable=True),
        sa.Column("identity_confidence", sa.Text(), nullable=True),
        sa.Column("comparability", sa.Text(), nullable=True),
        sa.Column("estimated_cost_minor_units", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("billing_unit", sa.Text(), nullable=True),
        sa.Column("billing_rate_micro_units", sa.BigInteger(), nullable=True),
        sa.Column("rate_effective_date", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_network_operations"),
        sa.UniqueConstraint(
            "network_request_id", name="uq_network_operations_network_request_id"
        ),
        sa.ForeignKeyConstraint(
            ["retry_parent_id"],
            ["network_operations.network_request_id"],
            name="fk_no_retry_parent_id_network_operations",
        ),
        sa.ForeignKeyConstraint(
            ["parent_operation_id"],
            ["network_operations.network_request_id"],
            name="fk_no_parent_operation_id_network_operations",
        ),
        sa.CheckConstraint(
            "estimated_cost_minor_units IS NULL OR estimated_cost_minor_units >= 0",
            name="ck_network_operations_no_estimated_cost_non_negative",
        ),
        sa.CheckConstraint(
            "billing_rate_micro_units IS NULL OR billing_rate_micro_units >= 0",
            name="ck_network_operations_no_billing_rate_non_negative",
        ),
        sa.CheckConstraint(
            "estimated_cost_minor_units IS NULL OR currency IS NOT NULL",
            name="ck_network_operations_no_cost_requires_currency",
        ),
        sa.CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name="ck_network_operations_no_currency_is_iso4217",
        ),
    )
    op.create_index(
        "ix_network_operations_provider_created_at",
        "network_operations",
        ["provider", "created_at"],
    )
    op.create_index(
        "ix_network_operations_scrape_job_id", "network_operations", ["scrape_job_id"]
    )
    op.create_index(
        "ix_network_operations_canonical_url_hash_created_at",
        "network_operations",
        ["canonical_url_hash", "created_at"],
    )

    # --- 2. network_operation_allocations (tenant-visible, RLS'd) --------
    op.create_table(
        "network_operation_allocations",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("operation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("fraction_ppb", sa.BigInteger(), nullable=False),
        sa.Column("allocated_cost_minor_units", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_network_operation_allocations"),
        sa.UniqueConstraint(
            "operation_id", "workspace_id", name="uq_noa_operation_id_workspace_id"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["network_operations.network_request_id"],
            name="fk_noa_operation_id_network_operations",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_noa_workspace_id_workspaces",
        ),
        sa.CheckConstraint(
            "fraction_ppb >= 0 AND fraction_ppb <= 1000000000",
            name="ck_network_operation_allocations_noa_fraction_in_range",
        ),
        sa.CheckConstraint(
            "allocated_cost_minor_units >= 0",
            name="ck_network_operation_allocations_noa_cost_non_negative",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_network_operation_allocations_noa_currency_is_iso4217",
        ),
    )
    op.create_index(
        "ix_network_operation_allocations_workspace_id",
        "network_operation_allocations",
        ["workspace_id"],
    )
    op.create_index(
        "ix_noa_operation_id", "network_operation_allocations", ["operation_id"]
    )

    # --- 3. network_operation_settlements (append-only, fleet-owned) -----
    op.create_table(
        "network_operation_settlements",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("operation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("settlement_version", sa.Integer(), nullable=False),
        sa.Column("reconciled_cost_minor_units", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provider_usage_record_id", sa.Text(), nullable=True),
        sa.Column("method", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_network_operation_settlements"),
        sa.UniqueConstraint(
            "operation_id",
            "settlement_version",
            name="uq_nos_operation_id_settlement_version",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["network_operations.network_request_id"],
            name="fk_nos_operation_id_network_operations",
        ),
        sa.CheckConstraint(
            "settlement_version >= 1",
            name="ck_network_operation_settlements_nos_version_is_positive",
        ),
        sa.CheckConstraint(
            "reconciled_cost_minor_units >= 0",
            name="ck_network_operation_settlements_nos_cost_non_negative",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_network_operation_settlements_nos_currency_is_iso4217",
        ),
    )
    op.create_index(
        "ix_nos_operation_id", "network_operation_settlements", ["operation_id"]
    )

    # --- 4. RLS on the allocations table ONLY (§32, Principle II) --------
    # The other two tables are fleet facts with no workspace_id to scope
    # by; both are declared as annotated GAP entries in
    # scripts/rls_table_manifest.txt so --verify reports them every run.
    for statement in emit_rls_policy("network_operation_allocations"):
        op.execute(statement)

    # --- 5. The enforcement triggers -------------------------------------
    op.execute(NETWORK_OPERATION_IMMUTABILITY_SQL)
    op.execute(NETWORK_OPERATION_SETTLEMENTS_APPEND_ONLY_SQL)
    op.execute(NETWORK_OPERATION_ALLOCATION_TOTAL_SQL)

    # --- 6. request_attempts.network_operation_id ------------------------
    # ADD COLUMN on the partitioned parent propagates to every partition
    # (d5e8a3c164f2 precedent). The FK is added separately so the column
    # and the constraint are individually reversible.
    op.add_column(
        "request_attempts",
        sa.Column("network_operation_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_request_attempts_network_operation_id_network_operations",
        "request_attempts",
        "network_operations",
        ["network_operation_id"],
        ["network_request_id"],
    )


def downgrade() -> None:
    """Downgrade schema: drop the attempt FK, the triggers, then the tables."""
    op.drop_constraint(
        "fk_request_attempts_network_operation_id_network_operations",
        "request_attempts",
        type_="foreignkey",
    )
    op.drop_column("request_attempts", "network_operation_id")

    # Triggers and their functions first: a function is not dropped by
    # DROP TABLE, and leaving orphans behind would make a re-upgrade's
    # CREATE OR REPLACE silently inherit a stale body.
    op.execute(NETWORK_OPERATION_LEDGER_DROP_SQL)

    op.drop_index("ix_nos_operation_id", table_name="network_operation_settlements")
    op.drop_table("network_operation_settlements")

    op.drop_index("ix_noa_operation_id", table_name="network_operation_allocations")
    op.drop_index(
        "ix_network_operation_allocations_workspace_id",
        table_name="network_operation_allocations",
    )
    # The RLS policy is dropped with its table.
    op.drop_table("network_operation_allocations")

    op.drop_index(
        "ix_network_operations_canonical_url_hash_created_at",
        table_name="network_operations",
    )
    op.drop_index("ix_network_operations_scrape_job_id", table_name="network_operations")
    op.drop_index(
        "ix_network_operations_provider_created_at", table_name="network_operations"
    )
    op.drop_table("network_operations")
