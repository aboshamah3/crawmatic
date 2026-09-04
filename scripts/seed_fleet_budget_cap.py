#!/usr/bin/env python3
"""seed_fleet_budget_cap.py — put a money ceiling on the C1/C3 fleet budget.

`fleet_cost_budgets` is the fleet-owned half of the C3 gate: no
`workspace_id`, no RLS, one row per `(scope_key, period_key)` where
`scope_key` is a transport class (`proxy` / `browser` / `direct`, see
`app_shared.costauth.service.FLEET_PROVIDER_*`) and `period_key` is the
`%Y_%m` month. Every authorization locks that row before the tenant's and
decrements it — so it is the ONE place a fleet-wide money ceiling can
live, and the only ceiling that can stop a single tenant's runaway loop
from spending the shared provider balance.

Every `limit_*` column on every one of those rows is currently `NULL`,
which the model reads as "no ceiling on that dimension". There is
therefore no money ceiling at all. The 2026-08-12 extra.com discovery
leak (~$325/mo of proxy egress from one misconfigured `url_pattern`) is
what that costs when it goes wrong.

Owner decision (2026-08-26): **a fleet-wide monthly money cap.** This
script sets `limit_cost_micro_units` and nothing else — the other three
dimensions stay `NULL` deliberately: bytes/requests/browser-seconds have
no owner-agreed number, and inventing one would deny work nobody
budgeted for while adding no protection the money cap does not already
give (money is downstream of all three).

Three modes
-----------
* ``--propose`` — computes and PRINTS a recommended monthly cap
  (3x observed monthly spend) together with its full derivation, and
  writes nothing. It never guesses silently: if it cannot reach spend
  data it says so, in those words, and falls back to the repository's
  own measured evidence, naming the document each number came from.
* ``--apply --monthly-cap-usd D --currency XXX`` — upserts the cap onto
  the fleet budget rows. Dollars are the PRIMARY spelling since H4/B1:
  they are what the operator actually decides and what the settings
  (``FLEET_BUDGET_MONTHLY_CAP_USD_*``) hold, and they are converted by
  the same ``app_shared.costauth.fleet_budget_policy.usd_to_units`` the
  maintenance cadence uses. ``--monthly-cap-micro-units N`` spells the
  identical ceiling in the ledger's own unit.

  ``--monthly-cap-minor-units`` — the pre-H4 cents flag — is still
  PARSED and always REJECTED. The ledger's unit changed by a factor of
  10_000 under H4/B1, so an old runbook re-run verbatim would have set a
  cap 10_000x too small and silently strangled the fleet. It fails loudly
  instead.
* no flags — dry-run: prints exactly which rows would change and how.

Scope: which rows get the cap
------------------------------
``--scope-key`` (repeatable) defaults to the two PAID transport classes,
`proxy` and `browser`. `direct` is excluded by default because no call
site in this repository ever authorizes against it (`FLEET_PROVIDER_DIRECT`
is defined and unused — a DIRECT fetch is the free rung), so capping it
would be a ceiling on a counter that never moves.

Each named scope receives the SAME monthly ceiling, because the schema
has no single fleet-total row to put one number on. The script prints the
aggregate worst case (`cap x scopes`) on every run so that arithmetic is
never a surprise.

Periods: carried forward by the maintenance cadence
---------------------------------------------------
A fleet budget row is created on first use of a `(scope_key, period_key)`
pair with **NULL limits** (`app_shared.costauth.service.
_get_or_create_budget_locked`, and deliberately so — a row that
materialised with an invented ceiling would deny work nobody budgeted
for). Next month's row is therefore born uncapped, and a cap applied only
to the current month evaporates at the month boundary.

``--months-ahead`` (default 1, i.e. this month plus the next) exists for
exactly that reason. Since EPA A4/B3 it is no longer the only defence:
`app_shared.costauth.fleet_budget_policy` is the real budget-policy
writer, and `maintenance.fleet_budget_rollforward` runs it on the
scheduler's durable 6-hourly cadence, so the current and next month stay
capped without an operator. This script is now the way an operator
CHOOSES a number (`--propose`) and puts it in place immediately, not a
recurring calendar task.

Where the shared logic lives
-----------------------------
`DEFAULT_SCOPE_KEYS`, `period_keys_from`, `plan_budget_rows` and
`apply_budget_cap` moved to `app_shared.costauth.fleet_budget_policy` and
are re-imported here. The cadence and this script therefore run the SAME
"which rows get the cap" code — two implementations would be two answers
to what is capped, and only one of them would be running at 3am.

Session seam
------------
The sanctioned BYPASSRLS system session
(`app_shared.database.get_system_session`). `fleet_cost_budgets` is
declared `SYSTEM` in `scripts/rls_table_manifest.txt` and carries no
tenant column, and this script must additionally read the fleet-wide
ledger for `--propose`. Dry-run is DATABASE-level read-only (`SET
TRANSACTION READ ONLY` as the session's first statement), the same guard
`scripts/backfill_daily_rollups.py` uses.

Importable without connecting to anything: the engine is constructed
inside `main`, never at import time.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

# The shared policy: the cadence (`maintenance.fleet_budget_rollforward`)
# and this script must agree on which rows get the cap, so they run the
# same functions rather than two copies of them.
from app_shared.costauth.fleet_budget_policy import (
    DEFAULT_SCOPE_KEYS,
    USD_TO_UNITS,
    BudgetRowPlan,
    apply_budget_cap,
    period_keys_from,
    plan_budget_rows,
    usd_to_units,
)
from app_shared.models.network_operations import (
    NetworkOperation,
    NetworkOperationSettlement,
)

SessionFactory = Callable[[], Session]

#: The owner's multiplier: a cap is a runaway brake, not a forecast, so it
#: sits well clear of normal spend. 3x is high enough that ordinary
#: month-to-month variance (a catalogue growing, a site going hostile and
#: forcing browser escalation) never trips it, and low enough that the
#: 2026-08-12 extra.com discovery leak — ~5x the fleet's whole measured
#: proxy bill — would have been stopped by it.
CAP_MULTIPLIER = 3

#: Repository-carried spend evidence, used ONLY when the database has no
#: settled/estimated cost to measure. Each entry is
#: ``(monthly_micro_units, currency, derivation)`` and every derivation
#: names the document it came from, so a proposal can always be audited
#: back to a measurement rather than to this script's opinion.
#:
#: 60_000_000 micro-USD = $60.00/month: $2.00 per full catalogue refresh
#: (HANDOVER_READINESS_CYCLE_2026-08-15.md §6, measured against the
#: DataImpulse billing API when sizing a sub-user allocation; the same
#: run is priced at $1.96 from the billing API + prod DB in
#: PLAN_PROXY_COST_REDUCTION.md's Aug-10 baseline) at the daily cadence
#: PLAN_AMAZON_NOON_PRICING.md §3 costs out.
_REPO_EVIDENCE_MONTHLY_MICRO_UNITS = 60_000_000
_REPO_EVIDENCE_CURRENCY = "USD"
_REPO_EVIDENCE_DERIVATION = (
    "no settled or estimated cost found in the database; falling back to "
    "repository-carried measurements: $2.00 per full catalogue refresh "
    "(HANDOVER_READINESS_CYCLE_2026-08-15.md section 6, measured against the "
    "DataImpulse billing API; PLAN_PROXY_COST_REDUCTION.md's Aug-10 baseline "
    "prices the same run at $1.96) x 30 days at the daily cadence costed in "
    "PLAN_AMAZON_NOON_PRICING.md section 3 = $60.00/month observed proxy spend"
)


@dataclass(frozen=True)
class SpendObservation:
    """One month's observed fleet spend, and where the number came from.

    ``source`` is always populated and always names a table or a
    document. A proposal that cannot say where its number came from is a
    guess, and this script does not make guesses — see
    :func:`observe_monthly_spend`.
    """

    monthly_micro_units: int
    currency: str
    source: str
    derivation: str


def observe_monthly_spend(session: Session, *, now: datetime) -> SpendObservation:
    """Observed monthly fleet spend, from the best evidence available.

    Three sources, tried in descending order of authority, and the FIRST
    one that yields a non-zero number wins:

    1. ``network_operation_settlements.reconciled_cost_micro_units`` —
       money a provider's own usage export agreed to (EPA C5's
       reconciliation). This is the only source that is a *bill* rather
       than a model of one.
    2. ``network_operations.estimated_cost_micro_units`` — the ledger's
       own pre-dispatch pricing. Available whenever the fleet has run at
       all, and (being an estimate the settlement later corrects) it is
       the right second choice: an estimate that is too high produces a
       cap that is too generous, never one that denies real work.
    3. The repository's measured evidence
       (:data:`_REPO_EVIDENCE_DERIVATION`) — used when the database is
       empty, which is the normal state of a machine that has never run
       production traffic.

    Both database sources are scaled from the observed window to 30 days
    rather than assumed to be a full month: a ledger holding four days of
    traffic describes four days of spend, and reading it as a month would
    under-cap the fleet by ~7x.

    Never raises on an empty or missing ledger — an absent measurement is
    a reportable outcome, not an error.
    """
    settled = session.execute(
        select(
            func.coalesce(
                func.sum(NetworkOperationSettlement.reconciled_cost_micro_units), 0
            ),
            func.min(NetworkOperationSettlement.created_at),
            func.max(NetworkOperationSettlement.created_at),
            func.min(NetworkOperationSettlement.currency),
        )
    ).one()
    observation = _scale_to_month(
        total=int(settled[0] or 0),
        first=settled[1],
        last=settled[2],
        currency=settled[3],
        now=now,
        source="network_operation_settlements.reconciled_cost_micro_units",
        what="provider-reconciled settlements",
    )
    if observation is not None:
        return observation

    estimated = session.execute(
        select(
            func.coalesce(func.sum(NetworkOperation.estimated_cost_micro_units), 0),
            func.min(NetworkOperation.created_at),
            func.max(NetworkOperation.created_at),
            func.min(NetworkOperation.currency),
        )
    ).one()
    observation = _scale_to_month(
        total=int(estimated[0] or 0),
        first=estimated[1],
        last=estimated[2],
        currency=estimated[3],
        now=now,
        source="network_operations.estimated_cost_micro_units",
        what="ledger cost estimates",
    )
    if observation is not None:
        return observation

    return SpendObservation(
        monthly_micro_units=_REPO_EVIDENCE_MONTHLY_MICRO_UNITS,
        currency=_REPO_EVIDENCE_CURRENCY,
        source="repository evidence (no database spend reachable)",
        derivation=_REPO_EVIDENCE_DERIVATION,
    )


def _scale_to_month(
    *,
    total: int,
    first: datetime | None,
    last: datetime | None,
    currency: str | None,
    now: datetime,
    source: str,
    what: str,
) -> SpendObservation | None:
    """Project ``total`` over its observed window onto 30 days.

    Returns ``None`` — "this source has nothing to say" — when the total
    is zero or the window is unusable, so the caller can fall through to
    the next source. A window shorter than a day is treated as a full day
    to avoid multiplying a few minutes of traffic into an absurd monthly
    figure.
    """
    if total <= 0 or first is None or last is None:
        return None
    window_days = max((last - first).total_seconds() / 86_400.0, 1.0)
    monthly = int(round(total * 30.0 / window_days))
    return SpendObservation(
        monthly_micro_units=monthly,
        currency=currency or _REPO_EVIDENCE_CURRENCY,
        source=source,
        derivation=(
            f"{what}: {total} micro-USD observed over {window_days:.2f} day(s) "
            f"({first.isoformat()} -> {last.isoformat()}), scaled to 30 days "
            f"= {monthly} micro-USD/month"
        ),
    )


def propose_cap(observation: SpendObservation) -> int:
    """The recommended monthly cap: :data:`CAP_MULTIPLIER` x observed spend."""
    return observation.monthly_micro_units * CAP_MULTIPLIER


def format_proposal(observation: SpendObservation, *, scope_keys: Sequence[str]) -> str:
    """The `--propose` report: the number, the formula, and the provenance."""
    cap = propose_cap(observation)
    lines = [
        "seed_fleet_budget_cap PROPOSE",
        f"  observed monthly spend : {observation.monthly_micro_units} micro-USD "
        f"({observation.currency})",
        f"  source                 : {observation.source}",
        f"  derivation             : {observation.derivation}",
        f"  formula                : {CAP_MULTIPLIER} x observed monthly spend",
        f"  RECOMMENDED CAP        : {cap} micro-USD ({observation.currency}) "
        "per scope per month",
        f"  scopes                 : {', '.join(scope_keys)}",
        f"  aggregate worst case   : {cap * len(scope_keys)} micro-USD/month "
        f"across {len(scope_keys)} scope(s)",
        "  apply with             : "
        f"uv run python scripts/seed_fleet_budget_cap.py --apply "
        f"--monthly-cap-usd {cap / USD_TO_UNITS:.6f} "
        f"--currency {observation.currency}",
    ]
    return "\n".join(lines)


def format_plan(
    plans: Sequence[BudgetRowPlan], *, apply: bool, currency: str, changed: int
) -> str:
    """Per-row report of what changed (or would change)."""
    mode = "APPLY" if apply else "DRY-RUN"
    lines = [
        f"seed_fleet_budget_cap mode={mode} currency={currency} "
        f"rows_planned={len(plans)} rows_changed={changed}"
    ]
    for plan in plans:
        was = (
            "NULL (no ceiling)"
            if plan.existing_limit_micro_units is None
            else str(plan.existing_limit_micro_units)
        )
        verb = "unchanged" if not plan.is_change else ("row absent -> insert" if not plan.row_exists else "update")
        lines.append(
            f"  scope_key={plan.scope_key} period_key={plan.period_key} "
            f"limit_cost_micro_units: {was} -> {plan.new_limit_micro_units} ({verb})"
        )
    if plans:
        lines.append(
            f"  NOTE: the last period capped here is {plans[-1].period_key}; beyond "
            "it the cap is carried forward automatically by "
            "maintenance.fleet_budget_rollforward "
            "(app_shared.costauth.fleet_budget_policy), which re-caps the current "
            "and next month every 6h — so this is no longer a recurring operator "
            "task. Set FLEET_BUDGET_MONTHLY_CAP_USD_PROXY / _BROWSER to make that "
            "cadence write THIS number rather than carry the last one it finds."
        )
    return "\n".join(lines)


def run(
    *,
    session_factory: SessionFactory,
    now: datetime,
    propose: bool,
    apply: bool,
    monthly_cap_micro_units: int | None,
    currency: str,
    scope_keys: Sequence[str],
    months_ahead: int,
) -> str:
    """Run one invocation and return the text report. One session, one transaction.

    `--propose` and the write modes deliberately share a session: both
    are read-mostly, and a proposal computed against a different snapshot
    than the plan it recommends would be a proposal about a different
    fleet. Non-apply invocations (proposal AND dry run) issue `SET
    TRANSACTION READ ONLY` as the session's first statement.
    """
    session = session_factory()
    try:
        if not apply:
            session.execute(text("SET TRANSACTION READ ONLY"))

        if propose:
            report = format_proposal(
                observe_monthly_spend(session, now=now), scope_keys=scope_keys
            )
            session.rollback()
            return report

        if monthly_cap_micro_units is None:
            raise ValueError(
                "--monthly-cap-micro-units is required (run --propose first to "
                "get a recommendation derived from observed spend)"
            )

        period_keys = period_keys_from(now, months_ahead=months_ahead)
        plans = plan_budget_rows(
            session,
            scope_keys=scope_keys,
            period_keys=period_keys,
            monthly_cap_micro_units=monthly_cap_micro_units,
        )
        changed = 0
        if apply:
            changed = apply_budget_cap(
                session,
                plans=plans,
                currency=currency,
                monthly_cap_micro_units=monthly_cap_micro_units,
            )
            session.commit()
        else:
            changed = sum(1 for plan in plans if plan.is_change)
            session.rollback()
        return format_plan(plans, apply=apply, currency=currency, changed=changed)
    finally:
        session.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotently set a fleet-wide monthly money cap on "
            "fleet_cost_budgets.limit_cost_micro_units. Never touches "
            "per-workspace (cost_budgets) rows."
        )
    )
    parser.add_argument(
        "--propose",
        action="store_true",
        help=(
            "Compute and print a recommended monthly cap "
            f"({CAP_MULTIPLIER}x observed monthly spend) with its full "
            "derivation. Writes nothing."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Commit the cap. Without this flag (and without --propose), runs "
            "a dry-run: prints every row that WOULD change, then rolls back."
        ),
    )
    # One ceiling, three spellings — one of which is a tombstone.
    # `--monthly-cap-usd` is PRIMARY because dollars are what an operator
    # actually decides and what the SETTINGS the maintenance cadence
    # reads (`FLEET_BUDGET_MONTHLY_CAP_USD_PROXY` / `_BROWSER`) hold.
    # `--monthly-cap-micro-units` spells the same ceiling in the ledger's
    # own unit. `--monthly-cap-minor-units` is the pre-H4 cents flag: it
    # is still accepted by the parser and then always rejected, because
    # H4/B1 changed the ledger's unit by 10_000x and an old runbook
    # re-run verbatim would otherwise have capped the fleet at one
    # ten-thousandth of the intended ceiling — a silent outage, where
    # this is a loud one. Mutually exclusive: two ceilings on one run is
    # a question, not an instruction.
    cap_group = parser.add_mutually_exclusive_group()
    cap_group.add_argument(
        "--monthly-cap-usd",
        type=float,
        default=None,
        help=(
            "PRIMARY. The monthly money ceiling in DOLLARS, converted with "
            f"app_shared.costauth.fleet_budget_policy.usd_to_units "
            f"(x{USD_TO_UNITS}) — the identical conversion the "
            "maintenance cadence applies to "
            "FLEET_BUDGET_MONTHLY_CAP_USD_PROXY / _BROWSER, so the two "
            "routes to a cap can never disagree by a rounding step."
        ),
    )
    cap_group.add_argument(
        "--monthly-cap-micro-units",
        type=int,
        default=None,
        help=(
            "The same ceiling in integer MICRO-USD (1 USD == "
            f"{USD_TO_UNITS}) — never a float, per the app_shared.money "
            "contract. Use --monthly-cap-usd unless you need the exact "
            "ledger integer."
        ),
    )
    cap_group.add_argument(
        "--monthly-cap-minor-units",
        type=int,
        default=None,
        dest="monthly_cap_minor_units_removed",
        help=(
            "REMOVED (H4/B1): the ledger is in micro-USD, not cents. "
            "Accepted only so a pre-H4 runbook fails loudly instead of "
            "capping the fleet 10000x too low. Use --monthly-cap-usd."
        ),
    )
    parser.add_argument(
        "--currency",
        default="USD",
        help=(
            "ISO-4217 currency for rows this run CREATES (default: USD). An "
            "existing row's currency is never rewritten — the counters on it "
            "are already denominated."
        ),
    )
    parser.add_argument(
        "--scope-key",
        action="append",
        dest="scope_keys",
        default=None,
        help=(
            "Fleet scope_key to cap; repeatable. Default: "
            f"{' + '.join(DEFAULT_SCOPE_KEYS)} (the paid transport classes; "
            "'direct' is free and no call site authorizes against it)."
        ),
    )
    parser.add_argument(
        "--months-ahead",
        type=int,
        default=1,
        help=(
            "How many FUTURE months to cap alongside the current one "
            "(default: 1). A budget row is born with NULL limits, so a cap "
            "set only for this month evaporates at the month boundary."
        ),
    )
    args = parser.parse_args(argv)
    # Normalise to the ledger's unit immediately, so exactly one field
    # carries the cap from here on and `validate_args`/`run` never have
    # to ask which spelling was used.
    if args.monthly_cap_usd is not None:
        args.monthly_cap_micro_units = usd_to_units(args.monthly_cap_usd)
    return args


def validate_args(args: argparse.Namespace) -> str | None:
    """Return an error message for a self-contradictory invocation, else ``None``.

    Kept separate from :func:`parse_args` so the rules are unit-testable
    without argparse's ``SystemExit``.
    """
    if getattr(args, "monthly_cap_minor_units_removed", None) is not None:
        return (
            "--monthly-cap-minor-units was REMOVED by H4/B1: the ledger is no "
            f"longer in cents but in micro-USD (1 USD == {USD_TO_UNITS}). The "
            "same number passed through would have capped the fleet 10000x too "
            "low. Re-express the ceiling with --monthly-cap-usd "
            "(or --monthly-cap-micro-units for the exact ledger integer)."
        )
    if args.propose and args.apply:
        return "--propose and --apply are mutually exclusive: propose reads, apply writes"
    if args.apply and args.monthly_cap_micro_units is None:
        return "--apply requires --monthly-cap-usd or --monthly-cap-micro-units (run --propose first)"
    if args.monthly_cap_micro_units is not None and args.monthly_cap_micro_units <= 0:
        return "--monthly-cap-micro-units must be a positive integer of micro-USD"
    if len(args.currency) != 3 or not args.currency.isupper() or not args.currency.isalpha():
        return "--currency must be a three-letter uppercase ISO-4217 code"
    if args.months_ahead < 0:
        return "--months-ahead must be >= 0"
    return None


def main(argv: list[str] | None = None) -> int:
    """Entry point. The engine is built HERE, never at import time."""
    args = parse_args(argv)
    error = validate_args(args)
    if error is not None:
        print(f"seed_fleet_budget_cap: {error}", file=sys.stderr)
        return 1

    scope_keys = tuple(args.scope_keys) if args.scope_keys else DEFAULT_SCOPE_KEYS

    from app_shared.database import get_system_sessionmaker

    try:
        report = run(
            session_factory=get_system_sessionmaker(),
            now=datetime.now(timezone.utc),
            propose=args.propose,
            apply=args.apply,
            monthly_cap_micro_units=args.monthly_cap_micro_units,
            currency=args.currency,
            scope_keys=scope_keys,
            months_ahead=args.months_ahead,
        )
    except ValueError as exc:
        print(f"seed_fleet_budget_cap: {exc}", file=sys.stderr)
        return 1

    print(report)
    if not args.apply and not args.propose:
        print("seed_fleet_budget_cap: nothing written — re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
