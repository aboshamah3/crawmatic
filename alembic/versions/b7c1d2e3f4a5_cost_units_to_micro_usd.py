"""cost amounts: cents -> micro-USD (H4)

Revision ID: b7c1d2e3f4a5
Revises: e92029e9902c
Create Date: 2026-09-03

Why
---
Every money amount in this ledger was an integer number of currency
MINOR units — cents. Cents cannot represent what this fleet actually
spends. A direct fetch costs ``$0.0000046`` and a proxied amazon.sa fetch
``$0.00021``; in cents both are ``0``, and because the estimator floors
at one unit so that a stream of "free" requests still moves a counter,
both were booked as ``1`` — the same number, 47x to 2174x over reality,
for two costs that differ by a factor of 45. A ledger whose smallest
expressible amount is thousands of times the price of the thing it meters
is not a ledger; it is a request counter with a currency symbol on it.

This migration moves EVERY amount column to **micro-USD**
(1 USD == 1_000_000 units, :data:`SCALE` == 10_000 per cent) and renames
each one from ``*_cost_minor_units`` to ``*_cost_micro_units``. The
rename is the point: a silent unit change under an unchanged name is how
a rescaled ledger gets read by unrescaled code.

Rates are NOT touched. ``network_operations.billing_rate_micro_units``
keeps its own ``BILLING_RATE_SCALE`` (millionths of one cent) and no
amount is derived from it anywhere in the repository.

How
---
``ALTER TABLE ... ALTER COLUMN ... TYPE bigint USING col * 10000`` rather
than ``UPDATE``, and deliberately: ``network_operations`` carries a
BEFORE UPDATE trigger that rejects every UPDATE of a closed row (and a
closed row is precisely one with a cost), and
``network_operation_settlements`` carries one that rejects every UPDATE
full stop. An ``UPDATE``-based rescale would have to disable both
integrity triggers; ``ALTER COLUMN ... TYPE ... USING`` fires no row
triggers at all, re-verifies the CHECK constraints on the way through,
and never leaves a table's own invariants switched off.

The deferred ``trg_noa_allocation_total`` constraint trigger compares
``network_operation_allocations.allocated_cost_*`` against
``network_operations.estimated_cost_*`` at COMMIT. Both are scaled by the
same factor inside this one transaction, so the totals still agree.

``network_operation_allocations_check_total()`` IS recreated here.
PostgreSQL rewrites CHECK-constraint expressions when a column is
renamed, but it does NOT rewrite plpgsql function bodies, so that trigger
would fail at runtime on the first allocation written after this
migration.

``network_operations`` is NOT partitioned (``request_attempts`` is, and
carries no cost column), so a plain ``ALTER TABLE`` reaches every row.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c1d2e3f4a5"
down_revision: Union[str, None] = "e92029e9902c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Micro-USD per cent. 1 USD == 100 cents == 1_000_000 micro-USD.
SCALE = 10_000

#: Every money AMOUNT column in the ledger: ``(table, old, new)``.
#: Rates are absent on purpose — see the module docstring.
COLUMNS: list[tuple[str, str, str]] = [
    ("cost_reservations", "reserved_cost_minor_units", "reserved_cost_micro_units"),
    ("cost_reservations", "settled_cost_minor_units", "settled_cost_micro_units"),
    ("cost_budgets", "limit_cost_minor_units", "limit_cost_micro_units"),
    ("cost_budgets", "reserved_cost_minor_units", "reserved_cost_micro_units"),
    ("cost_budgets", "settled_cost_minor_units", "settled_cost_micro_units"),
    ("fleet_cost_budgets", "limit_cost_minor_units", "limit_cost_micro_units"),
    ("fleet_cost_budgets", "reserved_cost_minor_units", "reserved_cost_micro_units"),
    ("fleet_cost_budgets", "settled_cost_minor_units", "settled_cost_micro_units"),
    (
        "fleet_network_cost_rollups",
        "estimated_cost_minor_units",
        "estimated_cost_micro_units",
    ),
    (
        "fleet_network_cost_rollups",
        "reconciled_cost_minor_units",
        "reconciled_cost_micro_units",
    ),
    (
        "network_cost_rollups",
        "estimated_cost_minor_units",
        "estimated_cost_micro_units",
    ),
    (
        "network_cost_rollups",
        "reconciled_cost_minor_units",
        "reconciled_cost_micro_units",
    ),
    ("network_operations", "estimated_cost_minor_units", "estimated_cost_micro_units"),
    (
        "network_operation_allocations",
        "allocated_cost_minor_units",
        "allocated_cost_micro_units",
    ),
    (
        "network_operation_settlements",
        "reconciled_cost_minor_units",
        "reconciled_cost_micro_units",
    ),
]


def _rescale(table: str, column: str, expression: str) -> None:
    """Rewrite one column through ``expression``, firing no row triggers.

    Every column here is already ``bigint``; the redundant ``TYPE bigint``
    is what makes PostgreSQL apply the ``USING`` rewrite. ``NULL`` stays
    ``NULL`` — a NULL limit means "no ceiling", and no scale factor should
    turn that into a number.
    """
    op.execute(
        f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE bigint USING {expression}'
    )


#: The allocation-total trigger function spelled against the NEW column
#: names. Identical to the definition in
#: ``app_shared.models.network_operations`` apart from those names.
ALLOCATION_TOTAL_SQL_MICRO = """
CREATE OR REPLACE FUNCTION network_operation_allocations_check_total()
RETURNS trigger AS $$
DECLARE
    op_id        uuid;
    op_cost      bigint;
    alloc_total  bigint;
    alloc_frac   bigint;
    alloc_count  integer;
BEGIN
    IF TG_OP = 'DELETE' THEN
        op_id := OLD.operation_id;
    ELSE
        op_id := NEW.operation_id;
    END IF;

    SELECT o.estimated_cost_micro_units
      INTO op_cost
      FROM network_operations o
     WHERE o.network_request_id = op_id;

    IF NOT FOUND OR op_cost IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT COALESCE(SUM(a.allocated_cost_micro_units), 0),
           COALESCE(SUM(a.fraction_ppb), 0),
           COUNT(*)
      INTO alloc_total, alloc_frac, alloc_count
      FROM network_operation_allocations a
     WHERE a.operation_id = op_id;

    IF alloc_count = 0 THEN
        RETURN NULL;
    END IF;

    IF alloc_total <> op_cost THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'network_operation_allocations do not sum to the operation cost',
            DETAIL  = 'allocated total ' || alloc_total::text
                      || ' micro-USD vs operation cost ' || op_cost::text
                      || ' micro-USD. Split with largest-remainder rounding so the '
                      || 'parts sum exactly.';
    END IF;

    IF alloc_frac <> 1000000000 THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'network_operation_allocations fractions do not sum to 1.0',
            DETAIL  = 'fraction_ppb total ' || alloc_frac::text || ' of 1000000000.';
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

#: The same function against the OLD (cents) column names, for downgrade.
ALLOCATION_TOTAL_SQL_MINOR = (
    ALLOCATION_TOTAL_SQL_MICRO.replace(
        "estimated_cost_micro_units", "estimated_cost_minor_units"
    )
    .replace("allocated_cost_micro_units", "allocated_cost_minor_units")
    .replace("micro-USD", "minor units")
)


def upgrade() -> None:
    # Scale first, while the columns still answer to their old names, so a
    # half-applied migration is never a renamed column holding an
    # unrescaled number.
    for table, old, _new in COLUMNS:
        _rescale(table, old, f'"{old}" * {SCALE}')
    for table, old, new in COLUMNS:
        op.alter_column(table, old, new_column_name=new)
    op.execute(ALLOCATION_TOTAL_SQL_MICRO)


def downgrade() -> None:
    # Rename back first, then divide. Integer division truncates, so a
    # downgrade LOSES every sub-cent amount this ledger exists to record —
    # the honest cost of the old unit, and the reason the forward
    # direction is the only one anyone should run against real data.
    for table, old, new in COLUMNS:
        op.alter_column(table, new, new_column_name=old)
    for table, old, _new in COLUMNS:
        _rescale(table, old, f'"{old}" / {SCALE}')
    op.execute(ALLOCATION_TOTAL_SQL_MINOR)
