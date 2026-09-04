"""``roll_fleet_budget_caps_forward`` across a real month boundary (EPA G4, B3 proof).

The plan's claim is about a **database column surviving a month
boundary**, not about Python: "call the roll-forward on 2026-10-28 and
``fleet_cost_budgets`` grows ``2026_11`` rows with the ``2026_10`` cap
copied onto them, with no operator running the seeder" is a statement
about what ends up committed in Postgres. A fake ``Session`` (as
``tests/unit/test_fleet_budget_rollforward_task.py`` uses, to prove the
*task* wires the *policy* correctly) cannot make that claim — only a real
table can. This suite is the missing half: the policy function itself,
against a real ``fleet_cost_budgets``, migrated to the real alembic head.

Fixture wiring mirrors ``test_cost_authorization.py`` /
``test_fair_scheduling.py``: its own throwaway ``postgres:18-alpine``
container on a dedicated port, migrated with ``alembic -x db_url=``
(never touching the developer's ``DATABASE_URL``/``AUTH_DATABASE_URL``),
removed on teardown. It SKIPS cleanly when Docker is unavailable.

Seeding goes through the real A4 helpers (:func:`plan_budget_rows` +
:func:`apply_budget_cap`) rather than raw ``INSERT``s, so the seeded
``2026_10`` rows are indistinguishable from ones an operator's seeder run
would have produced -- the roll-forward under test cannot tell the
difference, and neither should this test.

The scenario is deliberately the CARRY-forward path (case 3 of the
policy's three-way decision, see ``fleet_budget_policy`` module
docstring): ``caps_usd`` is ``{PROXY: None, BROWSER: None}``, i.e. no
settings are configured at all for 2026-10-28. That is the exact failure
mode the seeder's own docstring names -- "until a real budget-policy
writer exists, re-running this script is a recurring operator task" --
and it is the strongest form of the B3 proof: even with nothing
configured, the last cap an operator chose survives the boundary.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app_shared.costauth.fleet_budget_policy import (
    USD_TO_UNITS,
    apply_budget_cap,
    plan_budget_rows,
    roll_fleet_budget_caps_forward,
    usd_to_units,
)
from app_shared.costauth.service import FLEET_PROVIDER_BROWSER, FLEET_PROVIDER_PROXY
from app_shared.models.cost_authorization import FleetCostBudget

REPO_ROOT = Path(__file__).resolve().parents[2]

# A port and a container name nothing else in this repository uses, so a
# stale container from another suite can never be mistaken for this one's
# (and so this suite can never delete another's).
_PG_IMAGE = "postgres:18-alpine"
_PG_CONTAINER = "cm-g4-fleetbudget-pg"
_PG_PORT = 55499
_PG_PASSWORD = "fleetbudget-scratch"  # noqa: S105 - throwaway container, never a real secret
_PG_DB = "crawmatic_fleetbudget"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode
            == 0
        )
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _docker_available(),
        reason="No Docker daemon — this suite provisions its own scratch Postgres",
    ),
]


def _dsn() -> str:
    return f"postgresql+psycopg://postgres:{_PG_PASSWORD}@127.0.0.1:{_PG_PORT}/{_PG_DB}"


@pytest.fixture(scope="module")
def engine():
    """A scratch Postgres migrated to alembic head. Removed on teardown."""
    subprocess.run(["docker", "rm", "-f", _PG_CONTAINER], capture_output=True)
    up = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", _PG_CONTAINER,
            "-e", f"POSTGRES_PASSWORD={_PG_PASSWORD}",
            "-e", f"POSTGRES_DB={_PG_DB}",
            "-p", f"127.0.0.1:{_PG_PORT}:5432",
            _PG_IMAGE,
        ],
        capture_output=True,
        text=True,
    )
    if up.returncode != 0:
        pytest.skip(f"could not start {_PG_IMAGE}: {up.stderr.strip()[:200]}")

    try:
        eng = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                eng = create_engine(_dsn(), pool_size=10, max_overflow=10)
                with eng.connect() as conn:
                    conn.execute(text("SELECT 1"))
                break
            except Exception:
                if eng is not None:
                    eng.dispose()
                eng = None
                time.sleep(1)
        if eng is None:
            pytest.skip("scratch Postgres never became reachable")

        # `-x db_url=` (alembic/env.py::_resolve_db_url) — the sanctioned
        # one-off override. No environment variable is written, so the
        # developer's own DSNs are untouched by this suite.
        migrate = subprocess.run(
            ["uv", "run", "alembic", "-x", f"db_url={_dsn()}", "upgrade", "head"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert migrate.returncode == 0, migrate.stderr[-4000:]

        yield eng
        eng.dispose()
    finally:
        subprocess.run(["docker", "rm", "-f", _PG_CONTAINER], capture_output=True)


@pytest.fixture()
def sessions(engine):
    """A ``sessionmaker`` + a fresh, empty ``fleet_cost_budgets`` for each test."""
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE fleet_cost_budgets RESTART IDENTITY CASCADE"))
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_capped_row(
    session: Session, *, scope_key: str, period_key: str, cap_usd: float
) -> int:
    """Cap one ``(scope_key, period_key)`` row via the real A4 helpers.

    Returns the cap in micro-USD, so the caller has the exact number to
    assert against rather than recomputing it.
    """
    cap_micro = usd_to_units(cap_usd)
    plans = plan_budget_rows(
        session,
        scope_keys=(scope_key,),
        period_keys=(period_key,),
        monthly_cap_micro_units=cap_micro,
    )
    apply_budget_cap(
        session, plans=plans, currency="USD", monthly_cap_micro_units=cap_micro
    )
    return cap_micro


def _rows_for(session: Session, period_keys: Iterator[str]) -> dict[tuple[str, str], int]:
    return {
        (row.scope_key, row.period_key): row.limit_cost_micro_units
        for row in session.execute(
            select(FleetCostBudget).where(FleetCostBudget.period_key.in_(list(period_keys)))
        ).scalars()
    }


def test_caps_roll_forward_across_the_month_boundary_and_are_idempotent(sessions) -> None:
    """October's operator-set caps survive into November with no operator,
    and a second run of the same cadence tick changes nothing."""
    with sessions() as session:
        proxy_cap_micro = _seed_capped_row(
            session, scope_key=FLEET_PROVIDER_PROXY, period_key="2026_10", cap_usd=75.0
        )
        browser_cap_micro = _seed_capped_row(
            session, scope_key=FLEET_PROVIDER_BROWSER, period_key="2026_10", cap_usd=25.0
        )
        session.commit()

    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    # Nothing configured for October 28th — the exact gap B3 closes: the
    # seeder's fixed run ended at 2026_10 and no operator re-ran it.
    caps_usd = {FLEET_PROVIDER_PROXY: None, FLEET_PROVIDER_BROWSER: None}

    with sessions() as session:
        report = roll_fleet_budget_caps_forward(
            session, now=now, months_ahead=1, caps_usd=caps_usd
        )
        session.commit()

    assert report.written == 2, report
    assert sorted(report.carried) == [
        (FLEET_PROVIDER_BROWSER, "2026_11"),
        (FLEET_PROVIDER_PROXY, "2026_11"),
    ], report
    assert report.uncapped == [], report

    with sessions() as session:
        rows = _rows_for(session, ["2026_10", "2026_11"])

    print("\nscope    | period  | cap_micro_usd |  cap_usd")
    for (scope, period), micro in sorted(rows.items()):
        print(f"{scope:<8} | {period} | {micro:>13} | {micro / USD_TO_UNITS:>8.2f}")

    assert rows[(FLEET_PROVIDER_PROXY, "2026_10")] == proxy_cap_micro
    assert rows[(FLEET_PROVIDER_BROWSER, "2026_10")] == browser_cap_micro
    assert rows[(FLEET_PROVIDER_PROXY, "2026_11")] == proxy_cap_micro, (
        "the November proxy cap must be the SAME number copied from October, "
        "not invented or left uncapped"
    )
    assert rows[(FLEET_PROVIDER_BROWSER, "2026_11")] == browser_cap_micro

    # Idempotent: a second call against the same `now` finds every targeted
    # row already capped (case 1 of the three-way decision) and writes
    # nothing — no duplicate rows, no changed amounts.
    with sessions() as session:
        second_report = roll_fleet_budget_caps_forward(
            session, now=now, months_ahead=1, caps_usd=caps_usd
        )
        session.commit()

    assert second_report.written == 0, second_report
    assert second_report.carried == [], second_report
    assert second_report.uncapped == [], second_report

    with sessions() as session:
        row_counts = session.execute(
            text(
                "SELECT scope_key, period_key, COUNT(*) AS n FROM fleet_cost_budgets "
                "WHERE period_key IN ('2026_10', '2026_11') "
                "GROUP BY scope_key, period_key"
            )
        ).all()
        unchanged_rows = _rows_for(session, ["2026_10", "2026_11"])

    assert len(row_counts) == 4, row_counts  # 2 scopes x 2 periods, no duplicates
    assert all(n == 1 for _, _, n in row_counts), row_counts
    assert unchanged_rows == rows, (unchanged_rows, rows)
