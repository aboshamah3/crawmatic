"""Durable maintenance cadences (2026-08-15 readiness cycle).

The regression these tests exist for: `apps/scheduler` drove
`partition_create` / `daily_rollup` / `retention_drop` off in-process
float accumulators that reset to ``0.0`` on every process start, against
an interval of ``86400``. On Railway — where every deploy, OOM and host
migration restarts the container — that countdown could be reset forever,
and in production it was: no ``2026_09`` partition existed on any of the
four partitioned tables (a dated total write outage) and
``variant_price_daily_rollups`` had never received a single row.

``test_cadence_survives_a_process_restart`` is the test that would have
caught it: it restarts the "process" repeatedly inside the interval and
asserts the task still fires on schedule. Against the old accumulator
design it is unsatisfiable by construction.

No DB — the claim is one ``UPDATE ... WHERE next_due_at <= :now
RETURNING id`` statement, so a small fake session that interprets exactly
those two statements models the contract (including the row-serialised
one-winner semantics real Postgres provides) without a live Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app_shared.maintenance.cadence import (
    EVENT_CADENCE_OVERDUE,
    CadenceStatus,
    claim_cadence,
    claimant_id,
    ensure_cadence_rows,
    log_overdue_cadences,
)
from app_shared.models.maintenance_cadence import (
    CADENCE_DAILY_ROLLUP,
    CADENCE_PARTITION_CREATE,
    CADENCE_RETENTION_DROP,
    DURABLE_CADENCE_KEYS,
    EPOCH_DUE,
)

UTC = timezone.utc
DAY = 86400


# ---------------------------------------------------------------------
# Fake durable store
# ---------------------------------------------------------------------


@dataclass
class _Row:
    cadence_key: str
    next_due_at: datetime
    last_run_at: datetime | None = None
    last_claimed_by: str | None = None
    run_count: int = 0


@dataclass
class _FakeDB:
    """The shared, *durable* state. Survives `_FakeSession` objects the
    way a database survives processes — which is the whole point."""

    rows: dict[str, _Row] = field(default_factory=dict)


class _FakeSession:
    """Interprets only the two statements `cadence.py` issues.

    The UPDATE is applied to the shared `_FakeDB` immediately, modelling
    Postgres' row-level lock: a concurrent claimant's identical statement
    re-evaluates its predicate against the already-advanced deadline and
    matches zero rows.
    """

    def __init__(self, db: _FakeDB) -> None:
        self.db = db
        self.commits = 0
        self.rollbacks = 0

    def execute(self, stmt):  # noqa: ANN001 - TextClause
        sql = str(stmt)
        params = stmt.compile().params
        if "INSERT INTO maintenance_cadences" in sql:
            key = params["cadence_key"]
            if key not in self.db.rows:  # ON CONFLICT DO NOTHING
                self.db.rows[key] = _Row(cadence_key=key, next_due_at=params["epoch"])
            return _FakeResult(None)
        if "UPDATE maintenance_cadences" in sql:
            key = params["cadence_key"]
            row = self.db.rows.get(key)
            if row is None or row.next_due_at > params["now"]:
                return _FakeResult(None)
            row.next_due_at = params["next_due_at"]
            row.last_run_at = params["now"]
            row.last_claimed_by = params["claimed_by"]
            row.run_count += 1
            return _FakeResult((f"id-{key}",))
        raise AssertionError(f"unexpected statement: {sql}")

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _FakeResult:
    def __init__(self, row) -> None:  # noqa: ANN001
        self._row = row

    def first(self):  # noqa: ANN201
        return self._row


def _boot(db: _FakeDB) -> _FakeSession:
    """Simulate a scheduler process starting: brand-new session, no
    in-process state carried over, self-healing seed."""
    session = _FakeSession(db)
    ensure_cadence_rows(session)
    session.commit()
    return session


# ---------------------------------------------------------------------
# Core regression
# ---------------------------------------------------------------------


def test_cadence_survives_a_process_restart() -> None:
    """THE regression. Restarting every hour inside a 24h interval must
    not reset the countdown — the task still fires once, ~24h after the
    previous run, exactly as if nothing had restarted.

    Against the replaced design (a float re-initialised to 0.0 in
    `main()`), the 24th assertion below can never pass: the accumulator
    would be back at ~3600s at every restart and never reach 86400.
    """
    db = _FakeDB()
    start = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    # First boot: never run -> due immediately.
    session = _boot(db)
    assert claim_cadence(session, CADENCE_PARTITION_CREATE, interval_seconds=DAY, now=start)
    session.commit()

    # 23 hourly restarts inside the interval: each boots fresh, each must
    # decline (the deadline is in the database, not in the process).
    for hour in range(1, 24):
        now = start + timedelta(hours=hour)
        restarted = _boot(db)
        assert not claim_cadence(
            restarted, CADENCE_PARTITION_CREATE, interval_seconds=DAY, now=now
        ), f"claimed too early at +{hour}h"

    # 24h after the last run, a freshly restarted process fires it.
    now = start + timedelta(hours=24)
    restarted = _boot(db)
    assert claim_cadence(restarted, CADENCE_PARTITION_CREATE, interval_seconds=DAY, now=now)
    assert db.rows[CADENCE_PARTITION_CREATE].run_count == 2


def test_never_run_cadence_is_due_on_first_boot() -> None:
    """A brand-new cadence row is born at the epoch, so the very first
    boot runs the task instead of waiting a full interval."""
    db = _FakeDB()
    session = _boot(db)

    assert db.rows[CADENCE_PARTITION_CREATE].next_due_at == EPOCH_DUE
    assert db.rows[CADENCE_PARTITION_CREATE].last_run_at is None
    assert claim_cadence(
        session,
        CADENCE_PARTITION_CREATE,
        interval_seconds=DAY,
        now=datetime(2026, 8, 15, 0, 0, tzinfo=UTC),
    )


def test_overdue_cadence_runs_immediately_not_after_another_interval() -> None:
    """A cadence three weeks past its deadline fires on the very next
    poll — and fires ONCE, not 21 times catching up."""
    db = _FakeDB()
    session = _boot(db)
    t0 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    assert claim_cadence(session, CADENCE_DAILY_ROLLUP, interval_seconds=DAY, now=t0)

    late = t0 + timedelta(days=22)
    assert claim_cadence(session, CADENCE_DAILY_ROLLUP, interval_seconds=DAY, now=late)
    # Immediately re-polling does NOT fire again: the deadline was
    # recomputed from `now`, not chained off the stale one.
    assert not claim_cadence(session, CADENCE_DAILY_ROLLUP, interval_seconds=DAY, now=late)
    assert db.rows[CADENCE_DAILY_ROLLUP].next_due_at == late + timedelta(seconds=DAY)


def test_concurrent_schedulers_claim_at_most_once() -> None:
    """N replicas polling the same instant: exactly one wins."""
    db = _FakeDB()
    replicas = [_boot(db) for _ in range(5)]
    now = datetime(2026, 8, 15, 6, 0, tzinfo=UTC)

    wins = [
        claim_cadence(s, CADENCE_RETENTION_DROP, interval_seconds=DAY, now=now)
        for s in replicas
    ]

    assert sum(wins) == 1, wins
    assert db.rows[CADENCE_RETENTION_DROP].run_count == 1


def test_restart_loop_does_not_thundering_herd() -> None:
    """Fifty crash-restarts in the same second enqueue the task once."""
    db = _FakeDB()
    now = datetime(2026, 8, 15, 6, 0, tzinfo=UTC)

    wins = 0
    for _ in range(50):
        session = _boot(db)
        if claim_cadence(session, CADENCE_PARTITION_CREATE, interval_seconds=DAY, now=now):
            wins += 1
            session.commit()

    assert wins == 1


def test_ensure_cadence_rows_is_idempotent_and_covers_every_durable_cadence() -> None:
    db = _FakeDB()
    _boot(db)
    _boot(db)
    _boot(db)

    assert set(db.rows) == set(DURABLE_CADENCE_KEYS)
    assert all(row.run_count == 0 for row in db.rows.values())


def test_claim_stamps_diagnostics() -> None:
    db = _FakeDB()
    session = _boot(db)
    now = datetime(2026, 8, 15, 6, 0, tzinfo=UTC)

    claim_cadence(session, CADENCE_DAILY_ROLLUP, interval_seconds=DAY, now=now, claimed_by="rep-1")

    row = db.rows[CADENCE_DAILY_ROLLUP]
    assert row.last_run_at == now
    assert row.last_claimed_by == "rep-1"
    assert "/" in claimant_id()


# ---------------------------------------------------------------------
# Overdue assertion
# ---------------------------------------------------------------------


def test_overdue_cadence_logs_error_event(caplog) -> None:  # noqa: ANN001 - pytest fixture
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    statuses = [
        CadenceStatus(
            cadence_key=CADENCE_PARTITION_CREATE,
            next_due_at=now - timedelta(days=30),
            last_run_at=None,
            run_count=0,
        ),
        CadenceStatus(
            cadence_key=CADENCE_DAILY_ROLLUP,
            next_due_at=now + timedelta(hours=5),
            last_run_at=now - timedelta(hours=19),
            run_count=12,
        ),
    ]

    with caplog.at_level("ERROR"):
        overdue = log_overdue_cadences(statuses, now=now, grace_seconds=7200)

    assert overdue == [CADENCE_PARTITION_CREATE]
    assert EVENT_CADENCE_OVERDUE in caplog.text
    assert "last_run_at=never" in caplog.text
    assert CADENCE_DAILY_ROLLUP not in caplog.text
