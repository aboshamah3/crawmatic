"""cost authorization store (reservations, budgets, entitlement evidence)

Revision ID: f1a7c02de5b4
Revises: d9a46612bcc8
Create Date: 2026-08-26 00:00:00.000000

EPA C3 (2026-08-25, READY-006): the schema behind
``app_shared.costauth.service.CostAuthorizationService`` — the single gate
every paid dispatch passes through.

FOUR tables, not one
--------------------
The task's file list names ``cost_reservations``. The task's *interface*
section names more than that in the same breath — "budget counters and
reservations live in ``cost_reservations`` + budget rows updated with
``SELECT ... FOR UPDATE``" — and the ``authorize`` contract additionally
requires an entitlement denial "from durable local evidence ... even when
SaaS/engine sync is down". Neither is expressible with a reservations
table alone, so this revision creates the reservation table and the three
rows-of-record it must lock and read:

1. ``cost_reservations`` — one row per authorization grant.
   Workspace-owned, RLS ENABLE + FORCE + the standard
   ``workspace_id = <ctx>`` policy (``emit_rls_policy``). ``state`` moves
   exactly once, ``RESERVED -> SETTLED | RELEASED``, by compare-and-set.
   ``lease_expires_at`` is a **lease**: an expired one is only reaped
   after the sweeper confirms C1's ledger holds no OPEN operation for the
   grant.
2. ``cost_budgets`` — the TENANT counters, one row per
   ``(workspace_id, period_key)``. Workspace-owned, same RLS. Four
   dimensions (money in integer minor units, bytes, requests,
   browser-seconds), each with a nullable limit, a ``reserved_*`` and a
   ``settled_*`` counter.
3. ``fleet_cost_budgets`` — the same counters for a FLEET provider
   budget, keyed by ``scope_key``. **No ``workspace_id``, no RLS**, by
   design and by precedent: this is the ``proxy_circuit_breakers`` shape.
   The money is spent against one shared operator account, so one
   tenant's runaway loop spends everyone's budget and the ceiling must be
   readable and decrementable from every workspace's scrape path. Filed
   ``SYSTEM`` in ``scripts/rls_table_manifest.txt`` — it names a provider
   and a month and carries no tenant-identifying column, which is
   precisely what ``SYSTEM`` asserts (contrast C1's two ``GAP`` entries,
   which DO carry tenant-linked columns and are filed GAP so ``--verify``
   says so on every run).
4. ``workspace_entitlements`` — the engine's durable local entitlement
   evidence. Workspace-owned, same RLS.

Why the engine grows an entitlement table here
-----------------------------------------------
A survey at C3 found **no engine-side entitlement store**: ``grep -ril
entitlement`` over ``libs``/``apps``/``scripts`` matched only C1's opaque
``network_operations.entitlement_version`` tag (which records *which*
evidence a dispatch used and cannot answer what the evidence IS).
``workspaces.status`` is the nearest existing column and is a different
fact — ``active``/``suspended`` operational lifecycle, no billing plan,
and critically **no observation timestamp**, so it cannot express the
staleness W1.1's contract requires ("entitlement state replicated to the
engine on every billing event, with staleness treated as inactive").

``workspace_entitlements`` is therefore the minimal durable evidence the
denial path needs and nothing more: ``state``, ``observed_at`` (the
freshness clock — deliberately not ``updated_at``, which any local touch
would move), ``evidence_version``, ``plan_code``. It is shaped as
evidence-with-a-timestamp rather than as a mirror of any SaaS model, so a
schema change on the SaaS side cannot become an engine migration. W1.1
owns the writer; this is the table it writes into.

Money
-----
Integer minor units + an ISO-4217 ``currency`` code throughout, the same
``app_shared.money`` §19 contract C1's ledger expresses the same way.
There is no ``NUMERIC`` and no float anywhere in these four tables.

No DDL for the new enums
------------------------
``ReservationState``, ``AuthorizationPurpose`` and ``EntitlementState``
are app-validated ``VARCHAR(32)`` columns
(``app_shared.enums.enum_column`` -> ``_AppValidatedEnumString``), never
Postgres-native ``ENUM``s — the convention every status column in this
repo follows (``a6b0234cd4ad``, ``e7b21f3a8c94``, ``c4b19e7a2f08``).
There is nothing to ``CREATE TYPE``, and ``downgrade`` therefore has no
type to drop.

The one hand-written index
--------------------------
``uq_cost_reservations_live_dedupe_key`` is a PARTIAL unique index over
``(workspace_id, dedupe_key) WHERE state = 'RESERVED'``, mirroring
``OUTBOX_PENDING_DEDUP_INDEX``'s shape and for the same reason: at most
one LIVE grant may carry a dedupe key, but the same key must be reusable
once the previous grant settled. A full unique index would make a
domain's second-ever refresh collide with its first.

Reversible: ``downgrade`` drops the four tables (each one's RLS policy
goes with it) in dependency order. Nothing else in the schema references
them — C1's ``network_operations.authorization_id`` is deliberately a
plain nullable column with no foreign key (see that migration), so
dropping these tables leaves the ledger valid.

Hand-authored (matches ``app_shared.models.cost_authorization`` exactly)
— this build environment has no live Postgres connection for
autogenerate.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from app_shared.models import emit_rls_policy
from app_shared.models.cost_authorization import (
    COST_RESERVATION_DEDUPE_INDEX,
    COST_RESERVATION_DEDUPE_PREDICATE,
)

# revision identifiers, used by Alembic.
revision: str = "f1a7c02de5b4"
down_revision: Union[str, Sequence[str], None] = "d9a46612bcc8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _counter_columns() -> list[sa.Column]:
    """The four-dimension counter block both budget tables carry.

    Emitted from one helper because the tenant budget and the fleet
    provider budget are the SAME accounting object with different owners.
    Two hand-copied column lists would eventually differ by one dimension,
    and a dimension enforced against a workspace but not against the
    provider is a ceiling that silently is not one.
    """
    return [
        sa.Column("limit_cost_minor_units", sa.BigInteger(), nullable=True),
        sa.Column("limit_bytes", sa.BigInteger(), nullable=True),
        sa.Column("limit_requests", sa.BigInteger(), nullable=True),
        sa.Column("limit_browser_seconds", sa.BigInteger(), nullable=True),
        sa.Column(
            "reserved_cost_minor_units",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "reserved_bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "reserved_requests",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "reserved_browser_seconds",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "settled_cost_minor_units",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "settled_bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "settled_requests",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "settled_browser_seconds",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "decision_version", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "warned_thresholds",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("max_concurrent_reservations", sa.Integer(), nullable=True),
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
    ]


def upgrade() -> None:
    """Upgrade schema: reservations + two budget tables + entitlement evidence."""
    # --- 1. cost_reservations (workspace-owned, RLS'd) -------------------
    op.create_table(
        "cost_reservations",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("authorization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("transport", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("scrape_job_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("budget_decision_version", sa.Text(), nullable=False),
        sa.Column("entitlement_version", sa.Text(), nullable=True),
        sa.Column("breaker_decision", sa.Text(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("reserved_cost_minor_units", sa.BigInteger(), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("reserved_requests", sa.BigInteger(), nullable=False),
        sa.Column("reserved_browser_seconds", sa.BigInteger(), nullable=False),
        sa.Column("settled_cost_minor_units", sa.BigInteger(), nullable=True),
        sa.Column("settled_bytes", sa.BigInteger(), nullable=True),
        sa.Column("settled_requests", sa.BigInteger(), nullable=True),
        sa.Column("settled_browser_seconds", sa.BigInteger(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_cost_reservations"),
        sa.UniqueConstraint(
            "authorization_id", name="uq_cost_reservations_authorization_id"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_cost_reservations_workspace_id_workspaces",
        ),
        sa.CheckConstraint(
            "reserved_cost_minor_units >= 0 AND reserved_bytes >= 0 "
            "AND reserved_requests >= 0 AND reserved_browser_seconds >= 0",
            name="cr_reserved_non_negative",
        ),
        sa.CheckConstraint(
            "settled_cost_minor_units IS NULL OR settled_cost_minor_units >= 0",
            name="cr_settled_cost_non_negative",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="cr_currency_is_iso4217",
        ),
    )
    op.create_index(
        "ix_cost_reservations_workspace_id", "cost_reservations", ["workspace_id"]
    )
    op.create_index(
        "ix_cost_reservations_state_lease_expires_at",
        "cost_reservations",
        ["state", "lease_expires_at"],
    )
    op.create_index(
        "ix_cost_reservations_scrape_job_id", "cost_reservations", ["scrape_job_id"]
    )
    # PARTIAL unique: at most one LIVE grant per (workspace, dedupe_key),
    # while the same key stays reusable once the previous grant settled.
    # The predicate is imported from the model module so the index and the
    # `ON CONFLICT` arbiter can never be spelled two different ways.
    op.create_index(
        COST_RESERVATION_DEDUPE_INDEX,
        "cost_reservations",
        ["workspace_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text(COST_RESERVATION_DEDUPE_PREDICATE),
    )

    # --- 2. cost_budgets (workspace-owned, RLS'd) ------------------------
    op.create_table(
        "cost_budgets",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("period_key", sa.String(length=16), nullable=False),
        sa.Column(
            "currency", sa.String(length=3), nullable=False, server_default=sa.text("'USD'")
        ),
        *_counter_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_cost_budgets"),
        sa.UniqueConstraint(
            "workspace_id", "period_key", name="uq_cost_budgets_workspace_id_period_key"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_cost_budgets_workspace_id_workspaces",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name="cb_currency_is_iso4217"
        ),
        sa.CheckConstraint(
            "reserved_cost_minor_units >= 0 AND settled_cost_minor_units >= 0",
            name="cb_cost_counters_non_negative",
        ),
    )
    op.create_index("ix_cost_budgets_workspace_id", "cost_budgets", ["workspace_id"])

    # --- 3. fleet_cost_budgets (global, NO workspace_id, NO RLS) ---------
    op.create_table(
        "fleet_cost_budgets",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("period_key", sa.String(length=16), nullable=False),
        sa.Column(
            "currency", sa.String(length=3), nullable=False, server_default=sa.text("'USD'")
        ),
        *_counter_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_fleet_cost_budgets"),
        sa.UniqueConstraint(
            "scope_key", "period_key", name="uq_fleet_cost_budgets_scope_key_period_key"
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="fcb_currency_is_iso4217",
        ),
        sa.CheckConstraint(
            "reserved_cost_minor_units >= 0 AND settled_cost_minor_units >= 0",
            name="fcb_cost_counters_non_negative",
        ),
    )

    # --- 4. workspace_entitlements (workspace-owned, RLS'd) --------------
    op.create_table(
        "workspace_entitlements",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("plan_code", sa.Text(), nullable=True),
        sa.Column("evidence_version", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_workspace_entitlements"),
        sa.UniqueConstraint(
            "workspace_id", name="uq_workspace_entitlements_workspace_id"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_workspace_entitlements_workspace_id_workspaces",
        ),
    )
    op.create_index(
        "ix_workspace_entitlements_workspace_id",
        "workspace_entitlements",
        ["workspace_id"],
    )

    # --- 5. RLS on the three workspace-owned tables (§32, Principle II) --
    # `fleet_cost_budgets` deliberately gets none: it has no workspace_id
    # to scope by, and a policy there would hide the shared provider
    # ceiling from exactly the tenant connections it exists to stop.
    for table in ("cost_reservations", "cost_budgets", "workspace_entitlements"):
        for statement in emit_rls_policy(table):
            op.execute(statement)


def downgrade() -> None:
    """Downgrade schema: drop the four tables (policies go with them)."""
    op.drop_index(
        "ix_workspace_entitlements_workspace_id", table_name="workspace_entitlements"
    )
    op.drop_table("workspace_entitlements")

    op.drop_table("fleet_cost_budgets")

    op.drop_index("ix_cost_budgets_workspace_id", table_name="cost_budgets")
    op.drop_table("cost_budgets")

    op.drop_index(COST_RESERVATION_DEDUPE_INDEX, table_name="cost_reservations")
    op.drop_index("ix_cost_reservations_scrape_job_id", table_name="cost_reservations")
    op.drop_index(
        "ix_cost_reservations_state_lease_expires_at", table_name="cost_reservations"
    )
    op.drop_index("ix_cost_reservations_workspace_id", table_name="cost_reservations")
    op.drop_table("cost_reservations")
