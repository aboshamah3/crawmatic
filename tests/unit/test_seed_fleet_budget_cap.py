"""Unit tests for `scripts/seed_fleet_budget_cap.py` (EPA go-live prep).

`fleet_cost_budgets` is the only place a fleet-wide money ceiling can
live — every authorization locks that row before the tenant's — and every
`limit_*` column on it is `NULL`, which the model reads as "no ceiling".
The 2026-08-12 extra.com discovery leak (~$325/mo of proxy egress from
one misconfigured `url_pattern`, PLAN_PROXY_COST_REDUCTION.md) is what
that costs when it goes wrong.

DB-independent throughout, and NOT by preference: `FleetCostBudget`
carries a `JSONB` `warned_thresholds` column that does not compile under
SQLite (confirmed while designing this test), so the substitute is a
hand-rolled `_FakeSession` recording `.add()` / `.execute()` — the same
choice, for the same reason, as `tests/unit/test_onboard_competitor.py`.
The tests that need real SQL evaluation live in
`tests/unit/test_seed_workspace_entitlements.py`, whose tables do compile.

The four claims worth pinning here:

1. **Only the money limit is written.** Bytes/requests/browser-seconds
   have no owner-agreed number; inventing one would deny work nobody
   budgeted for. The `reserved_*`/`settled_*` counters are the
   authorization path's and must never be reset — that would hand back
   money the fleet has already spent.
2. **Never a per-workspace row.** `cost_budgets` is a different table
   with a different owner; a "fleet cap" that landed there would cap one
   tenant and leave the shared provider account wide open.
3. **The cap does not carry itself forward.** A budget row is born with
   `NULL` limits (`_get_or_create_budget_locked`), so a cap set only for
   the current month evaporates at the month boundary — hence
   `--months-ahead`, and hence the warning line the report always prints.
4. **`--propose` never guesses silently.** Every number it prints carries
   the table or the document it came from.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.sql.functions import FunctionElement

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.seed_fleet_budget_cap as capseed  # noqa: E402
from app_shared.costauth.service import (  # noqa: E402
    FLEET_PROVIDER_BROWSER,
    FLEET_PROVIDER_DIRECT,
    FLEET_PROVIDER_PROXY,
)
from app_shared.models.cost_authorization import CostBudget, FleetCostBudget  # noqa: E402

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class _FakeResult:
    """Stands in for a `Result`: `.scalars()` / `.one()` over canned rows."""

    def __init__(self, rows: list[Any] | tuple[Any, ...]) -> None:
        self._rows = list(rows)

    def scalars(self) -> list[Any]:
        return self._rows

    def one(self) -> Any:
        return self._rows[0]

    def scalar_one(self) -> Any:
        return self._rows[0]


class _FakeSession:
    """Records `.add()`; answers `.execute(select(...))` from canned rows.

    Keyed by the selected entity (`FleetCostBudget`, ...) exactly as
    `tests/unit/test_onboard_competitor.py`'s fake does, with a separate
    `aggregates` slot for the `--propose` `func.sum(...)` selects, which
    have no single entity to key on.
    """

    def __init__(
        self,
        *,
        rows: dict[type, list[Any]] | None = None,
        aggregates: list[tuple] | None = None,
    ) -> None:
        self.rows = dict(rows or {})
        self.aggregates = list(aggregates or [])
        self.added: list[Any] = []
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def execute(self, statement: Any) -> Any:
        rendered = str(statement)
        self.statements.append(rendered)
        descriptions = getattr(statement, "column_descriptions", None)
        if not descriptions:
            return _FakeResult([])
        # An aggregate select still reports the ORM class it was built
        # from as its `entity`, so the entity alone cannot tell a
        # `select(FleetCostBudget)` from a `select(func.sum(...))` --
        # the column EXPRESSION can.
        if any(isinstance(d["expr"], FunctionElement) for d in descriptions):
            return _FakeResult([self.aggregates.pop(0)] if self.aggregates else [])
        return _FakeResult(self.rows.get(descriptions[0]["entity"], []))

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


def _existing_row(scope_key: str, period_key: str, limit: int | None) -> FleetCostBudget:
    """A `FleetCostBudget` built WITHOUT a session — plain ORM object."""
    return FleetCostBudget(
        scope_key=scope_key,
        period_key=period_key,
        currency="USD",
        limit_cost_micro_units=limit,
    )


# --- period arithmetic --------------------------------------------------


def test_period_keys_cover_this_month_and_the_months_ahead() -> None:
    assert capseed.period_keys_from(_NOW, months_ahead=0) == ("2026_08",)
    assert capseed.period_keys_from(_NOW, months_ahead=2) == (
        "2026_08",
        "2026_09",
        "2026_10",
    )


def test_period_keys_roll_over_the_year_boundary() -> None:
    """December -> January must advance the YEAR, not produce month 13.
    A cap that lands on `2026_13` is a cap on a period nothing will ever
    authorize against — i.e. no cap at all, silently."""
    december = datetime(2026, 12, 3, tzinfo=timezone.utc)
    assert capseed.period_keys_from(december, months_ahead=2) == (
        "2026_12",
        "2027_01",
        "2027_02",
    )


def test_negative_months_ahead_is_rejected() -> None:
    with pytest.raises(ValueError):
        capseed.period_keys_from(_NOW, months_ahead=-1)


# --- scope selection ----------------------------------------------------


def test_default_scopes_are_the_paid_transport_classes_only() -> None:
    """`direct` is the free rung and no call site in the repo authorizes
    against `FLEET_PROVIDER_DIRECT`, so capping it would be a ceiling on a
    counter that never moves."""
    assert capseed.DEFAULT_SCOPE_KEYS == (FLEET_PROVIDER_PROXY, FLEET_PROVIDER_BROWSER)
    assert FLEET_PROVIDER_DIRECT not in capseed.DEFAULT_SCOPE_KEYS


# --- planning -----------------------------------------------------------


def test_plan_marks_an_absent_row_as_an_insert() -> None:
    session = _FakeSession(rows={FleetCostBudget: []})

    plans = capseed.plan_budget_rows(
        session,
        scope_keys=("proxy",),
        period_keys=("2026_08",),
        monthly_cap_micro_units=18_000,
    )

    assert len(plans) == 1
    assert plans[0].row_exists is False
    assert plans[0].existing_limit_micro_units is None
    assert plans[0].is_change is True


def test_plan_marks_an_already_capped_row_as_no_change() -> None:
    """Idempotence, stated as a plan property: a second `--apply` with the
    same number must report zero changed rows, not rewrite them."""
    session = _FakeSession(
        rows={FleetCostBudget: [_existing_row("proxy", "2026_08", 18_000)]}
    )

    plans = capseed.plan_budget_rows(
        session,
        scope_keys=("proxy",),
        period_keys=("2026_08",),
        monthly_cap_micro_units=18_000,
    )

    assert plans[0].is_change is False


def test_plan_covers_every_scope_period_pair() -> None:
    session = _FakeSession(rows={FleetCostBudget: []})

    plans = capseed.plan_budget_rows(
        session,
        scope_keys=("proxy", "browser"),
        period_keys=("2026_08", "2026_09"),
        monthly_cap_micro_units=1,
    )

    assert {(p.scope_key, p.period_key) for p in plans} == {
        ("proxy", "2026_08"),
        ("proxy", "2026_09"),
        ("browser", "2026_08"),
        ("browser", "2026_09"),
    }


# --- applying -----------------------------------------------------------


def test_apply_inserts_a_capped_row_when_none_exists() -> None:
    """The row is INSERTed already carrying the cap, rather than left for
    `_get_or_create_budget_locked` to materialise uncapped on the month's
    first authorization — the ceiling has to be in place BEFORE the
    period's first spend, which is the whole point of `--months-ahead`."""
    session = _FakeSession(rows={FleetCostBudget: []})
    plans = capseed.plan_budget_rows(
        session,
        scope_keys=("proxy",),
        period_keys=("2026_09",),
        monthly_cap_micro_units=18_000,
    )

    changed = capseed.apply_budget_cap(
        session, plans=plans, currency="USD", monthly_cap_micro_units=18_000
    )

    assert changed == 1
    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, FleetCostBudget)
    assert row.scope_key == "proxy"
    assert row.period_key == "2026_09"
    assert row.limit_cost_micro_units == 18_000
    assert row.currency == "USD"


def test_apply_sets_only_the_money_limit_and_leaves_the_others_null() -> None:
    """Bytes/requests/browser-seconds have no owner-agreed number. A
    limit invented here would deny work nobody budgeted for while adding
    no protection the money cap does not already give."""
    session = _FakeSession(rows={FleetCostBudget: []})
    plans = capseed.plan_budget_rows(
        session,
        scope_keys=("proxy",),
        period_keys=("2026_08",),
        monthly_cap_micro_units=18_000,
    )

    capseed.apply_budget_cap(
        session, plans=plans, currency="USD", monthly_cap_micro_units=18_000
    )

    row = session.added[0]
    assert row.limit_bytes is None
    assert row.limit_requests is None
    assert row.limit_browser_seconds is None


def test_apply_updates_an_existing_row_without_touching_its_counters() -> None:
    """Resetting `reserved_*`/`settled_*` would hand back money the fleet
    has already spent — the counters belong to the authorization path."""
    existing = _existing_row("proxy", "2026_08", None)
    existing.reserved_cost_micro_units = 4_200
    existing.settled_cost_micro_units = 9_100
    session = _FakeSession(rows={FleetCostBudget: [existing]})
    plans = capseed.plan_budget_rows(
        session,
        scope_keys=("proxy",),
        period_keys=("2026_08",),
        monthly_cap_micro_units=18_000,
    )

    changed = capseed.apply_budget_cap(
        session, plans=plans, currency="USD", monthly_cap_micro_units=18_000
    )

    assert changed == 1
    assert session.added == [], "an existing row must be updated, never re-inserted"
    assert existing.limit_cost_micro_units == 18_000
    assert existing.reserved_cost_micro_units == 4_200
    assert existing.settled_cost_micro_units == 9_100


def test_apply_is_idempotent_when_the_cap_is_already_in_place() -> None:
    existing = _existing_row("proxy", "2026_08", 18_000)
    session = _FakeSession(rows={FleetCostBudget: [existing]})
    plans = capseed.plan_budget_rows(
        session,
        scope_keys=("proxy",),
        period_keys=("2026_08",),
        monthly_cap_micro_units=18_000,
    )

    changed = capseed.apply_budget_cap(
        session, plans=plans, currency="USD", monthly_cap_micro_units=18_000
    )

    assert changed == 0
    assert session.added == []


def test_the_script_never_names_the_per_workspace_budget_table() -> None:
    """A "fleet cap" written to `cost_budgets` would cap ONE tenant and
    leave the shared provider account uncapped — the exact failure the
    fleet row exists to prevent. Statically pinned because the mistake is
    a one-word edit away."""
    source = Path(capseed.__file__).read_text(encoding="utf-8")
    # `FleetCostBudget` legitimately contains the substring, so match the
    # tenant model's name only where it is NOT the fleet one's suffix.
    assert re.search(rf"(?<!Fleet){CostBudget.__name__}", source) is None


# --- proposing ----------------------------------------------------------


def test_propose_prefers_reconciled_settlements_over_estimates() -> None:
    """A settlement is a bill the provider agreed to; an estimate is a
    model of one. When both exist the bill wins."""
    first = datetime(2026, 8, 1, tzinfo=timezone.utc)
    last = datetime(2026, 8, 16, tzinfo=timezone.utc)
    session = _FakeSession(aggregates=[(3_000, first, last, "USD")])

    observation = capseed.observe_monthly_spend(session, now=_NOW)

    # 3000 micro-USD over 15 days -> 6000/month.
    assert observation.monthly_micro_units == 6_000
    assert "settlements" in observation.source
    assert "15.00 day(s)" in observation.derivation


def test_propose_falls_back_to_ledger_estimates_when_nothing_is_settled() -> None:
    first = datetime(2026, 8, 1, tzinfo=timezone.utc)
    last = datetime(2026, 8, 11, tzinfo=timezone.utc)
    session = _FakeSession(
        aggregates=[(0, None, None, None), (1_000, first, last, "USD")]
    )

    observation = capseed.observe_monthly_spend(session, now=_NOW)

    # 1000 micro-USD over 10 days -> 3000/month.
    assert observation.monthly_micro_units == 3_000
    assert "estimated_cost_micro_units" in observation.source


def test_propose_says_so_out_loud_when_no_database_spend_is_reachable() -> None:
    """"NEVER guess silently": with an empty ledger the proposal falls
    back to the repository's own measured evidence AND names it."""
    session = _FakeSession(aggregates=[(0, None, None, None), (0, None, None, None)])

    observation = capseed.observe_monthly_spend(session, now=_NOW)

    assert observation.monthly_micro_units == 60_000_000
    assert "no database spend reachable" in observation.source
    assert "no settled or estimated cost found in the database" in observation.derivation
    assert "HANDOVER_READINESS_CYCLE_2026-08-15.md" in observation.derivation


def test_a_sub_day_window_is_not_multiplied_into_an_absurd_month() -> None:
    """Ten minutes of traffic describes ten minutes of spend. Scaling it
    literally would propose a cap ~4300x too large — which is no cap."""
    first = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    last = datetime(2026, 8, 26, 12, 10, tzinfo=timezone.utc)
    session = _FakeSession(aggregates=[(100, first, last, "USD")])

    observation = capseed.observe_monthly_spend(session, now=_NOW)

    assert observation.monthly_micro_units == 3_000  # 100 * 30 / 1 day floor


def test_the_cap_is_three_times_observed_spend() -> None:
    observation = capseed.SpendObservation(
        monthly_micro_units=6_000, currency="USD", source="t", derivation="d"
    )
    assert capseed.CAP_MULTIPLIER == 3
    assert capseed.propose_cap(observation) == 18_000


def test_proposal_report_carries_the_number_the_formula_and_the_provenance() -> None:
    observation = capseed.SpendObservation(
        monthly_micro_units=6_000,
        currency="USD",
        source="repository evidence (no database spend reachable)",
        derivation="$2.00 per full catalogue refresh x 30 days",
    )

    report = capseed.format_proposal(observation, scope_keys=("proxy", "browser"))

    assert "RECOMMENDED CAP        : 18000 micro-USD (USD)" in report
    assert "3 x observed monthly spend" in report
    assert "$2.00 per full catalogue refresh" in report
    assert "aggregate worst case   : 36000" in report
    assert "--monthly-cap-usd 0.018000" in report


# --- the report ---------------------------------------------------------


def test_plan_report_always_warns_that_the_cap_does_not_carry_forward() -> None:
    """A budget row is born with NULL limits, so a cap set only for this
    month evaporates at the month boundary. The operator must be told the
    LAST period covered on every single run."""
    plans = [
        capseed.BudgetRowPlan(
            scope_key="proxy",
            period_key="2026_09",
            existing_limit_micro_units=None,
            new_limit_micro_units=18_000,
            row_exists=False,
        )
    ]

    report = capseed.format_plan(plans, apply=False, currency="USD", changed=1)

    assert "DRY-RUN" in report
    assert "NULL (no ceiling) -> 18000" in report
    assert "the last period capped here is 2026_09" in report


# --- CLI validation -----------------------------------------------------


def _args(**overrides: Any) -> SimpleNamespace:
    base = dict(
        propose=False,
        apply=False,
        monthly_cap_micro_units=None,
        currency="USD",
        scope_keys=None,
        months_ahead=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_pre_h4_cents_flag_is_parsed_and_always_rejected() -> None:
    """`--monthly-cap-minor-units` named CENTS. H4/B1 made the ledger
    micro-USD, so the same number re-run from an old runbook would have
    capped the fleet at one ten-thousandth of the intended ceiling — a
    silent outage. The flag still PARSES (an "unrecognized argument" is
    easy to paper over with a shrug) and is then refused in words that
    say what changed and what to use instead."""
    args = capseed.parse_args(["--apply", "--monthly-cap-minor-units", "7500"])
    error = capseed.validate_args(args)

    assert error is not None
    assert "--monthly-cap-minor-units was REMOVED" in error
    assert "micro-USD" in error
    assert "--monthly-cap-usd" in error
    # And it never leaks through as a cap under the new name.
    assert args.monthly_cap_micro_units is None


def test_dollars_and_micro_units_reach_the_same_ceiling() -> None:
    """The two live spellings are one ceiling: $75 and 75_000_000
    micro-USD are the same instruction, converted by the same
    `usd_to_units` the maintenance cadence applies to its settings."""
    from_usd = capseed.parse_args(["--apply", "--monthly-cap-usd", "75"])
    from_units = capseed.parse_args(["--apply", "--monthly-cap-micro-units", "75000000"])

    assert from_usd.monthly_cap_micro_units == from_units.monthly_cap_micro_units
    assert capseed.validate_args(from_usd) is None
    assert capseed.validate_args(from_units) is None


def test_propose_and_apply_are_mutually_exclusive() -> None:
    assert "mutually exclusive" in (
        capseed.validate_args(_args(propose=True, apply=True)) or ""
    )


def test_apply_without_a_cap_is_refused() -> None:
    assert "requires --monthly-cap-usd or --monthly-cap-micro-units" in (
        capseed.validate_args(_args(apply=True)) or ""
    )


def test_a_non_positive_cap_is_refused() -> None:
    """A zero money limit is not "no ceiling" (that is NULL) — it is a
    ceiling of nothing, which denies every paid request in the fleet."""
    assert capseed.validate_args(_args(apply=True, monthly_cap_micro_units=0)) is not None
    assert capseed.validate_args(_args(apply=True, monthly_cap_micro_units=-1)) is not None


def test_currency_must_be_iso4217_shaped() -> None:
    """The table's own CHECK is `currency ~ '^[A-Z]{3}$'`; failing here
    beats failing as an IntegrityError mid-transaction."""
    assert capseed.validate_args(_args(currency="usd")) is not None
    assert capseed.validate_args(_args(currency="DOLLAR")) is not None
    assert capseed.validate_args(_args(currency="SAR")) is None


def test_a_valid_dry_run_invocation_passes_validation() -> None:
    assert capseed.validate_args(_args(monthly_cap_micro_units=18_000)) is None


# --- run() wiring -------------------------------------------------------


def test_run_dry_run_issues_the_read_only_guard_first_and_rolls_back() -> None:
    session = _FakeSession(rows={FleetCostBudget: []})

    report = capseed.run(
        session_factory=lambda: session,
        now=_NOW,
        propose=False,
        apply=False,
        monthly_cap_micro_units=18_000,
        currency="USD",
        scope_keys=("proxy",),
        months_ahead=0,
    )

    assert "SET TRANSACTION READ ONLY" in session.statements[0]
    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.added == []
    assert "DRY-RUN" in report and "rows_changed=1" in report


def test_run_apply_commits_and_never_issues_the_guard() -> None:
    session = _FakeSession(rows={FleetCostBudget: []})

    capseed.run(
        session_factory=lambda: session,
        now=_NOW,
        propose=False,
        apply=True,
        monthly_cap_micro_units=18_000,
        currency="USD",
        scope_keys=("proxy",),
        months_ahead=0,
    )

    assert not any("READ ONLY" in stmt for stmt in session.statements)
    assert session.commits == 1
    assert len(session.added) == 1


def test_run_refuses_to_plan_without_a_cap() -> None:
    session = _FakeSession(rows={FleetCostBudget: []})

    with pytest.raises(ValueError, match="--monthly-cap-micro-units"):
        capseed.run(
            session_factory=lambda: session,
            now=_NOW,
            propose=False,
            apply=False,
            monthly_cap_micro_units=None,
            currency="USD",
            scope_keys=("proxy",),
            months_ahead=0,
        )
    assert session.closed == 1, "the session must be closed even on the error path"
