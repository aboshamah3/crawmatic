"""Audited job cancellation + reconciliation (EPA A2, 2026-08-25).

The **only** sanctioned way to close a scrape job whose targets nothing
will ever pick up again. It exists because every other way of getting a
stranded job out of the way is a lie: marking its targets ``COMPLETED``
invents successes that never happened, marking them ``FAILED`` invents
scraper failures that never happened, and deleting the rows destroys the
evidence. This module terminalizes the remaining targets as
``CANCELLED`` — a status that means exactly "a human closed this" — and
records who and why on each row.

Why there is a *fence* and not a transaction
--------------------------------------------
Cancelling a job touches four systems that cannot share a transaction:

1. Postgres  — job status, target statuses, the durable event;
2. Scrapyd   — runs already scheduled on remote nodes;
3. Redis     — the ``dispatched:{job}:*`` idempotency sentinels (keyed on
   the dispatch identity's digest since B1 rather than a batch position;
   still job-prefixed, so step 3's scan still finds every one of them);
4. the C3 reservation ledger (``cost_reservations`` + the budget
   counters it holds against).

Pretending those four commit atomically is how systems end up with a
job that the database calls ``CANCELLED`` while a spider is still
writing observations into it. So this module does not pretend. It
establishes a **durable fence** in step 1 and treats steps 2-4 as
best-effort cleanup that may crash, be retried, or never run at all:

    the cancellation transaction bumps
    ``scrape_jobs.cancellation_generation`` and sets the job status to
    ``CANCELLED`` **first**, before any target is touched and before
    anything outside Postgres is contacted.

Everything downstream reads that generation as "the generation this work
was authorized under". Dispatch paths (B1/B2) reject a stale generation
rather than re-POSTing; the scraping layer's result-persistence path (its
batch flush, via :func:`cancelled_scrape_job_ids`) refuses to persist an
observation for a cancelled job and records the attempt as
``late_after_cancel`` instead. That caller is deliberately named only by
role here — this package must never reference the scraping package, not
even in prose (``tests/unit/test_import_boundaries.py`` enforces the
one-way dependency edge by scanning source text, so a comment naming it
would be indistinguishable from a lazy import waiting to happen).
The consequence is the property that matters: **once the database says
``CANCELLED``, nothing can contradict it** — not a spider that was
mid-flight, not a duplicate Celery delivery, not a Scrapyd node that
never got the cancel request.

Ordering protocol (idempotent, resumable)
-----------------------------------------
:func:`cancel_and_reconcile_job` runs, in this order:

1. **DB transaction** — fence (generation bump + job ``CANCELLED``),
   then ``mark_target(..., CANCELLED)`` for every non-terminal target,
   then one outbox event. Commits as a unit, **and this function is what
   commits it, on every path** — see below.
2. **Scrapyd** — best-effort ``cancel.json`` POST for every Scrapyd job
   id recorded for this job (see :func:`iter_known_scrapyd_job_ids`).
3. **Redis** — delete surviving ``dispatched:{job_id}:*`` guards. Safe
   only *because* step 1 already committed: with the fence in place a
   re-dispatch that races this delete is rejected on its stale
   generation, so removing the sentinels cannot resurrect the job.
4. **Reservations** — :func:`release_reservations_for_job`, real as of
   EPA C3: it releases every ``RESERVED`` ``cost_reservations`` row the
   job holds, returning each hold to the tenant and fleet budget
   counters. Idempotent by compare-and-set, so re-running a crashed
   cancellation cannot double-credit a budget.

Steps 2-4 are individually failure-tolerant and safe to re-run: calling
:func:`cancel_and_reconcile_job` again on an already-cancelled job
cancels nothing, reports ``idempotent_replay=True``, emits no second
event, and still redoes the cleanup — which is what makes a crash
between steps recoverable by simply calling it again.

**Who commits step 1.** The ordering above is only true if the fence is
durable *before* step 2 runs, so :func:`cancel_and_reconcile_job` owns
that commit itself rather than delegating it to whoever called it:

* handed an **unscoped** session, it opens its own
  :func:`~app_shared.maintenance.scoping.workspace_context`, which
  commits on the way out of the block;
* handed a session that is **already inside a workspace-scoped
  transaction** (the FastAPI request path — ``apps/api/app/deps.py``
  yields the session to the route and commits only *after* the handler
  returns), it calls ``session.commit()`` explicitly before step 2.

Without that explicit commit the primary production path ran steps 2-4
with the fence still uncommitted: a failure in the dependency teardown
would then have left the ``dispatched:{job}:*`` sentinels deleted while
the job was *not* cancelled — precisely the state step 3's comment says
cannot happen (EPA Phase A review F-1). The consequence for callers is
stated in :func:`cancel_and_reconcile_job`'s ``session`` argument: this
function ends the transaction it was given, so it must not be called
from inside a ``workspace_context``/``session.begin()`` block.

Scraping-free and Celery-free (Constitution I/V,
``tests/unit/test_import_boundaries.py``): SQLAlchemy + stdlib +
``app_shared`` only. The event is *recorded*, never published, here.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_shared.enums import ScrapeJobStatus, ScrapeTargetStatus, WebhookEventType
from app_shared.ids import new_uuid7
from app_shared.jobs.dispatch_intents import iter_dispatch_scrapyd_job_ids
from app_shared.jobs.targets import mark_target
from app_shared.maintenance.scoping import workspace_context
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.outbox.writer import write_outbox_message
from app_shared.redis_client import get_redis_client
from app_shared.task_names import CREATE_WEBHOOK_EVENT

__all__ = [
    "LATE_AFTER_CANCEL_REASON",
    "CancellationReport",
    "cancel_and_reconcile_job",
    "cancelled_scrape_job_ids",
    "iter_known_scrapyd_job_ids",
    "release_reservations_for_job",
]

logger = logging.getLogger(__name__)

#: The single audited outcome for a scraper result that arrives after the
#: fence. Recorded on the ``request_attempts`` row (``error_message``) by
#: the scraping layer's batch flush — the attempt is kept, the
#: observation is not. A string constant rather than a
#: ``ScrapeErrorCode`` member on purpose: this is not a scrape failure,
#: it is a persistence refusal, and folding it into the scrape-error
#: vocabulary would corrupt every strategy/health statistic that scores
#: those codes.
LATE_AFTER_CANCEL_REASON = "late_after_cancel"

#: Statuses a target can be in and still be worth cancelling. Anything
#: else already reached a real outcome and is never touched again —
#: mirrors ``app_shared.jobs.targets._TERMINAL_TARGET_STATUSES`` from the
#: other side, deliberately as an allow-list so a future non-terminal
#: status is excluded until someone decides it should be cancellable.
_CANCELLABLE_TARGET_STATUSES = frozenset(
    {
        ScrapeTargetStatus.PENDING,
        ScrapeTargetStatus.STARTED,
        ScrapeTargetStatus.DEFERRED,
    }
)


@dataclass(frozen=True)
class CancellationReport:
    """The outcome of one :func:`cancel_and_reconcile_job` call.

    ``targets_cancelled`` counts only rows this call actually moved, so a
    replay reports ``0`` — the number is a record of what happened, never
    a restatement of the job's shape. ``outbox_message_id`` is ``None``
    on a replay for the same reason: exactly one event exists per job
    cancellation, and only the call that created it may claim it.
    """

    job_id: str
    targets_cancelled: int
    targets_already_terminal: int
    outbox_message_id: str | None
    idempotent_replay: bool


# ---------------------------------------------------------------------------
# Seams for work that other tasks own
# ---------------------------------------------------------------------------


def iter_known_scrapyd_job_ids(
    session: Session, scrape_job_id: uuid.UUID | str
) -> Sequence[str]:
    """Scrapyd job ids recorded for ``scrape_job_id`` (EPA B1: **real**).

    Reads ``dispatch_intents`` — the durable record of "we POSTed this
    batch to node X and it answered with Scrapyd job id Y" that B1
    introduced. Step 2 of the ordering protocol now actually asks those
    runs to stop, where before B1 it iterated an empty sequence.

    The workspace is resolved from the job (a single-column projection,
    :func:`_job_workspace_id`) and then passed explicitly into a
    ``scoped_select``, so the tenant boundary is asserted in the SQL
    rather than left to the ``app.workspace_id`` GUC — the same posture
    every other query in this module takes. A job that is not visible in
    the caller's workspace yields nothing to cancel, which is the correct
    (and safe) answer.

    Fails **soft**: any lookup error is logged and reported as "no known
    runs". Cancelling a remote run is a cost optimization, never a
    correctness requirement — the fence already guarantees that whatever
    those runs produce is refused by the persistence path — so a failure
    here must not be able to break a committed cancellation.
    """
    try:
        job_uuid = (
            scrape_job_id
            if isinstance(scrape_job_id, uuid.UUID)
            else uuid.UUID(str(scrape_job_id))
        )
        workspace_id = _job_workspace_id(session, job_uuid)
        if workspace_id is None:
            return ()
        return iter_dispatch_scrapyd_job_ids(
            session, workspace_id=workspace_id, scrape_job_id=job_uuid
        )
    except Exception:  # noqa: BLE001 - lookup must never fail a cancellation
        logger.warning(
            "job_cancel.scrapyd_job_id_lookup_failed scrape_job_id=%s",
            scrape_job_id,
            exc_info=True,
        )
        return ()


def release_reservations_for_job(session: Session, scrape_job_id: uuid.UUID | str) -> int:
    """Release every live cost reservation held by ``scrape_job_id``.

    Step 4 of the ordering protocol, and **real** as of EPA C3: it
    iterates the job's ``RESERVED`` ``cost_reservations`` rows and
    compare-and-sets each to ``RELEASED``, returning its whole hold to
    the tenant and fleet budget counters. Returns how many rows this call
    moved.

    Idempotent, twice over. The CAS makes a second call a no-op on every
    row it already released (so it returns ``0``), and each individual
    release is a no-op on an already-terminal row — which is what makes
    re-running a crashed cancellation safe, and what completes the
    "supported" certification cancellation could not claim while this was
    a stub.

    **Transaction posture.** This runs AFTER the cancellation transaction
    has committed (the module's ordering protocol requires the fence to be
    durable before anything else happens), so it cannot assume a live
    transaction and must not leave one open:

    * handed a session already inside a transaction — the fake/scoped
      posture — it joins it and lets the caller commit;
    * handed an unscoped one, it resolves the job's workspace id (and
      *only* the id) on the sanctioned BYPASSRLS system role and does the
      work inside its own :func:`~app_shared.maintenance.scoping.
      workspace_context`, which commits on the way out.

    Fails **soft** on a workspace that cannot be resolved: a job whose
    workspace is gone has no reservations anyone can scope to, and a
    lookup failure must never be able to turn a committed cancellation
    into an exception. The lease sweeper is the backstop — an unreleased
    reservation's lease lapses and is reaped once the ledger confirms no
    operation is open under it.
    """
    from app_shared.costauth.service import release_reservations_for_scrape_job

    job_uuid = (
        scrape_job_id
        if isinstance(scrape_job_id, uuid.UUID)
        else uuid.UUID(str(scrape_job_id))
    )

    if session.in_transaction():
        # Already scoped by the caller: the reservation rows are visible
        # under the live `app.workspace_id`, and the workspace predicate
        # is redundant-but-harmless, so it is left to the caller's scope.
        return release_reservations_for_scrape_job(
            session, workspace_id=None, scrape_job_id=job_uuid
        )

    try:
        workspace_id = _resolve_workspace_id(session, job_uuid)
    except LookupError:
        logger.warning(
            "job_cancel.reservation_release_unresolved scrape_job_id=%s", job_uuid
        )
        return 0

    with workspace_context(session, workspace_id):
        return release_reservations_for_scrape_job(
            session, workspace_id=workspace_id, scrape_job_id=job_uuid
        )


def cancelled_scrape_job_ids(
    session: Session, scrape_job_ids: Iterable[uuid.UUID | str]
) -> set[uuid.UUID]:
    """Which of ``scrape_job_ids`` are fenced (job status ``CANCELLED``).

    The read side of the fence, used by the result-persistence path to
    decide whether an arriving batch is a late result. One scoped ``IN``
    query for the whole batch — never a per-item lookup.

    Fails **closed toward persistence**, i.e. a job that cannot be
    resolved is simply absent from the result and its results persist as
    before. That is the right direction: this check exists to stop a
    cancelled job from being contradicted, not to become a new way for
    a database hiccup to silently discard good observations.
    """
    ids = {
        job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
        for job_id in scrape_job_ids
        if job_id is not None
    }
    if not ids:
        return set()

    rows = (
        session.execute(
            select(ScrapeJob.id).where(
                ScrapeJob.id.in_(ids),
                ScrapeJob.status == ScrapeJobStatus.CANCELLED,
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


# ---------------------------------------------------------------------------
# The cancellation itself
# ---------------------------------------------------------------------------


def cancel_and_reconcile_job(
    session: Session,
    *,
    scrape_job_id: str,
    actor: str,
    reason: str,
) -> CancellationReport:
    """Close ``scrape_job_id``: fence it, cancel its live targets, clean up.

    Args:
        session: the caller's session. If it is already inside a
            workspace-scoped transaction (the API request path, or a
            worker that has already called ``set_workspace_context``) the
            fenced write joins that transaction and **this function
            commits it** before the out-of-band steps run. If it is not in
            a transaction, this opens exactly one
            :func:`~app_shared.maintenance.scoping.workspace_context` for
            the job's workspace and commits it on the way out. Both
            postures are supported because ``SET LOCAL app.workspace_id``
            is transaction-local: entering a nested context would silently
            re-scope the caller's pending writes (see
            ``WorkspaceContextError``), so the only safe answer is to
            detect which posture we are in rather than assume one.

            Either way the transaction ends inside this call, because the
            module's ordering protocol requires the fence to be durable
            before anything outside Postgres is touched and the caller
            cannot be trusted to have committed by then (on the API path
            it demonstrably has not — ``deps.py`` commits after the
            handler returns). Two consequences for callers:
            **(a)** any unrelated pending write on the same session is
            committed with the fence, so do not batch other work into it;
            **(b)** do not call this from inside a ``workspace_context``
            (or any ``with session.begin():`` block) — the block's own
            exit would then try to commit a transaction that is already
            closed. Hand it either a scoped session you own outright or an
            unscoped one.
        actor: who requested the cancellation. Recorded on every target
            row and in the event payload — an unattributed cancellation
            is not auditable, so this is required, not optional.
        reason: why. Same durability as ``actor``.

    Returns:
        A :class:`CancellationReport`.

    Raises:
        LookupError: ``scrape_job_id`` does not resolve in the session's
            workspace. Deliberately loud: silently reporting "0 targets
            cancelled" for a job id that was mistyped, or that belongs to
            another tenant, would look exactly like a successful
            cancellation.
        ValueError: ``actor`` or ``reason`` is blank.
    """
    if not actor or not actor.strip():
        raise ValueError("actor is required — an unattributed cancellation is not auditable")
    if not reason or not reason.strip():
        raise ValueError("reason is required — an unexplained cancellation is not auditable")

    job_uuid = uuid.UUID(str(scrape_job_id))

    if session.in_transaction():
        # The caller's transaction is already workspace-scoped (`SET LOCAL
        # app.workspace_id`), so RLS decides what this session can see. Read
        # the owning workspace id *through that scope* — a job in another
        # tenant simply is not visible and resolves to `None`, which is the
        # 404 the API path wants. The id is then threaded explicitly into
        # every row query below, so tenant isolation is asserted in the SQL
        # (Principle II / `scripts/check_workspace_scoping.py`) rather than
        # left implicit in a GUC set by somebody else.
        workspace_id = _job_workspace_id(session, job_uuid)
        if workspace_id is None:
            raise LookupError(f"scrape job {job_uuid} not found in the current workspace")
        report, scrapyd_job_ids = _cancel_fenced(
            session, job_uuid, workspace_id=workspace_id, actor=actor, reason=reason
        )
        # Make the fence DURABLE here, not whenever the caller gets round
        # to it. On the API path the caller is `deps.py`'s dependency,
        # which commits only after the route handler has returned — so
        # without this line steps 2-4 below would run against an
        # uncommitted fence and a failure in the teardown would leave the
        # `dispatched:{job}:*` guards deleted for a job that is not
        # cancelled. That is the one state step 3 asserts is impossible,
        # so the assertion is made true rather than merely written down.
        # The dependency's later `commit()` is then a no-op on an empty
        # transaction.
        session.commit()
    else:
        workspace_id = _resolve_workspace_id(session, job_uuid)
        with workspace_context(session, workspace_id):
            report, scrapyd_job_ids = _cancel_fenced(
                session, job_uuid, workspace_id=workspace_id, actor=actor, reason=reason
            )

    # --- steps 2-4: best-effort, post-COMMIT ------------------------------
    #
    # Both branches above end their transaction, so the fence is on disk
    # by the time control reaches this line — that is the precondition
    # every step below is documented as relying on, and it now holds on
    # the HTTP path too.
    #
    # Everything below this line may fail, be interrupted, or be run
    # twice. None of it can change the answer to "is this job
    # cancelled?" — that was settled above and is durable. Failures are
    # logged and swallowed so a Redis blip or an unreachable Scrapyd node
    # can never turn a successful, committed cancellation into an
    # exception the caller has to interpret.
    _best_effort_scrapyd_cancel(job_uuid, scrapyd_job_ids)
    _delete_dispatch_guards(job_uuid)
    try:
        released = release_reservations_for_job(session, job_uuid)
    except Exception:  # noqa: BLE001 - cleanup must never fail a committed cancel
        logger.exception("job_cancel.reservation_release_failed scrape_job_id=%s", job_uuid)
        released = 0

    logger.info(
        "job_cancel actor=%s reason=%s scrape_job_id=%s cancelled=%d already_terminal=%d "
        "replay=%s reservations_released=%d",
        actor,
        reason,
        report.job_id,
        report.targets_cancelled,
        report.targets_already_terminal,
        report.idempotent_replay,
        released,
    )
    return report


def _job_workspace_id(session: Session, job_uuid: uuid.UUID) -> uuid.UUID | None:
    """The workspace owning ``job_uuid``, or ``None`` — id resolution ONLY.

    A single-column projection on purpose. It is the one query in this
    module that cannot carry a ``workspace_id`` predicate (finding the
    workspace is the whole point), so it deliberately reads *nothing but
    the id* — no row content crosses a tenant boundary even when this is
    run on the BYPASSRLS system role by :func:`_resolve_workspace_id`.
    That is exactly the ``fleet``-scope contract in
    :mod:`app_shared.maintenance.scoping`: ids here, rows inside a scope.

    ``.all()`` + attribute access rather than ``.scalar_one_or_none()``
    so the shape is a ``Row``, which is what both a real ``Session`` and
    the jobs-unit-test session double return for a column projection.
    """
    rows = session.execute(
        select(ScrapeJob.workspace_id).where(ScrapeJob.id == job_uuid)
    ).all()
    return rows[0].workspace_id if rows else None


def _resolve_workspace_id(session: Session, job_uuid: uuid.UUID) -> uuid.UUID:
    """The workspace owning ``job_uuid``, resolved id-only on the system role.

    Only reached when the caller handed over an unscoped session, which
    means we cannot read the job through RLS yet — and we cannot set the
    workspace GUC until we know which workspace to set it to. Resolving
    the id (and nothing else) on the sanctioned BYPASSRLS system role is
    exactly the ``fleet``-scope pattern
    ``app_shared.maintenance.scoping`` documents: ids across workspaces
    here, every row read/write inside ``workspace_context`` afterwards.
    """
    from app_shared.database import get_system_session

    with get_system_session() as system_session:
        workspace_id = _job_workspace_id(system_session, job_uuid)
    if workspace_id is None:
        raise LookupError(f"scrape job {job_uuid} not found")
    return workspace_id


def _cancel_fenced(
    session: Session,
    job_uuid: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
    actor: str,
    reason: str,
) -> tuple[CancellationReport, Sequence[str]]:
    """Step 1: fence, terminalize, record the event — one transaction.

    ``workspace_id`` is passed in (never re-derived from the loaded row)
    so the job load itself is workspace-scoped in SQL: the tenant
    boundary is asserted by the predicate, not merely trusted to the
    ``app.workspace_id`` GUC that RLS reads. A job id belonging to
    another tenant therefore resolves to "not found" here even if the
    GUC were somehow wrong.

    Returns the report plus the Scrapyd job ids read while the
    transaction was open, so step 2 can run after the commit without
    needing a live session.
    """
    job = session.execute(
        select(ScrapeJob).where(
            ScrapeJob.workspace_id == workspace_id,
            ScrapeJob.id == job_uuid,
        )
    ).scalar_one_or_none()
    if job is None:
        raise LookupError(f"scrape job {job_uuid} not found in the current workspace")

    now = datetime.now(timezone.utc)
    idempotent_replay = job.status == ScrapeJobStatus.CANCELLED

    # --- THE FENCE, first --------------------------------------------------
    #
    # Before a single target is touched. A reader that sees any cancelled
    # target must already be able to see the bumped generation, otherwise
    # there is a window in which a target is closed while dispatch still
    # believes its authorization is current.
    #
    # The bump happens once per cancellation, not once per call: a replay
    # is a no-op on the fence. Re-bumping would be harmless for
    # correctness (generations only ever move forward) but would make the
    # column useless as evidence of how many times this job was actually
    # cancelled.
    if not idempotent_replay:
        job.cancellation_generation = (job.cancellation_generation or 0) + 1
        job.status = ScrapeJobStatus.CANCELLED
        if job.completed_at is None:
            job.completed_at = now

    # --- terminalize the live targets --------------------------------------
    targets = list(
        session.execute(
            select(ScrapeJobTarget).where(
                ScrapeJobTarget.workspace_id == workspace_id,
                ScrapeJobTarget.scrape_job_id == job_uuid,
            )
        )
        .scalars()
        .all()
    )

    cancelled = 0
    already_terminal = 0
    for target in targets:
        if target.status not in _CANCELLABLE_TARGET_STATUSES:
            already_terminal += 1
            continue
        # `mark_target` is the single writer of a target's status
        # transition (contracts/lifecycle-counters.md). Cancellation goes
        # through it like every other transition — no direct row update
        # here — which is also what makes it re-entrant: the writer's own
        # terminal guard rejects a second cancellation of the same row.
        mark_target(
            session,
            workspace_id=workspace_id,
            scrape_job_id=job_uuid,
            match_id=target.match_id,
            status=ScrapeTargetStatus.CANCELLED,
            cancelled_by=actor,
            cancelled_reason=reason,
        )
        cancelled += 1

    scrapyd_job_ids = list(iter_known_scrapyd_job_ids(session, job_uuid))

    # --- the durable event -------------------------------------------------
    outbox_message_id: str | None = None
    if not idempotent_replay:
        outbox_message_id = str(
            _record_cancellation_event(
                session,
                workspace_id=workspace_id,
                job_uuid=job_uuid,
                actor=actor,
                reason=reason,
                targets_cancelled=cancelled,
                targets_already_terminal=already_terminal,
                now=now,
            )
        )

    return (
        CancellationReport(
            job_id=str(job_uuid),
            targets_cancelled=cancelled,
            targets_already_terminal=already_terminal,
            outbox_message_id=outbox_message_id,
            idempotent_replay=idempotent_replay,
        ),
        scrapyd_job_ids,
    )


def _record_cancellation_event(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    job_uuid: uuid.UUID,
    actor: str,
    reason: str,
    targets_cancelled: int,
    targets_already_terminal: int,
    now: datetime,
) -> uuid.UUID:
    """Record the ``scrape.job.cancelled`` event in the caller's transaction.

    **Consumer choice (b): the existing webhook-event seam.** The event
    goes out as an ``outbox_messages`` row whose ``task_name`` is
    :data:`~app_shared.task_names.CREATE_WEBHOOK_EVENT`, on the
    ``webhook_events`` queue — the same shape ``finalize_jobs`` and the
    strategy-transition seam already use.

    The alternative considered (and rejected) was a bespoke
    ``jobs.publish_job_cancelled`` task name. The outbox dispatcher
    ``send_task``s whatever ``task_name`` it finds; a name with no
    registered consumer and no queue route is published into the void and
    the message is marked as delivered, so the event would be **silently
    dropped** — the exact failure mode the outbox exists to eliminate.
    Choosing an already-registered, already-routed consumer means the
    event's delivery path is proven by every job-finalization event
    already flowing through it, with no new task, queue, worker
    subscription or deployment step to get wrong.

    Dedup: one event per job cancellation, keyed
    ``job-cancel:{scrape_job_id}``. The key is used twice — as the outbox
    row's ``dedup_key`` (the PENDING partial unique index collapses a
    concurrent racer) and as the webhook consumer's own dedup key (which
    collapses a redelivery after publication, when the outbox row is no
    longer PENDING and its index no longer guards anything).
    """
    # The message id doubles as the consumer's idempotency key — see
    # `apps/workers/app/workers/tasks_webhooks.py::create_webhook_event`.
    message_id = new_uuid7()
    dedup_key = f"job-cancel:{job_uuid}"
    payload = {
        "scrape_job_id": str(job_uuid),
        "status": ScrapeJobStatus.CANCELLED.value,
        "cancelled_by": actor,
        "reason": reason,
        "targets_cancelled": targets_cancelled,
        "targets_already_terminal": targets_already_terminal,
    }
    write_outbox_message(
        session,
        workspace_id=workspace_id,
        task_name=CREATE_WEBHOOK_EVENT,
        queue="webhook_events",
        kwargs={
            "workspace_id": str(workspace_id),
            "event_type": WebhookEventType.SCRAPE_JOB_CANCELLED.value,
            "payload": payload,
            "dedup_key": dedup_key,
            "event_id": str(message_id),
            "occurred_at": now.isoformat(),
        },
        dedup_key=dedup_key,
        now=now,
        message_id=message_id,
    )
    return message_id


# ---------------------------------------------------------------------------
# Steps 2-3: out-of-band cleanup
# ---------------------------------------------------------------------------


def _best_effort_scrapyd_cancel(
    job_uuid: uuid.UUID, scrapyd_job_ids: Sequence[str]
) -> None:
    """Step 2: ask Scrapyd to stop runs we know about. Best effort, always.

    Live since B1: :func:`iter_known_scrapyd_job_ids` now reads real
    Scrapyd job ids out of ``dispatch_intents``, so this loop actually
    POSTs ``cancel.json`` for each of them.

    A failure here is logged and swallowed on purpose: the fence already
    guarantees that whatever those runs produce will be refused, so an
    unreachable node costs money, not correctness.
    """
    if not scrapyd_job_ids:
        return

    from app_shared.scrapyd.client import ScrapydDispatchClient  # local: keeps `requests` lazy

    for scrapyd_job_id in scrapyd_job_ids:
        try:
            ScrapydDispatchClient().cancel(scrapyd_job_id)
        except Exception:  # noqa: BLE001 - cleanup must never fail a committed cancel
            logger.warning(
                "job_cancel.scrapyd_cancel_failed scrape_job_id=%s scrapyd_job_id=%s",
                job_uuid,
                scrapyd_job_id,
                exc_info=True,
            )


def _delete_dispatch_guards(job_uuid: uuid.UUID) -> None:
    """Step 3: delete this job's surviving ``dispatched:{job}:*`` sentinels.

    These are ``SET NX`` idempotency guards that stop dispatch re-POSTing
    a batch. Leaving them behind is harmless but leaks keys until their
    TTL; deleting them is only *safe* because step 1 already committed —
    with the fence in place, a dispatch that races this delete is
    rejected on its stale generation, so removing the guard cannot
    resurrect the job.

    Scoped strictly to this job's key prefix, and swallowing every error:
    Redis is not on the correctness path here.
    """
    pattern = f"dispatched:{job_uuid}:*"
    try:
        redis = get_redis_client()
        keys = list(redis.scan_iter(match=pattern, count=500))
        if keys:
            redis.delete(*keys)
    except Exception:  # noqa: BLE001 - cleanup must never fail a committed cancel
        logger.warning(
            "job_cancel.guard_delete_failed scrape_job_id=%s pattern=%s",
            job_uuid,
            pattern,
            exc_info=True,
        )
