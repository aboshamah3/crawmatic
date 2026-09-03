"""Unit tests for `app_shared.costauth.fleet_budget_policy` (EPA A4/B3).

The defect this module closes: `scripts/seed_fleet_budget_cap.py` capped
`fleet_cost_budgets` for a FIXED number of months on one operator run.
The last month it covered is October 2026 — after which
`_get_or_create_budget_locked` materialises next month's row with `NULL`
limits on the first paid dispatch, and the fleet-wide money ceiling is
silently gone. "Re-run the script every month, forever" is not a control;
it is a reminder, and reminders are exactly what the 2026-08-12
extra.com discovery leak (~$325/mo of proxy egress from one misconfigured
`url_pattern`) got past.

So the properties pinned here are the ones that make an unattended
cadence safe to point at a money ceiling:

1. a configured cap is written for the current AND the next month, for
   every paid scope, so a month boundary is never crossed uncapped;
2. with no configured cap the LAST KNOWN cap carries forward — the
   cadence keeps the ceiling an operator already chose rather than
   inventing one;
3. an existing explicit cap is never lowered, never overwritten, and the
   `reserved_*`/`settled_*` counters are never touched (they are the
   authorization path's money, already spent);
4. when nothing is configured and nothing is known, the pair is REPORTED
   uncapped rather than papered over with a guess — that report is what
   the task logs at ERROR.

DB-independent, and not by preference: `FleetCostBudget` carries a
`JSONB` column with a `'[]'::jsonb` server default and a POSIX-regex
`CHECK` constraint, none of which SQLite can compile (re-confirmed while
writing this file), so the substitute is the same hand-rolled fake as
`tests/unit/test_seed_fleet_budget_cap.py`. This one additionally
EVALUATES the statement's real `WHERE` clause against its rows, so the
filtering the policy relies on is genuinely exercised rather than
assumed; an operator the evaluator does not know raises rather than
silently matching everything.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.sql.elements import BooleanClauseList

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.seed_fleet_budget_cap as capseed  # noqa: E402
from app_shared.costauth import fleet_budget_policy as policy  # noqa: E402
from app_shared.costauth.service import (  # noqa: E402
    FLEET_PROVIDER_BROWSER,
    FLEET_PROVIDER_PROXY,
)
from app_shared.models.cost_authorization import CostBudget, FleetCostBudget  # noqa: E402

#: The run this module was designed against: the last month the one-off
#: seeder covered is 2026_10, so a September run is the last one whose
#: carry-forward still has a cap to carry.
NOW_SEP = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _matches(clause: Any, row: Any) -> bool:
    """Evaluate a real SQLAlchemy `WHERE` clause against one ORM object.

    Handles exactly the operators the policy builds. Anything else raises
    — a fake that silently matched an operator it did not understand
    would turn a filtering bug into a green test.
    """
    if clause is None:
        return True
    if isinstance(clause, BooleanClauseList):
        return all(_matches(child, row) for child in clause.clauses)
    operator_name = clause.operator.__name__
    actual = getattr(row, clause.left.name)
    expected = clause.right.value
    if operator_name == "eq":
        return actual == expected
    if operator_name == "in_op":
        return actual in expected
    if operator_name == "le":
        return actual is not None and actual <= expected
    raise AssertionError(f"fake session cannot evaluate operator {operator_name!r}")


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> list[Any]:
        return self._rows

    def scalar_one(self) -> Any:
        assert len(self._rows) == 1, self._rows
        return self._rows[0]


class _FakeSession:
    """Rows in, filtered rows out. `.add()` makes the row visible to the
    next `.execute()`, exactly as a real `Session`'s autoflush does."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows: list[Any] = list(rows or [])
        self.added: list[Any] = []
        self.commits = 0
        self.executes = 0

    def execute(self, statement: Any) -> Any:
        self.executes += 1
        return _FakeResult(
            [row for row in self.rows if _matches(statement.whereclause, row)]
        )

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        self.rows.append(obj)

    def commit(self) -> None:
        self.commits += 1


def _row(scope_key: str, period_key: str, limit: int | None, **counters: int):
    row = FleetCostBudget(
        scope_key=scope_key,
        period_key=period_key,
        currency="USD",
        limit_cost_minor_units=limit,
    )
    for name, value in counters.items():
        setattr(row, name, value)
    return row


def _limits(session: _FakeSession) -> dict[tuple[str, str], int | None]:
    return {
        (row.scope_key, row.period_key): row.limit_cost_minor_units
        for row in session.rows
    }


# --- money conversion ---------------------------------------------------


def test_usd_converts_to_the_ledger_minor_unit() -> None:
    """One place converts operator dollars into ledger units, so Task B1's
    micro-USD flip is one constant rather than a hunt through call sites."""
    assert policy.USD_TO_UNITS == 100
    assert policy.usd_to_units(75.0) == 7_500
    assert policy.usd_to_units(25.0) == 2_500


def test_usd_to_units_rounds_rather_than_truncates() -> None:
    """`int(x * 100)` on binary floats truncates ($0.29 -> 28 cents). A cap
    is not a place to lose a unit to representation, and a sub-unit cap
    rounds to 0 rather than to a silently-wrong number."""
    assert policy.usd_to_units(0.29) == 29  # 0.29 * 100 == 28.999999999999996
    assert policy.usd_to_units(1.999) == 200
    assert policy.usd_to_units(0.0001) == 0  # below one ledger unit == nothing


# --- the roll-forward ---------------------------------------------------


def test_configured_caps_are_written_for_this_month_and_the_next() -> None:
    """The whole point: October is capped without an operator, and so is
    every month after it, because every run covers the boundary ahead."""
    session = _FakeSession()

    report = policy.roll_fleet_budget_caps_forward(
        session,
        now=NOW_SEP,
        months_ahead=1,
        caps_usd={FLEET_PROVIDER_PROXY: 75.0, FLEET_PROVIDER_BROWSER: 25.0},
    )

    assert report.written == 4
    assert report.carried == []
    assert report.uncapped == []
    assert _limits(session) == {
        (FLEET_PROVIDER_PROXY, "2026_09"): 7_500,
        (FLEET_PROVIDER_PROXY, "2026_10"): 7_500,
        (FLEET_PROVIDER_BROWSER, "2026_09"): 2_500,
        (FLEET_PROVIDER_BROWSER, "2026_10"): 2_500,
    }


def test_the_last_known_cap_carries_forward_when_nothing_is_configured() -> None:
    """A deploy with no cap env vars must not silently uncap the fleet. The
    ceiling an operator already chose is the best evidence available, so it
    is carried rather than replaced by a guess or by NULL."""
    session = _FakeSession([_row(FLEET_PROVIDER_PROXY, "2026_08", 9_000)])

    report = policy.roll_fleet_budget_caps_forward(
        session,
        now=NOW_SEP,
        months_ahead=1,
        caps_usd={FLEET_PROVIDER_PROXY: None, FLEET_PROVIDER_BROWSER: 25.0},
    )

    assert report.carried == [
        (FLEET_PROVIDER_PROXY, "2026_09"),
        (FLEET_PROVIDER_PROXY, "2026_10"),
    ]
    assert report.uncapped == []
    assert report.written == 4
    assert _limits(session)[(FLEET_PROVIDER_PROXY, "2026_09")] == 9_000
    assert _limits(session)[(FLEET_PROVIDER_PROXY, "2026_10")] == 9_000


def test_the_most_recent_earlier_cap_wins_when_several_are_known() -> None:
    """`%Y_%m` sorts chronologically, and "most recent" must mean July over
    March — a cap the operator raised must not be undone by an older one."""
    session = _FakeSession(
        [
            _row(FLEET_PROVIDER_PROXY, "2026_03", 1_000),
            _row(FLEET_PROVIDER_PROXY, "2026_07", 4_000),
            _row(FLEET_PROVIDER_PROXY, "2026_05", 2_000),
        ]
    )

    policy.roll_fleet_budget_caps_forward(
        session,
        now=NOW_SEP,
        months_ahead=1,
        caps_usd={FLEET_PROVIDER_PROXY: None, FLEET_PROVIDER_BROWSER: 25.0},
    )

    assert _limits(session)[(FLEET_PROVIDER_PROXY, "2026_09")] == 4_000
    assert _limits(session)[(FLEET_PROVIDER_PROXY, "2026_10")] == 4_000


def test_an_existing_cap_is_never_lowered_and_no_counter_is_touched() -> None:
    """Two independent refusals in one row. Lowering a ceiling an operator
    raised on purpose would deny work they had budgeted for; resetting
    `settled_*` would hand back money the fleet has already spent."""
    existing = _row(
        FLEET_PROVIDER_PROXY,
        "2026_09",
        9_000,
        settled_cost_minor_units=12_345,
        reserved_cost_minor_units=678,
    )
    session = _FakeSession([existing])

    report = policy.roll_fleet_budget_caps_forward(
        session,
        now=NOW_SEP,
        months_ahead=1,
        caps_usd={FLEET_PROVIDER_PROXY: 75.0, FLEET_PROVIDER_BROWSER: 25.0},
    )

    assert existing.limit_cost_minor_units == 9_000
    assert existing.settled_cost_minor_units == 12_345
    assert existing.reserved_cost_minor_units == 678
    # proxy/2026_09 skipped; the other three pairs written.
    assert report.written == 3
    assert _limits(session)[(FLEET_PROVIDER_PROXY, "2026_10")] == 7_500


def test_a_row_born_uncapped_gets_the_configured_cap(
) -> None:
    """The exact production shape: `_get_or_create_budget_locked` already
    materialised this month's row with `NULL` limits on the first paid
    dispatch. The cadence must UPDATE it, not skip it and not insert a
    duplicate."""
    born_uncapped = _row(
        FLEET_PROVIDER_PROXY, "2026_09", None, settled_cost_minor_units=4_242
    )
    session = _FakeSession([born_uncapped])

    report = policy.roll_fleet_budget_caps_forward(
        session,
        now=NOW_SEP,
        months_ahead=1,
        caps_usd={FLEET_PROVIDER_PROXY: 75.0, FLEET_PROVIDER_BROWSER: 25.0},
    )

    assert born_uncapped.limit_cost_minor_units == 7_500
    assert born_uncapped.settled_cost_minor_units == 4_242
    assert session.added == [
        row for row in session.rows if row is not born_uncapped
    ], "the existing row must be updated in place, never re-inserted"
    assert report.written == 4


def test_pairs_with_no_configured_and_no_known_cap_are_reported_uncapped() -> None:
    """"Nothing is known" is a reportable outcome, never a silent one: this
    list is what the maintenance task logs at ERROR, and it is the only
    signal an operator gets that the fleet is running without a ceiling."""
    session = _FakeSession()

    report = policy.roll_fleet_budget_caps_forward(
        session,
        now=NOW_SEP,
        months_ahead=1,
        caps_usd={FLEET_PROVIDER_PROXY: None, FLEET_PROVIDER_BROWSER: None},
    )

    assert report.written == 0
    assert report.carried == []
    assert report.uncapped == [
        (FLEET_PROVIDER_PROXY, "2026_09"),
        (FLEET_PROVIDER_PROXY, "2026_10"),
        (FLEET_PROVIDER_BROWSER, "2026_09"),
        (FLEET_PROVIDER_BROWSER, "2026_10"),
    ]
    assert session.rows == []


def test_a_second_run_writes_nothing(
) -> None:
    """Idempotence stated as a property. The cadence fires four times a
    day; the second run of a day must be a pure read."""
    session = _FakeSession()
    caps = {FLEET_PROVIDER_PROXY: 75.0, FLEET_PROVIDER_BROWSER: 25.0}

    policy.roll_fleet_budget_caps_forward(
        session, now=NOW_SEP, months_ahead=1, caps_usd=caps
    )
    second = policy.roll_fleet_budget_caps_forward(
        session, now=NOW_SEP, months_ahead=1, caps_usd=caps
    )

    assert second.written == 0
    assert second.carried == []
    assert second.uncapped == []


def test_a_cap_that_rounds_away_to_nothing_is_not_written() -> None:
    """A `0` ceiling denies ALL paid work fleet-wide — the outage this
    module exists to prevent, arrived at by a typo. A configured cap that
    does not survive conversion is treated as absent, so the carry-forward
    (or the uncapped report) handles it."""
    session = _FakeSession()

    report = policy.roll_fleet_budget_caps_forward(
        session,
        now=NOW_SEP,
        months_ahead=1,
        caps_usd={FLEET_PROVIDER_PROXY: 0.0001, FLEET_PROVIDER_BROWSER: 25.0},
    )

    assert (FLEET_PROVIDER_PROXY, "2026_09") in report.uncapped
    assert _limits(session).get((FLEET_PROVIDER_PROXY, "2026_09")) is None
    assert _limits(session)[(FLEET_PROVIDER_BROWSER, "2026_09")] == 2_500


def test_only_the_money_limit_is_written_on_a_created_row() -> None:
    """Bytes/requests/browser-seconds have no owner-agreed number; the
    module inherits the seeder's refusal to invent one."""
    session = _FakeSession()

    policy.roll_fleet_budget_caps_forward(
        session,
        now=NOW_SEP,
        months_ahead=0,
        caps_usd={FLEET_PROVIDER_PROXY: 75.0, FLEET_PROVIDER_BROWSER: 25.0},
    )

    for row in session.added:
        assert row.limit_bytes is None
        assert row.limit_requests is None
        assert row.limit_browser_seconds is None
        assert row.currency == "USD"


# --- the seam with the one-off seeder -----------------------------------


def test_the_seeder_and_the_cadence_share_one_implementation() -> None:
    """Not "the same behaviour" — the same objects. Two implementations of
    "which rows get the cap" is how an operator's manual run and the
    cadence end up disagreeing about what is capped."""
    assert capseed.DEFAULT_SCOPE_KEYS is policy.DEFAULT_SCOPE_KEYS
    assert capseed.period_keys_from is policy.period_keys_from
    assert capseed.plan_budget_rows is policy.plan_budget_rows
    assert capseed.apply_budget_cap is policy.apply_budget_cap
    assert capseed.BudgetRowPlan is policy.BudgetRowPlan


def test_default_scopes_are_the_paid_transport_classes() -> None:
    assert policy.DEFAULT_SCOPE_KEYS == (FLEET_PROVIDER_PROXY, FLEET_PROVIDER_BROWSER)


def test_the_policy_never_names_the_per_workspace_budget_table() -> None:
    """A "fleet cap" written to `cost_budgets` would cap ONE tenant and
    leave the shared provider account wide open. Statically pinned here
    for the same reason it is pinned on the script: the mistake is a
    one-word edit away."""
    source = Path(policy.__file__).read_text(encoding="utf-8")
    assert re.search(rf"(?<!Fleet){CostBudget.__name__}", source) is None
