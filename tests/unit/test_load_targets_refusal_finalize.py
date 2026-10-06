"""A target `load_targets` refuses must leave the run terminal (2026-10-05).

**The bug this suite keeps fixed.** `load_targets` claims the whole batch
first (PENDING/DEFERRED -> STARTED, EPA A5) and only afterwards asks the
attempt ladder about each target. A target the ladder refuses
(`ATTEMPT_BUDGET_EXHAUSTED` / `TARGET_DEADLINE_EXCEEDED`) was then written
with ``only_if_status=(PENDING, DEFERRED)`` -- which the claim a few
statements earlier had just made false. The write matched nothing, the
target was dropped from the run, and it stayed STARTED. The run ended in
2-3 s, the ended-run reaper reverted the row to PENDING, the dispatcher
re-sent the batch, and the cycle repeated until the 12 h job deadline
failed it ``JOB_DEADLINE_EXCEEDED``: 328 targets (noon 314) on the night
of 2026-10-03, 109 empty spider runs, never a fetch.

These tests run the production order -- claim, then refuse -- against a
real (SQLite) engine, so the ``status IN (...)`` guard is evaluated by a
database rather than asserted against a mock.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app_shared.enums import ScrapeErrorCode, ScrapeTargetStatus
from app_shared.jobs.targets import (
    REFUSAL_FINALIZABLE_TARGET_STATUSES,
    claim_targets_started,
)
from app_shared.models.jobs import ScrapeJobTarget
from scrape_core import targets as targets_mod

_WORKSPACE_ID = uuid.uuid4()
_JOB_ID = uuid.uuid4()


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    ScrapeJobTarget.metadata.create_all(engine, tables=[ScrapeJobTarget.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _add(session: Session, status: ScrapeTargetStatus) -> uuid.UUID:
    match_id = uuid.uuid4()
    session.add(
        ScrapeJobTarget(
            workspace_id=_WORKSPACE_ID,
            scrape_job_id=_JOB_ID,
            match_id=match_id,
            status=status,
            created_at=datetime.now(timezone.utc) - timedelta(hours=8),
        )
    )
    session.flush()
    return match_id


def _row(session: Session, match_id: uuid.UUID) -> ScrapeJobTarget:
    session.flush()
    session.expire_all()
    return session.execute(
        select(ScrapeJobTarget).where(ScrapeJobTarget.match_id == match_id)
    ).scalar_one()


def test_a_target_refused_after_the_claim_is_finalized_with_the_refusal_code(
    db_session: Session,
) -> None:
    refused = _add(db_session, ScrapeTargetStatus.PENDING)
    kept = _add(db_session, ScrapeTargetStatus.PENDING)

    # Production order: the pickup claims the batch first...
    claimed = claim_targets_started(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_ids=[refused, kept],
    )
    assert claimed == {refused, kept}
    # ...then the ladder refuses one of them.
    targets_mod._finalize_refused_targets(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        refusals_by_match={refused: ScrapeErrorCode.ATTEMPT_BUDGET_EXHAUSTED},
        claimed_match_ids=claimed,
    )

    row = _row(db_session, refused)
    assert row.status == ScrapeTargetStatus.FAILED
    assert row.error_code == ScrapeErrorCode.ATTEMPT_BUDGET_EXHAUSTED
    assert row.completed_at is not None
    # The claimed-but-not-refused target is untouched: still this run's.
    assert _row(db_session, kept).status == ScrapeTargetStatus.STARTED


@pytest.mark.parametrize(
    "status", [ScrapeTargetStatus.PENDING, ScrapeTargetStatus.DEFERRED]
)
def test_a_claimed_pending_or_deferred_target_is_finalized(
    db_session: Session, status: ScrapeTargetStatus
) -> None:
    match_id = _add(db_session, status)
    claimed = claim_targets_started(
        db_session, workspace_id=_WORKSPACE_ID, scrape_job_id=_JOB_ID, match_ids=[match_id]
    )

    targets_mod._finalize_refused_targets(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        refusals_by_match={match_id: ScrapeErrorCode.TARGET_DEADLINE_EXCEEDED},
        claimed_match_ids=claimed,
    )

    row = _row(db_session, match_id)
    assert row.status == ScrapeTargetStatus.FAILED
    assert row.error_code == ScrapeErrorCode.TARGET_DEADLINE_EXCEEDED


def test_a_refused_target_another_load_holds_is_left_to_that_load(
    db_session: Session,
) -> None:
    """2026-10-06 (A9): only rows THIS load claimed are finalized.

    A duplicate spider run (a re-dispatch racing a live run) loads a
    target the live run already holds STARTED; its claim matches nothing
    for that row. Its ladder may still refuse it -- e.g. it reads the
    budget the live run has just charged -- and the old guard (every
    non-terminal status) then failed a target that was mid-fetch."""
    held = _add(db_session, ScrapeTargetStatus.STARTED)
    claimed = claim_targets_started(
        db_session, workspace_id=_WORKSPACE_ID, scrape_job_id=_JOB_ID, match_ids=[held]
    )
    assert claimed == set()

    targets_mod._finalize_refused_targets(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        refusals_by_match={held: ScrapeErrorCode.ATTEMPT_BUDGET_EXHAUSTED},
        claimed_match_ids=claimed,
    )

    row = _row(db_session, held)
    assert row.status == ScrapeTargetStatus.STARTED
    assert row.error_code is None


def test_claim_returns_exactly_the_rows_it_moved(db_session: Session) -> None:
    pending = _add(db_session, ScrapeTargetStatus.PENDING)
    deferred = _add(db_session, ScrapeTargetStatus.DEFERRED)
    started = _add(db_session, ScrapeTargetStatus.STARTED)
    done = _add(db_session, ScrapeTargetStatus.COMPLETED)

    claimed = claim_targets_started(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        match_ids=[pending, deferred, started, done],
    )

    assert claimed == {pending, deferred}


@pytest.mark.parametrize(
    "status",
    [
        ScrapeTargetStatus.COMPLETED,
        ScrapeTargetStatus.FAILED,
        ScrapeTargetStatus.SKIPPED,
        ScrapeTargetStatus.CANCELLED,
    ],
)
def test_a_refusal_never_overwrites_a_finished_target(
    db_session: Session, status: ScrapeTargetStatus
) -> None:
    match_id = _add(db_session, status)

    targets_mod._finalize_refused_targets(
        db_session,
        workspace_id=_WORKSPACE_ID,
        scrape_job_id=_JOB_ID,
        refusals_by_match={match_id: ScrapeErrorCode.ATTEMPT_BUDGET_EXHAUSTED},
        # Even if a caller wrongly listed it, a terminal row is never
        # overwritten: the status guard still applies.
        claimed_match_ids={match_id},
    )

    row = _row(db_session, match_id)
    assert row.status == status
    assert row.error_code is None


def test_refusal_finalizable_statuses_are_exactly_the_non_terminal_ones() -> None:
    assert set(REFUSAL_FINALIZABLE_TARGET_STATUSES) == {
        ScrapeTargetStatus.PENDING,
        ScrapeTargetStatus.DEFERRED,
        ScrapeTargetStatus.STARTED,
    }


def test_load_targets_finalizes_refusals_through_the_helper() -> None:
    """Structural pin: the load path uses the helper, not an inline
    ``mark_target`` with the pickup-eligible guard that caused the loop."""
    import inspect

    source = inspect.getsource(targets_mod.load_targets)
    assert "_finalize_refused_targets(" in source
    assert "only_if_status=PICKUP_ELIGIBLE_TARGET_STATUSES" not in source
    # ...and hands it the rows its own claim moved (2026-10-06).
    assert "claimed_match_ids=claimed_match_ids" in source


# --- a load-made selection is persisted on the cursor ------------------


class _Method:
    def __init__(self) -> None:
        self.id = uuid.uuid4()


class _Selection:
    def __init__(self, method: _Method, attempt_ordinal: int) -> None:
        self.method = method
        self.attempt_ordinal = attempt_ordinal


def test_a_load_made_selection_is_persisted_on_the_cursor(db_session: Session) -> None:
    """A re-pickup must reuse this cursor (no second charge), exactly as it
    reuses one the dispatcher wrote -- see `_persist_load_selection`."""
    match_id = _add(db_session, ScrapeTargetStatus.PENDING)
    job_target = _row(db_session, match_id)
    method = _Method()

    targets_mod._persist_load_selection(job_target, _Selection(method, 1))

    row = _row(db_session, match_id)
    assert row.current_strategy_method_id == method.id
    assert row.strategy_attempt_ordinal == 1
    assert row.chain_token is not None


def test_persisting_a_selection_keeps_an_existing_chain_token(db_session: Session) -> None:
    match_id = _add(db_session, ScrapeTargetStatus.PENDING)
    job_target = _row(db_session, match_id)
    token = uuid.uuid4()
    job_target.chain_token = token

    targets_mod._persist_load_selection(job_target, _Selection(_Method(), 2))

    assert _row(db_session, match_id).chain_token == token


def test_persisting_without_a_job_target_is_a_no_op() -> None:
    targets_mod._persist_load_selection(None, _Selection(_Method(), 1))


def test_load_targets_persists_its_own_selections() -> None:
    import inspect

    source = inspect.getsource(targets_mod.load_targets)
    assert "_persist_load_selection(job_target, selection)" in source
