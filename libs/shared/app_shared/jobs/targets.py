"""Target-row lifecycle transitions + counter aggregation.

Per ``contracts/lifecycle-counters.md`` (D5, FR-017, FR-018). Pure
SQLAlchemy — no scrapy/twisted/fastapi.

:func:`mark_target` is the **single writer** of a ``scrape_job_targets``
row's ``status``/timestamps/``error_code`` transition
(``PENDING -> STARTED -> COMPLETED/FAILED/SKIPPED``). It touches ONLY the
target row it resolves — never the parent job's counters. This is the
FR-017 seam the SPEC-07 item-persistence pipeline calls as targets
progress (wired in T052; that pipeline lives outside this package so
this module stays scrapy/twisted/fastapi-free and importable from it).

:func:`aggregate_counts` performs the one scoped ``GROUP BY status`` read
the ``finalize_jobs``/``refresh_job_counters`` maintenance tasks
(``apps/workers/app/workers/tasks_jobs.py``) use to overwrite a job row's
counters in a single ``UPDATE`` — never a per-target increment (FR-018,
SC-004, Principle VIII).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from collections.abc import Iterable, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app_shared.enums import ScrapeErrorCode, ScrapeTargetStatus
from app_shared.models.jobs import ScrapeJobTarget

__all__ = [
    "Counts",
    "PHASE_TIMESTAMP_COLUMNS",
    "PICKUP_ELIGIBLE_TARGET_STATUSES",
    "aggregate_counts",
    "mark_target",
    "mark_targets_started",
    "stamp_target_timestamps",
]

#: EPA A5 (2026-09-07). The statuses a target may legitimately be picked
#: up FROM. ``PENDING`` is a target that has never run; ``DEFERRED`` is
#: the non-terminal overflow outcome handed back to dispatch (SPEC-11
#: US3) and re-pickup is exactly what it exists for. Every other status
#: is either already ``STARTED`` (re-stamping it would move
#: ``started_at``) or terminal (``_TERMINAL_TARGET_STATUSES``).
PICKUP_ELIGIBLE_TARGET_STATUSES: tuple[ScrapeTargetStatus, ...] = (
    ScrapeTargetStatus.PENDING,
    ScrapeTargetStatus.DEFERRED,
)

#: EPA A5. The per-phase lifecycle timestamp columns
#: (``app_shared.models.jobs.ScrapeJobTarget``) this module is the single
#: writer of. Every one is stamped with ``COALESCE(<column>, :value)``
#: semantics -- **first writer wins** -- because each names a boundary
#: that happened exactly once; a retried attempt or a duplicate flush must
#: never be able to move a boundary that already occurred.
PHASE_TIMESTAMP_COLUMNS: tuple[str, ...] = (
    "claimed_at",
    "remote_accepted_at",
    "first_network_at",
    "document_received_at",
    "extraction_finished_at",
    "persisted_at",
)

# A target in one of these statuses has reached a terminal outcome —
# `completed_at` is stamped exactly once, on the transition into one of
# these (D5). `DEFERRED` (SPEC-11 US3, overflow) is deliberately NOT a
# member -- it is a non-terminal, requeue-cap-exceeded outcome handed
# back to Celery for re-dispatch (contracts/overflow-dispatch.md §1;
# data-model.md §2.1), so a transition into it stamps neither
# `completed_at` nor `started_at`.
#
# `CANCELLED` (EPA A2, 2026-08-25) IS a member: an administratively
# cancelled target is finished for good. Its membership is what makes
# cancellation idempotent for free -- a replayed cancellation finds the
# target already terminal and returns without touching it -- and what
# lets `finalize_jobs` converge on a cancelled job instead of waiting
# forever for targets nothing will ever pick up.
_TERMINAL_TARGET_STATUSES = frozenset(
    {
        ScrapeTargetStatus.COMPLETED,
        ScrapeTargetStatus.FAILED,
        ScrapeTargetStatus.SKIPPED,
        ScrapeTargetStatus.CANCELLED,
    }
)


@dataclass(frozen=True)
class Counts:
    """Aggregated `scrape_job_targets` counts for one job."""

    success: int
    failure: int
    skipped: int
    total: int
    #: EPA A2. Administratively cancelled targets. Deliberately its OWN
    #: bucket with a default, not folded into `skipped`: a cancellation is
    #: not a scraper outcome, and rolling it into any existing counter
    #: would make a job that was closed by hand read back as though the
    #: scraper had decided something. `scrape_jobs` has no matching
    #: counter column, so nothing persists this — it exists so a reader
    #: that wants the full picture (`success + failure + skipped +
    #: cancelled` vs `total`) can get it from the one aggregate query
    #: instead of re-scanning the target rows.
    cancelled: int = 0


def _phase_timestamp_values(phases: dict[str, datetime | None]) -> dict[str, datetime]:
    """Drop the ``None``\\ s: an unsupplied phase is not a phase that
    happened at NULL, it is a phase this caller knows nothing about, and
    writing it would erase what an earlier writer recorded."""
    unknown = set(phases) - set(PHASE_TIMESTAMP_COLUMNS)
    if unknown:  # pragma: no cover - programming error, not a runtime path
        raise ValueError(f"unknown phase timestamp column(s): {sorted(unknown)}")
    return {name: value for name, value in phases.items() if value is not None}


def stamp_target_timestamps(
    session: Session,
    *,
    workspace_id: uuid.UUID | str,
    scrape_job_id: uuid.UUID | str,
    match_id: uuid.UUID | str,
    claimed_at: datetime | None = None,
    remote_accepted_at: datetime | None = None,
    first_network_at: datetime | None = None,
    document_received_at: datetime | None = None,
    extraction_finished_at: datetime | None = None,
    persisted_at: datetime | None = None,
) -> int:
    """Stamp per-phase lifecycle timestamps WITHOUT touching ``status``.

    EPA A5. One ``UPDATE ... SET <col> = COALESCE(<col>, :value)`` for the
    supplied phases only — first writer wins, so a duplicate flush or a
    retried attempt can never move a boundary that already happened.
    Deliberately status-blind and terminal-blind: a phase boundary is a
    fact about wall-clock time, not a lifecycle transition, and recording
    when the document arrived for a target that has since been cancelled
    is still true. Every *status* transition still goes through
    :func:`mark_target`, which remains the single writer of ``status``.

    Returns the number of rows updated (0 when the target does not
    resolve in-workspace, or when no phase was supplied at all).
    """
    values = _phase_timestamp_values(
        {
            "claimed_at": claimed_at,
            "remote_accepted_at": remote_accepted_at,
            "first_network_at": first_network_at,
            "document_received_at": document_received_at,
            "extraction_finished_at": extraction_finished_at,
            "persisted_at": persisted_at,
        }
    )
    if not values:
        return 0
    stmt = (
        update(ScrapeJobTarget)
        .where(
            ScrapeJobTarget.workspace_id == workspace_id,
            ScrapeJobTarget.scrape_job_id == scrape_job_id,
            ScrapeJobTarget.match_id == match_id,
        )
        .values(
            {
                name: func.coalesce(getattr(ScrapeJobTarget, name), value)
                for name, value in values.items()
            }
        )
    )
    return session.execute(stmt).rowcount or 0


def mark_targets_started(
    session: Session,
    *,
    workspace_id: uuid.UUID | str,
    scrape_job_id: uuid.UUID | str,
    match_ids: Sequence[uuid.UUID | str],
    only_if_status: Iterable[ScrapeTargetStatus] = PICKUP_ELIGIBLE_TARGET_STATUSES,
) -> int:
    """Move every pickup-eligible target of ``match_ids`` to ``STARTED``.

    EPA A5, and the reason this exists at all: **no production code path
    ever transitioned a target to ``STARTED``** before this task. Targets
    went ``PENDING`` -> terminal, so ``started_at`` was always NULL, the
    ``STARTED`` bucket was always empty, and every consumer of that fact —
    the 2026-09-03 stale-target reaper (which only reaps ``STARTED`` rows
    aged past a ceiling), the ``crawmatic_target_oldest_age_seconds
    {status="STARTED"}`` gauge, and the deploy-survival proof that reads
    "work was in flight when the process died" — was silently inert. This
    function is what makes them mean something.

    The batch sibling of :func:`mark_target`'s ``only_if_status`` path:
    **exactly one** ``UPDATE`` for the whole batch regardless of
    ``len(match_ids)`` (Principle IV — the spider-side target loader that
    calls this is a documented bounded load and must not grow a per-match
    query; naming its module here would be a forbidden reverse dependency
    edge, guarded by ``tests/unit/test_import_boundaries.py``), with
    identical semantics:

    ``UPDATE scrape_job_targets SET status='STARTED',
    started_at = COALESCE(started_at, now())
    WHERE workspace_id=... AND scrape_job_id=... AND match_id IN (...)
      AND status IN ('PENDING','DEFERRED')``

    Two properties come out of that one statement, and both matter:

    * **Idempotent.** The ``status IN`` predicate excludes rows already
      ``STARTED`` or terminal, so a second load — a duplicate spider run,
      a re-dispatch — matches zero rows and changes nothing. The
      ``COALESCE`` is the belt to that braces: even if a row were somehow
      re-matched, ``started_at`` keeps its first value.
    * **Never resurrects a finished target.** Terminal statuses are simply
      not in the eligible set, so this can no more re-open a COMPLETED
      target than :func:`mark_target` can (the same invariant that lets
      ``finalize_jobs`` converge).

    Returns the number of targets actually transitioned.
    """
    eligible = tuple(only_if_status)
    if not match_ids or not eligible:
        return 0
    stmt = (
        update(ScrapeJobTarget)
        .where(
            ScrapeJobTarget.workspace_id == workspace_id,
            ScrapeJobTarget.scrape_job_id == scrape_job_id,
            ScrapeJobTarget.match_id.in_(list(match_ids)),
            ScrapeJobTarget.status.in_(eligible),
        )
        .values(
            status=ScrapeTargetStatus.STARTED,
            started_at=func.coalesce(ScrapeJobTarget.started_at, func.now()),
        )
    )
    return session.execute(stmt).rowcount or 0


def mark_target(
    session: Session,
    *,
    workspace_id: uuid.UUID | str,
    scrape_job_id: uuid.UUID | str,
    match_id: uuid.UUID | str,
    status: ScrapeTargetStatus,
    error_code: ScrapeErrorCode | None = None,
    cancelled_by: str | None = None,
    cancelled_reason: str | None = None,
    only_if_status: Iterable[ScrapeTargetStatus] | None = None,
    claimed_at: datetime | None = None,
    remote_accepted_at: datetime | None = None,
    first_network_at: datetime | None = None,
    document_received_at: datetime | None = None,
    extraction_finished_at: datetime | None = None,
    persisted_at: datetime | None = None,
) -> None:
    """Transition the target ``(workspace_id, scrape_job_id, match_id)``.

    Sets ``started_at`` on a transition to ``STARTED``, ``completed_at``
    on a transition to any terminal status (``COMPLETED``/``FAILED``/
    ``SKIPPED`` — NOT ``DEFERRED``, which is non-terminal, SPEC-11 US3),
    and persists ``error_code`` whenever one is provided — regardless of
    ``status`` (``error_code is not None``). This covers ``FAILED``
    (HTTP/extraction/validation failures), ``SKIPPED`` (carrying
    ``LOCKED_ALREADY_RUNNING`` on a match-lock collision), and
    ``DEFERRED`` (carrying ``RATE_LIMITED`` on a requeue-cap overflow) —
    previously the gate only fired on ``status == FAILED``, silently
    dropping the code on ``SKIPPED``/``DEFERRED`` (analyze finding G1,
    FR-020/FR-021, SC-006, `contracts/overflow-dispatch.md` §2).

    ``CANCELLED`` (EPA A2, 2026-08-25) is the administrative terminal
    transition, and it is why this writer grew ``cancelled_by`` /
    ``cancelled_reason``: closing a stranded job must record *who*
    decided and *why*, on the row itself, so a target finished without a
    result can never later be mistaken for a scraper outcome. Both are
    stamped (together with ``cancelled_at``) **only** on a transition to
    ``CANCELLED`` — passing them with any other status is ignored rather
    than silently writing cancellation provenance onto a real result.
    ``app_shared.jobs.cancellation`` is the only intended caller; it goes
    through this function precisely so ``scrape_job_targets`` keeps
    exactly one transition writer (no direct row updates anywhere).

    **A target already in a terminal status is never transitioned again**
    (2026-08-03) — see the inline note; this is what makes a job's
    terminal-target count monotonic and therefore lets `finalize_jobs`
    converge. Touches
    ONLY this target row — never the parent job's counters (D5);
    ``aggregate_counts`` is the sole counter source. A no-op if the
    target can't be resolved in-workspace (e.g. an already-deleted/
    archived row).

    **``only_if_status`` (EPA A5, 2026-09-07)** turns this writer into a
    single conditional statement instead of a read-then-mutate: when it
    is given, the transition is issued as ONE

    ``UPDATE scrape_job_targets SET status=:status,
    started_at = COALESCE(started_at, now()) [, ...]
    WHERE workspace_id=... AND scrape_job_id=... AND match_id=...
      AND status IN (...)``

    and the row is never loaded. That matters for the pickup transition
    (``PENDING``/``DEFERRED`` -> ``STARTED``): the database, not a
    read-modify-write window, decides whether this caller won, so two
    concurrent pickups cannot both believe they started the target and
    ``started_at`` cannot be moved by the loser. ``COALESCE`` makes the
    statement idempotent on its own terms — replaying it never advances a
    timestamp that is already set. The terminal-status guard below is
    subsumed by the caller's ``only_if_status`` set (pass only
    non-terminal statuses; :data:`PICKUP_ELIGIBLE_TARGET_STATUSES` is the
    intended value for pickup). See :func:`mark_targets_started` for the
    batch form, which is what ``load_targets`` uses so a bounded load
    stays bounded.

    **Phase timestamps (EPA A5)** — ``claimed_at``/``remote_accepted_at``/
    ``first_network_at``/``document_received_at``/
    ``extraction_finished_at``/``persisted_at`` may be supplied on any
    transition and are stamped with the same **first-writer-wins**
    ``COALESCE`` semantics as everywhere else in this module (see
    :data:`PHASE_TIMESTAMP_COLUMNS`); an omitted (``None``) phase is left
    exactly as it was rather than nulled. :func:`stamp_target_timestamps`
    records them without a status transition, for the persistence path
    that has boundaries to report but no lifecycle change to make.
    """
    phases = _phase_timestamp_values(
        {
            "claimed_at": claimed_at,
            "remote_accepted_at": remote_accepted_at,
            "first_network_at": first_network_at,
            "document_received_at": document_received_at,
            "extraction_finished_at": extraction_finished_at,
            "persisted_at": persisted_at,
        }
    )

    if only_if_status is not None:
        eligible = tuple(only_if_status)
        if not eligible:
            return
        now = datetime.now(timezone.utc)
        values: dict[str, Any] = {"status": status}
        if status == ScrapeTargetStatus.STARTED:
            values["started_at"] = func.coalesce(ScrapeJobTarget.started_at, func.now())
        if status in _TERMINAL_TARGET_STATUSES:
            values["completed_at"] = now
        if status == ScrapeTargetStatus.CANCELLED:
            values["cancelled_at"] = now
            values["cancelled_by"] = cancelled_by
            values["cancelled_reason"] = cancelled_reason
        if error_code is not None:
            values["error_code"] = error_code
        for name, value in phases.items():
            values[name] = func.coalesce(getattr(ScrapeJobTarget, name), value)
        session.execute(
            update(ScrapeJobTarget)
            .where(
                ScrapeJobTarget.workspace_id == workspace_id,
                ScrapeJobTarget.scrape_job_id == scrape_job_id,
                ScrapeJobTarget.match_id == match_id,
                ScrapeJobTarget.status.in_(eligible),
            )
            .values(values)
        )
        return

    stmt = select(ScrapeJobTarget).where(
        ScrapeJobTarget.workspace_id == workspace_id,
        ScrapeJobTarget.scrape_job_id == scrape_job_id,
        ScrapeJobTarget.match_id == match_id,
    )
    target = session.execute(stmt).scalar_one_or_none()
    if target is None:
        return

    # EPA A5: phase boundaries are stamped BEFORE the terminal guard
    # below. "The document arrived at T" stays true even for a target
    # that has since been cancelled or terminalized by an earlier
    # attempt; refusing to record it would lose the only evidence of
    # where the time went. First-writer-wins, like everywhere else.
    for _name, _value in phases.items():
        if getattr(target, _name) is None:
            setattr(target, _name, _value)

    # Terminal is terminal (2026-08-03). This writer used to be a blind
    # last-writer-wins overwrite, so a late-arriving DEFERRED from a
    # long-lived spider could resurrect a target that had already
    # finished -- observed live: a job's completed-target count moving
    # *backwards*, and (2026-08-02) noon targets flip-flopping between
    # FAILED and DEFERRED for three hours, which kept their job from ever
    # finalizing. A job only ever dispatches PENDING/DEFERRED targets, so
    # nothing legitimately re-opens a finished one; a genuine re-scrape
    # creates a new job with its own target rows.
    if target.status in _TERMINAL_TARGET_STATUSES:
        return

    target.status = status
    now = datetime.now(timezone.utc)
    if status == ScrapeTargetStatus.STARTED:
        target.started_at = now
    if status in _TERMINAL_TARGET_STATUSES:
        target.completed_at = now
    if status == ScrapeTargetStatus.CANCELLED:
        # Provenance is stamped only here (EPA A2). `cancelled_at` is
        # recorded alongside `completed_at` rather than instead of it:
        # the target IS finished (every terminal-status reader must see
        # that), and the cancellation columns say why it finished
        # without a result.
        target.cancelled_at = now
        target.cancelled_by = cancelled_by
        target.cancelled_reason = cancelled_reason
    if error_code is not None:
        target.error_code = error_code


def aggregate_counts(
    session: Session,
    scrape_job_id: uuid.UUID | str,
    workspace_id: uuid.UUID | str,
) -> Counts:
    """One scoped ``SELECT status, COUNT(*) ... GROUP BY status`` for `scrape_job_id`.

    Callers (``finalize_jobs``/``refresh_job_counters``) write the
    resulting totals to the job row in **one** ``UPDATE`` — never a
    per-target increment (FR-018, SC-004).
    """
    stmt = (
        select(ScrapeJobTarget.status, func.count())
        .where(
            ScrapeJobTarget.workspace_id == workspace_id,
            ScrapeJobTarget.scrape_job_id == scrape_job_id,
        )
        .group_by(ScrapeJobTarget.status)
    )
    by_status: dict[ScrapeTargetStatus, int] = dict(session.execute(stmt).all())

    success = by_status.get(ScrapeTargetStatus.COMPLETED, 0)
    failure = by_status.get(ScrapeTargetStatus.FAILED, 0)
    skipped = by_status.get(ScrapeTargetStatus.SKIPPED, 0)
    cancelled = by_status.get(ScrapeTargetStatus.CANCELLED, 0)
    total = sum(by_status.values())
    return Counts(
        success=success,
        failure=failure,
        skipped=skipped,
        total=total,
        cancelled=cancelled,
    )
