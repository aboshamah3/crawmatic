"""Fleet budget cap policy — the fleet money ceiling, kept in place unattended.

`fleet_cost_budgets` is the fleet-owned half of the C3 gate: no
``workspace_id``, no RLS, one row per ``(scope_key, period_key)`` where
``scope_key`` is a paid transport class (``proxy`` / ``browser``, see
:data:`~app_shared.costauth.service.FLEET_PROVIDER_PROXY`) and
``period_key`` is the ``%Y_%m`` month. Every authorization locks that row
before the tenant's and decrements it, so it is the ONE place a
fleet-wide money ceiling can live — and the only ceiling that can stop a
single tenant's runaway loop from draining the shared provider balance.

## Why a policy module and not just the seeder

`scripts/seed_fleet_budget_cap.py` (2026-08-26) put that ceiling on the
rows for a FIXED number of months: the operator ran it once, with
``--months-ahead 1``, and the last period it covered is ``2026_10``. A
budget row is born with ``NULL`` limits — deliberately, because a row
that materialised with an invented ceiling would deny work nobody
budgeted for (`app_shared.costauth.service._get_or_create_budget_locked`)
— so on the first paid dispatch of the month after the last one seeded,
`authorize()` creates an uncapped row and the fleet money ceiling is
simply gone. Nothing logs it. Nothing denies. The only symptom is the
provider bill.

The seeder's own docstring names the missing piece: *"until a real
budget-policy writer exists, re-running this script is a recurring
operator task"*. This module is that writer, and
`apps/workers/app/workers/tasks_maintenance.py::fleet_budget_rollforward`
runs it on the scheduler's durable 6-hourly cadence — so the current and
the next month are always capped, and a month boundary is never crossed
by more than one tick's worth of uncapped time.

## The three-way decision, per (scope, period)

1. **A row already carries a non-``NULL`` limit** -> leave it completely
   alone. Never lower it, never overwrite it, never touch a counter. The
   operator (or an earlier run) decided that number; a cadence that
   second-guesses it is a cadence that can silently deny budgeted work,
   and rewriting ``settled_*`` would hand back money already spent.
2. **A cap is configured for the scope** (``FLEET_BUDGET_MONTHLY_CAP_USD_
   PROXY`` / ``..._BROWSER``) -> write it.
3. **Neither** -> carry the most recent EARLIER period's cap forward. The
   ceiling an operator already chose is the best evidence available, and
   carrying it is the only option that neither invents a number nor
   leaves the fleet uncapped. With nothing to carry either, the pair is
   reported in :attr:`RollForwardReport.uncapped` — a reportable outcome,
   logged at ERROR by the task, never a silent one.

## Money units

:data:`USD_TO_UNITS` is the single conversion between the operator-facing
dollars of the settings and the ledger's integer units. Since H4/B1 the
ledger is in **micro-USD** (1 USD == 1_000_000 units) — cents could not
represent a ``$0.0000046`` direct request at all, and rounded every one
of them up to a whole cent. Every call site goes through
:func:`usd_to_units`, and the constant itself is
:data:`~app_shared.costauth.service.MICRO_UNITS_PER_USD` so the ledger's
unit has exactly one definition in the repository.

## Session seam

Every function here takes a `Session` and commits nothing — the caller
owns the transaction. The maintenance task supplies the sanctioned
BYPASSRLS system session (`fleet_cost_budgets` is declared ``SYSTEM`` in
`scripts/rls_table_manifest.txt` and carries no tenant column).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_shared.costauth.service import (
    FLEET_PROVIDER_BROWSER,
    FLEET_PROVIDER_PROXY,
    MICRO_UNITS_PER_USD,
    period_key_for,
)
from app_shared.models.cost_authorization import FleetCostBudget

#: The paid transport classes a fleet money cap must bind. ``direct`` is
#: deliberately absent: it is the free rung and no call site in this
#: repository ever authorizes against ``FLEET_PROVIDER_DIRECT``, so
#: capping it would be a ceiling on a counter that never moves.
DEFAULT_SCOPE_KEYS: tuple[str, ...] = (FLEET_PROVIDER_PROXY, FLEET_PROVIDER_BROWSER)

#: Ledger units in one US dollar — **micro-USD** since H4/B1 (was cents).
#: Re-exported from :mod:`app_shared.costauth.service` rather than
#: re-spelled, so the ledger's unit has ONE definition: a second literal
#: here is how a converter and an estimator drift 10_000x apart.
USD_TO_UNITS = MICRO_UNITS_PER_USD


def usd_to_units(usd: float) -> int:
    """Convert operator-facing dollars to integer ledger units.

    ``round`` rather than ``int()`` because binary floats truncate the
    wrong way: ``int(0.29 * 100)`` is 28, and a ceiling is not a place to
    lose a unit to representation. A value below one ledger unit converts
    to ``0`` — see :func:`roll_fleet_budget_caps_forward`, which refuses
    to write a ``0`` ceiling.
    """
    return int(round(usd * USD_TO_UNITS))


@dataclass(frozen=True)
class BudgetRowPlan:
    """What one ``(scope_key, period_key)`` row would become."""

    scope_key: str
    period_key: str
    existing_limit_micro_units: int | None
    new_limit_micro_units: int
    row_exists: bool

    @property
    def is_change(self) -> bool:
        """False when the row already carries exactly this cap."""
        return self.existing_limit_micro_units != self.new_limit_micro_units


@dataclass(frozen=True)
class RollForwardReport:
    """The outcome of one roll-forward pass. Every field is operator-facing.

    ``uncapped`` is the one that matters: it names every
    ``(scope_key, period_key)`` the pass could neither configure nor
    carry, i.e. every period the fleet will run through with no money
    ceiling at all. The task logs it at ERROR for exactly that reason.
    """

    written: int
    carried: list[tuple[str, str]]
    uncapped: list[tuple[str, str]]


def period_keys_from(now: datetime, *, months_ahead: int) -> tuple[str, ...]:
    """``%Y_%m`` keys for the current month plus ``months_ahead`` more.

    Computed by walking month numbers rather than adding days, so a
    31-day month never skips a period and February never doubles one.
    ``months_ahead=0`` yields the current month alone.
    """
    if months_ahead < 0:
        raise ValueError("months_ahead must be >= 0")
    keys: list[str] = []
    year, month = now.year, now.month
    for _ in range(months_ahead + 1):
        keys.append(period_key_for(datetime(year, month, 1, tzinfo=timezone.utc)))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return tuple(keys)


def plan_budget_rows(
    session: Session,
    *,
    scope_keys: Sequence[str],
    period_keys: Sequence[str],
    monthly_cap_micro_units: int,
) -> list[BudgetRowPlan]:
    """Classify every targeted ``(scope_key, period_key)`` row. Writes nothing.

    Separated from :func:`apply_budget_cap` so the seeder's dry run and
    its apply report the SAME plan rather than two independently-computed
    ones — the property that makes a dry run worth reading.
    """
    existing = {
        (row.scope_key, row.period_key): row
        for row in session.execute(
            select(FleetCostBudget).where(
                FleetCostBudget.scope_key.in_(list(scope_keys)),
                FleetCostBudget.period_key.in_(list(period_keys)),
            )
        ).scalars()
    }
    plans: list[BudgetRowPlan] = []
    for scope_key in scope_keys:
        for period_key in period_keys:
            row = existing.get((scope_key, period_key))
            plans.append(
                BudgetRowPlan(
                    scope_key=scope_key,
                    period_key=period_key,
                    existing_limit_micro_units=(
                        None if row is None else row.limit_cost_micro_units
                    ),
                    new_limit_micro_units=monthly_cap_micro_units,
                    row_exists=row is not None,
                )
            )
    return plans


def apply_budget_cap(
    session: Session,
    *,
    plans: Sequence[BudgetRowPlan],
    currency: str,
    monthly_cap_micro_units: int,
) -> int:
    """Upsert ``limit_cost_micro_units`` for every planned row. Returns rows changed.

    Only ``limit_cost_micro_units`` is written. The ``reserved_*`` /
    ``settled_*`` counters are the authorization path's to maintain and
    are never touched here — resetting a counter would hand back money
    the fleet has already spent — and the other three ``limit_*`` columns
    stay ``NULL``: bytes/requests/browser-seconds have no owner-agreed
    number, and inventing one would deny work nobody budgeted for while
    adding no protection the money cap does not already give.

    A row that does not exist yet is INSERTed with the cap already on it,
    rather than left for ``_get_or_create_budget_locked`` to materialise
    uncapped on the month's first authorization. That is the whole point
    of covering a month ahead: the ceiling must be in place BEFORE the
    first spend of the period, not after it.

    Does not commit — the caller owns the transaction.
    """
    changed = 0
    for plan in plans:
        if not plan.is_change:
            continue
        if plan.row_exists:
            row = session.execute(
                select(FleetCostBudget).where(
                    FleetCostBudget.scope_key == plan.scope_key,
                    FleetCostBudget.period_key == plan.period_key,
                )
            ).scalar_one()
            row.limit_cost_micro_units = monthly_cap_micro_units
        else:
            session.add(
                FleetCostBudget(
                    scope_key=plan.scope_key,
                    period_key=plan.period_key,
                    currency=currency,
                    limit_cost_micro_units=monthly_cap_micro_units,
                )
            )
        changed += 1
    return changed


def _read_known_limits(
    session: Session, *, scope_keys: Sequence[str], through_period_key: str
) -> dict[tuple[str, str], int | None]:
    """One snapshot of every capped/uncapped period up to ``through_period_key``.

    ONE read rather than a lookup per ``(scope, period)``: the table is
    two rows per month by construction (one per paid transport class), so
    the whole of it is a handful of rows even after years — and a single
    snapshot means the "is this already capped?" decision and the "what
    did we cap last?" decision can never be made against two different
    states of the table.

    ``%Y_%m`` sorts lexicographically in chronological order, which is
    what makes ``<=`` a valid period bound and
    :func:`_latest_known_limit`'s ``max`` a valid "most recent".
    """
    rows = session.execute(
        select(FleetCostBudget).where(
            FleetCostBudget.scope_key.in_(list(scope_keys)),
            FleetCostBudget.period_key <= through_period_key,
        )
    ).scalars()
    return {(row.scope_key, row.period_key): row.limit_cost_micro_units for row in rows}


def _latest_known_limit(
    limits: dict[tuple[str, str], int | None],
    *,
    scope_key: str,
    before_period_key: str,
) -> int | None:
    """The most recent EARLIER non-``NULL`` cap for ``scope_key``, or ``None``."""
    candidates = [
        (period_key, limit)
        for (scope, period_key), limit in limits.items()
        if scope == scope_key and period_key < before_period_key and limit is not None
    ]
    if not candidates:
        return None
    return max(candidates)[1]


def roll_fleet_budget_caps_forward(
    session: Session,
    *,
    now: datetime,
    months_ahead: int,
    caps_usd: dict[str, float | None],
    currency: str = "USD",
) -> RollForwardReport:
    """Ensure every paid scope is capped for this month and ``months_ahead`` more.

    The three-way decision per ``(scope_key, period_key)`` is spelled out
    in the module docstring: an existing non-``NULL`` limit is untouched,
    a configured cap is written, and otherwise the most recent earlier
    cap is carried forward — or the pair is reported ``uncapped``.

    Idempotent: a second call in the same period finds every row already
    capped and writes nothing, so the 6-hourly cadence costs one SELECT
    on all but the first tick of a month.

    A configured cap that does not survive conversion to a positive
    number of ledger units (:func:`usd_to_units`) is treated as ABSENT
    rather than written. A ``0`` ceiling denies all paid work fleet-wide —
    the exact outage this module exists to prevent, arrived at by a typo —
    so the carry-forward (or the uncapped report) handles it instead.

    Does not commit — the caller owns the transaction.
    """
    period_keys = period_keys_from(now, months_ahead=months_ahead)
    limits = _read_known_limits(
        session, scope_keys=DEFAULT_SCOPE_KEYS, through_period_key=period_keys[-1]
    )

    written = 0
    carried: list[tuple[str, str]] = []
    uncapped: list[tuple[str, str]] = []

    for scope_key in DEFAULT_SCOPE_KEYS:
        configured_usd = caps_usd.get(scope_key)
        configured_units: int | None = (
            None if configured_usd is None else usd_to_units(configured_usd)
        )
        if configured_units is not None and configured_units <= 0:
            configured_units = None

        for period_key in period_keys:
            if limits.get((scope_key, period_key)) is not None:
                continue  # already capped: never lower, never overwrite

            if configured_units is not None:
                new_limit = configured_units
            else:
                inherited = _latest_known_limit(
                    limits, scope_key=scope_key, before_period_key=period_key
                )
                if inherited is None:
                    uncapped.append((scope_key, period_key))
                    continue
                new_limit = inherited
                carried.append((scope_key, period_key))

            plans = plan_budget_rows(
                session,
                scope_keys=(scope_key,),
                period_keys=(period_key,),
                monthly_cap_micro_units=new_limit,
            )
            written += apply_budget_cap(
                session,
                plans=plans,
                currency=currency,
                monthly_cap_micro_units=new_limit,
            )
            # So a later period in this same pass can carry what this one
            # just wrote, without a second read.
            limits[(scope_key, period_key)] = new_limit

    return RollForwardReport(written=written, carried=carried, uncapped=uncapped)


__all__ = [
    "BudgetRowPlan",
    "DEFAULT_SCOPE_KEYS",
    "RollForwardReport",
    "USD_TO_UNITS",
    "apply_budget_cap",
    "period_keys_from",
    "plan_budget_rows",
    "roll_fleet_budget_caps_forward",
    "usd_to_units",
]
