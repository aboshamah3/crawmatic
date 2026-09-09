"""The pickup transition: targets really become ``STARTED`` (EPA A5).

**The bug this suite exists to keep fixed.** Nothing in production ever
wrote ``ScrapeTargetStatus.STARTED``. Targets went ``PENDING`` ->
terminal, ``scrape_job_targets.started_at`` was always NULL, and the
``STARTED`` bucket was permanently empty — so every reader of "work is in
flight" was silently inert:

* ``reap_stale_targets`` (``apps/workers/app/workers/tasks_jobs.py``)
  only reaps ``STARTED`` rows aged past a ceiling. It had nothing to act
  on, ever — the 2026-09-03 review's finding that the reaper was a no-op.
* ``crawmatic_target_oldest_age_seconds{status="STARTED"}`` reported a
  healthy 0 by measuring nothing.
* The deploy-survival proof ("work was in flight when the process died")
  had no way to be true.

So these tests assert the *transition*, not just the helper: a target
that a spider has picked up is ``STARTED`` with ``started_at`` set, in
the same transaction as the load, and a second load changes nothing.

Two levels, deliberately:

1. **Against a real database** (in-memory SQLite, the
   ``tests/unit/test_seed_workspace_entitlements.py`` pattern) for
   ``app_shared.jobs.targets``. ``scrape_job_targets`` is plain
   ``Uuid``/``TIMESTAMPTZ``/``Text``/enum-as-``VARCHAR`` — SQLite renders
   it exactly, and SQLAlchemy compiles ``func.now()`` to
   ``CURRENT_TIMESTAMP`` there — so the ``status IN (...)`` predicate and
   the ``COALESCE(started_at, now())`` that make the transition
   idempotent are evaluated by an actual engine rather than asserted
   against a canned rowcount. SQLite does not enforce foreign keys unless
   ``PRAGMA foreign_keys=ON`` (not set), so the table's FKs to
   ``workspaces``/``scrape_jobs``/``domain_strategy_methods``/
   ``dispatch_intents`` need none of those tables to exist.
2. **Against a fake session** for ``scrape_core.targets.load_targets``
   (the ``tests/unit/test_load_targets_terminal_skip.py`` idiom), which
   is a bounded multi-query load nothing can realistically stand up
   in-process. What matters there is *that it happens, on the load's own
   session, before any fetch work* — which a recorder proves precisely.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app_shared.enums import ScrapeTargetStatus
from app_shared.jobs.targets import (
    PICKUP_ELIGIBLE_TARGET_STATUSES,
    mark_target,
    mark_targets_started,
    stamp_target_timestamps,
)
from app_shared.models.jobs import ScrapeJobTarget
from scrape_core import targets as targets_mod

_WORKSPACE_ID = uuid.uuid4()
_JOB_ID = uuid.uuid4()
_OLD_STARTED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


# --- real-engine fixtures ----------------------------------------------


@pytest.fixture()
def db_session():
    """In-memory SQLite holding `scrape_job_targets` alone.

    `StaticPool` so the statement-recording listener and the session
    share ONE connection (a fresh connection would get its own private
    empty in-memory database).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    ScrapeJobTarget.metadata.create_all(engine, tables=[ScrapeJobTarget.__table__])
    factory = sessionmaker(bind=engine)
    session = factory()
    session.info["engine"] = engine
    yield session
    session.close()
    engine.dispose()


class _StatementRecorder:
    """Records every SQL string the engine actually executes."""

    def __init__(self, engine: Any) -> None:
        self.statements: list[str] = []
        self._engine = engine

        def _listen(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            self.statements.append(statement)

        self._listen = _listen
        event.listen(engine, "before_cursor_execute", _listen)

    def stop(self) -> None:
        event.remove(self._engine, "before_cursor_execute", self._listen)

    @property
    def updates(self) -> list[str]:
        return [s for s in self.statements if s.lstrip().upper().startswith("UPDATE")]


def _add_target(
    session: Session,
    *,
    match_id: uuid.UUID,
    status: ScrapeTargetStatus,
    started_at: datetime | None = None,
    workspace_id: uuid.UUID = _WORKSPACE_ID,
    scrape_job_id: uuid.UUID = _JOB_ID,
    **columns: Any,
) -> uuid.UUID:
    session.add(
        ScrapeJobTarget(
            workspace_id=workspace_id,
            scrape_job_id=scrape_job_id,
            match_id=match_id,
            status=status,
            started_at=started_at,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            **columns,
        )
    )
    session.flush()
    return match_id


def _reload(session: Session, match_id: uuid.UUID) -> ScrapeJobTarget:
    """Re-read a target from the engine.

    `flush()` BEFORE `expire_all()` and not the other way round:
    `expire_all()` discards un-flushed changes on persistent instances,
    which would silently throw away exactly what a `mark_target` ORM-path
    test just asserted. The flush pushes them to the database first; the
    expire then forces a real SELECT so a Core `UPDATE` (which the
    identity map knows nothing about) is seen too.
    """
    session.flush()
    session.expire_all()
    return session.execute(
        select(ScrapeJobTarget).where(ScrapeJobTarget.match_id == match_id)
    ).scalar_one()


# --- the pickup transition itself --------------------------------------


def test_load_pickup_marks_every_non_terminal_target_started(db_session: Session) -> None:
    """PENDING and DEFERRED both become STARTED with `started_at` set.

    DEFERRED is included on purpose: it is the non-terminal overflow
    outcome handed back to dispatch (SPEC-11 US3) and re-pickup is
    exactly what it exists for — excluding it would leave every requeued
    target permanently invisible to the reaper.
    """
    pending = _add_target(db_session, match_id=uuid.uuid4(), status=ScrapeTargetStatus.PENDING)
    deferred = _add_target(db_session, match_id=uuid.uuid4(), status=ScrapeTargetStatus.DEFERRED)

    changed = mark_targets_started(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_ids=[pending, deferred],
    )

    assert changed == 2
    for match_id in (pending, deferred):
        row = _reload(db_session, match_id)
        assert row.status == ScrapeTargetStatus.STARTED
        assert row.started_at is not None


def test_pickup_is_one_statement_for_the_whole_batch(db_session: Session) -> None:
    """`load_targets` is a documented bounded load (Principle IV): the
    pickup must cost ONE `UPDATE` whatever the batch size, not one per
    match. A regression here would put a per-target query on the hottest
    path in the engine."""
    match_ids = [
        _add_target(db_session, match_id=uuid.uuid4(), status=ScrapeTargetStatus.PENDING)
        for _ in range(25)
    ]
    db_session.commit()

    recorder = _StatementRecorder(db_session.info["engine"])
    try:
        changed = mark_targets_started(
            db_session,
            workspace_id=_WORKSPACE_ID,
            scrape_job_id=_JOB_ID,
            match_ids=match_ids,
        )
    finally:
        recorder.stop()

    assert changed == 25
    assert len(recorder.updates) == 1
    statement = recorder.updates[0].lower()
    assert "coalesce" in statement
    assert "status in" in statement


def test_second_pickup_is_a_no_op(db_session: Session) -> None:
    """The acceptance criterion, stated as behaviour: load, then load
    again — nothing moves.

    Both halves are checked, because they fail differently: the second
    call must change ZERO rows (the `status IN ('PENDING','DEFERRED')`
    predicate no longer matches a STARTED row), and `started_at` must
    still hold its first value (the `COALESCE`, which is what protects
    the timestamp even if a row were somehow re-matched).
    """
    match_id = _add_target(db_session, match_id=uuid.uuid4(), status=ScrapeTargetStatus.PENDING)

    assert mark_targets_started(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_ids=[match_id],
    ) == 1
    first_started_at = _reload(db_session, match_id).started_at
    assert first_started_at is not None

    assert mark_targets_started(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_ids=[match_id],
    ) == 0

    row = _reload(db_session, match_id)
    assert row.status == ScrapeTargetStatus.STARTED
    assert row.started_at == first_started_at


def test_re_pickup_of_a_deferred_target_keeps_the_original_started_at(
    db_session: Session,
) -> None:
    """A DEFERRED target that already ran once is eligible again — and
    the re-pickup must NOT move `started_at`. This is the `COALESCE`
    isolated from the `status IN` predicate: the row genuinely matches,
    and the timestamp still does not move."""
    match_id = _add_target(
        db_session,
        match_id=uuid.uuid4(),
        status=ScrapeTargetStatus.DEFERRED,
        started_at=_OLD_STARTED_AT,
    )

    assert mark_targets_started(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_ids=[match_id],
    ) == 1

    row = _reload(db_session, match_id)
    assert row.status == ScrapeTargetStatus.STARTED
    assert row.started_at.replace(tzinfo=timezone.utc) == _OLD_STARTED_AT


@pytest.mark.parametrize(
    "terminal",
    [
        ScrapeTargetStatus.COMPLETED,
        ScrapeTargetStatus.FAILED,
        ScrapeTargetStatus.SKIPPED,
        ScrapeTargetStatus.CANCELLED,
    ],
)
def test_pickup_never_resurrects_a_terminal_target(
    db_session: Session, terminal: ScrapeTargetStatus
) -> None:
    """Terminal is terminal — the same invariant that lets `finalize_jobs`
    converge and that the 2026-08-22 duplicate-run incident turned on. A
    pickup must be no more able to re-open a finished target than
    `mark_target` is."""
    assert terminal not in PICKUP_ELIGIBLE_TARGET_STATUSES
    match_id = _add_target(db_session, match_id=uuid.uuid4(), status=terminal)

    changed = mark_targets_started(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_ids=[match_id],
    )

    assert changed == 0
    row = _reload(db_session, match_id)
    assert row.status == terminal
    assert row.started_at is None


def test_pickup_is_workspace_and_job_scoped(db_session: Session) -> None:
    """Same match_id, another workspace and another job — untouched.
    Every write in this codebase is workspace-scoped; a lifecycle
    transition is no exception."""
    match_id = uuid.uuid4()
    _add_target(db_session, match_id=match_id, status=ScrapeTargetStatus.PENDING)
    other_workspace = uuid.uuid4()
    _add_target(
        db_session,
        match_id=match_id,
        status=ScrapeTargetStatus.PENDING,
        workspace_id=other_workspace,
        scrape_job_id=uuid.uuid4(),
    )

    assert mark_targets_started(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_ids=[match_id],
    ) == 1

    db_session.expire_all()
    foreign = db_session.execute(
        select(ScrapeJobTarget).where(ScrapeJobTarget.workspace_id == other_workspace)
    ).scalar_one()
    assert foreign.status == ScrapeTargetStatus.PENDING
    assert foreign.started_at is None


# --- `mark_target(only_if_status=...)`, the single-row form -------------


def test_mark_target_only_if_status_issues_exactly_one_conditional_update(
    db_session: Session,
) -> None:
    """The criterion literally: ONE `UPDATE ... SET status='STARTED',
    started_at=COALESCE(started_at, now()) WHERE status IN (...)`, with
    no preceding SELECT — the row is never loaded, so two concurrent
    pickups cannot both believe they won."""
    match_id = _add_target(db_session, match_id=uuid.uuid4(), status=ScrapeTargetStatus.PENDING)
    db_session.commit()

    recorder = _StatementRecorder(db_session.info["engine"])
    try:
        mark_target(
            db_session,
            workspace_id=_WORKSPACE_ID,
            scrape_job_id=_JOB_ID,
            match_id=match_id,
            status=ScrapeTargetStatus.STARTED,
            only_if_status=PICKUP_ELIGIBLE_TARGET_STATUSES,
        )
    finally:
        recorder.stop()

    assert len(recorder.updates) == 1
    assert not [s for s in recorder.statements if s.lstrip().upper().startswith("SELECT")]
    statement = recorder.updates[0].lower()
    assert "started_at=coalesce" in statement.replace(" =", "=").replace("= ", "=")
    assert "status in" in statement

    row = _reload(db_session, match_id)
    assert row.status == ScrapeTargetStatus.STARTED
    assert row.started_at is not None


def test_mark_target_only_if_status_refuses_an_ineligible_row(db_session: Session) -> None:
    match_id = _add_target(db_session, match_id=uuid.uuid4(), status=ScrapeTargetStatus.COMPLETED)

    mark_target(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_id=match_id,
        status=ScrapeTargetStatus.STARTED,
        only_if_status=PICKUP_ELIGIBLE_TARGET_STATUSES,
    )

    row = _reload(db_session, match_id)
    assert row.status == ScrapeTargetStatus.COMPLETED
    assert row.started_at is None


def test_mark_target_without_only_if_status_keeps_the_load_then_mutate_path(
    db_session: Session,
) -> None:
    """Backward compatibility: every existing caller (the reaper,
    cancellation, the persistence pipeline) passes no `only_if_status`
    and must keep the read-then-mutate behaviour, terminal guard
    included."""
    match_id = _add_target(db_session, match_id=uuid.uuid4(), status=ScrapeTargetStatus.STARTED)

    mark_target(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_id=match_id,
        status=ScrapeTargetStatus.COMPLETED,
    )
    assert _reload(db_session, match_id).status == ScrapeTargetStatus.COMPLETED

    # ... and terminal stays terminal.
    mark_target(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_id=match_id,
        status=ScrapeTargetStatus.FAILED,
    )
    assert _reload(db_session, match_id).status == ScrapeTargetStatus.COMPLETED


# --- per-phase timestamps ----------------------------------------------


def test_mark_target_stamps_phase_timestamps_first_writer_wins(db_session: Session) -> None:
    first = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
    later = datetime(2026, 9, 7, 11, 0, tzinfo=timezone.utc)
    match_id = _add_target(db_session, match_id=uuid.uuid4(), status=ScrapeTargetStatus.STARTED)

    mark_target(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_id=match_id,
        status=ScrapeTargetStatus.STARTED,
        first_network_at=first,
        document_received_at=first,
    )
    # A second, LATER report of the same boundary must not move it: the
    # boundary happened once, and a retried attempt reporting it again is
    # not new information about when it happened.
    mark_target(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_id=match_id,
        status=ScrapeTargetStatus.COMPLETED,
        first_network_at=later,
        persisted_at=later,
    )

    row = _reload(db_session, match_id)
    assert row.first_network_at.replace(tzinfo=timezone.utc) == first
    assert row.document_received_at.replace(tzinfo=timezone.utc) == first
    assert row.persisted_at.replace(tzinfo=timezone.utc) == later
    # An unsupplied phase is left alone, never nulled and never invented.
    assert row.extraction_finished_at is None
    assert row.claimed_at is None


def test_stamp_target_timestamps_records_phases_without_a_transition(
    db_session: Session,
) -> None:
    """The intermediate-attempt path in `pipelines._flush_batch`: a link
    in a strategy chain fetched a document and spent money, but owns no
    status transition. Its boundaries are still facts."""
    moment = datetime(2026, 9, 7, 9, 30, tzinfo=timezone.utc)
    match_id = _add_target(db_session, match_id=uuid.uuid4(), status=ScrapeTargetStatus.STARTED)

    changed = stamp_target_timestamps(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_id=match_id,
        first_network_at=moment,
        persisted_at=moment,
    )

    assert changed == 1
    row = _reload(db_session, match_id)
    assert row.status == ScrapeTargetStatus.STARTED
    assert row.first_network_at.replace(tzinfo=timezone.utc) == moment
    assert row.persisted_at.replace(tzinfo=timezone.utc) == moment


def test_stamp_target_timestamps_with_no_phases_issues_no_statement(
    db_session: Session,
) -> None:
    match_id = _add_target(db_session, match_id=uuid.uuid4(), status=ScrapeTargetStatus.STARTED)
    db_session.commit()

    recorder = _StatementRecorder(db_session.info["engine"])
    try:
        changed = stamp_target_timestamps(
            db_session,
            workspace_id=_WORKSPACE_ID,
            scrape_job_id=_JOB_ID,
            match_id=match_id,
        )
    finally:
        recorder.stop()

    assert changed == 0
    assert recorder.updates == []


def test_phase_timestamp_columns_all_exist_on_the_model() -> None:
    """The migration (`b6f1c40a97d2`) and the model must agree with the
    single writer's own column list — a typo in any one of the three
    would fail at runtime, on the persistence path, in production."""
    from app_shared.jobs.targets import PHASE_TIMESTAMP_COLUMNS

    columns = set(ScrapeJobTarget.__table__.columns.keys())
    assert set(PHASE_TIMESTAMP_COLUMNS) <= columns


# --- `load_targets` does the pickup, on its own session -----------------


class _FakeMatch:
    """The one attribute `load_targets` reads before the pickup."""

    def __init__(self) -> None:
        self.id = uuid.uuid4()


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Returns no terminal match_ids for the `scrape_job_targets` probe
    and one match for the matches query (the
    `test_load_targets_terminal_skip.py` idiom)."""

    def __init__(self, match: _FakeMatch) -> None:
        self.executed: list[Any] = []
        self._match = match

    def execute(self, stmt: Any) -> _FakeResult:
        self.executed.append(stmt)
        compiled = str(
            stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        if "scrape_job_targets" in compiled:
            return _FakeResult([])
        return _FakeResult([self._match])

    def get(self, *args: Any, **kwargs: Any) -> None:
        return None


class _FakeWorkspaceTxn:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self, workspace_id: Any) -> "_FakeWorkspaceTxn":
        return self

    def __enter__(self) -> _FakeSession:
        return self._session

    def __exit__(self, *exc_info: Any) -> bool:
        return False


class _FakeSettings:
    STRATEGY_PROFILE_SCOPE = "domain"


class _StopAfterPickup(Exception):
    """Sentinel: the pickup happened, so the rest of the bounded load
    (Redis resolution chains, strategy get-or-create, provider decryption)
    need not be faked to observe it."""


def test_load_targets_performs_the_pickup_on_the_loads_own_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`load_targets` marks the resolved matches STARTED using the SAME
    session it is already holding open — i.e. inside the same
    `workspace_txn` transaction as the terminal-status filter, so a
    target can never be observed half-picked-up."""
    match = _FakeMatch()
    session = _FakeSession(match)
    captured: dict[str, Any] = {}

    def _recorder(passed_session: Any, **kwargs: Any) -> int:
        captured["session"] = passed_session
        captured["kwargs"] = kwargs
        raise _StopAfterPickup

    monkeypatch.setattr("app_shared.config.get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(targets_mod, "workspace_txn", _FakeWorkspaceTxn(session))
    monkeypatch.setattr(targets_mod, "mark_targets_started", _recorder)

    with pytest.raises(_StopAfterPickup):
        targets_mod.load_targets(_WORKSPACE_ID, [match.id], scrape_job_id=_JOB_ID)

    assert captured["session"] is session
    assert captured["kwargs"]["workspace_id"] == _WORKSPACE_ID
    assert captured["kwargs"]["scrape_job_id"] == _JOB_ID
    assert captured["kwargs"]["match_ids"] == [match.id]
    # The pickup happens BEFORE any resolution/fetch work: only the
    # terminal-status probe and the matches query have run.
    assert len(session.executed) == 2


def test_load_targets_without_a_job_id_performs_no_pickup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target-less caller (no `scrape_job_id`) has no
    `scrape_job_targets` rows to transition; it must not try."""
    match = _FakeMatch()
    session = _FakeSession(match)
    calls: list[Any] = []

    monkeypatch.setattr("app_shared.config.get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(targets_mod, "workspace_txn", _FakeWorkspaceTxn(session))
    monkeypatch.setattr(
        targets_mod,
        "mark_targets_started",
        lambda *a, **k: calls.append(k) or 0,
    )

    with pytest.raises(Exception):  # noqa: B017 - the un-faked load continues and fails
        targets_mod.load_targets(_WORKSPACE_ID, [match.id])

    assert calls == []


def test_scrape_job_targets_table_has_the_new_phase_columns() -> None:
    """Guards the SQLite fixture above from silently drifting: if the
    model lost a column the fixture would still build a table and every
    behavioural test would pass against the wrong schema."""
    columns = ScrapeJobTarget.__table__.columns.keys()
    for name in (
        "claimed_at",
        "remote_accepted_at",
        "first_network_at",
        "document_received_at",
        "extraction_finished_at",
        "persisted_at",
    ):
        assert name in columns


def test_started_transition_is_reachable_from_production_code() -> None:
    """The whole point of A5: a PRODUCTION path writes STARTED. An AST-
    free but deliberate source check — if `load_targets` ever loses the
    call, every reader of "in flight" goes silently inert again, which is
    exactly the failure this task fixed and exactly the kind of
    regression a green suite would otherwise hide."""
    import inspect

    source = inspect.getsource(targets_mod.load_targets)
    assert "mark_targets_started(" in source


def test_sqlite_fixture_really_persisted_a_row(db_session: Session) -> None:
    """Sanity: the fixture is a real engine, not a silent no-op."""
    _add_target(db_session, match_id=uuid.uuid4(), status=ScrapeTargetStatus.PENDING)
    assert db_session.execute(text("SELECT COUNT(*) FROM scrape_job_targets")).scalar_one() == 1
