"""The durable authority behind dispatch idempotency (EPA B1, READY-002).

:class:`DispatchIntentStore` is the SQLAlchemy implementation of
:class:`app_shared.scrapyd.identity.DispatchIntentAuthority` — the thing
:class:`~app_shared.scrapyd.client.ScrapydDispatchClient` consults
*before* it touches Redis, and writes to on every state transition.

Transaction ownership — two modes, and why (EPA B2 / F06)
---------------------------------------------------------
**Caller-owned (``DispatchIntentStore(session, ...)``, the original B1
posture).** The store writes into the caller's session and flushes, never
commits; the planner owns the commit. This is what
:func:`app.workers.tasks_dispatch.stamp_targets_dispatched` uses — it
must read the committed intent in the very transaction it stamps
``dispatched_at`` in, so the read and the stamp are atomic.

**Store-owned (``DispatchIntentStore(None, session_factory=...)``, B2).**
Each of :meth:`plan`, :meth:`record_post`, :meth:`confirm`, :meth:`fail`,
:meth:`record_absence` and :meth:`mark_missing` opens its **own short
transaction** from the injected factory and commits it before returning. This is not a
refactor for tidiness: the five-step dispatch protocol requires the
``POSTED`` row to be *committed* before the ``schedule.json`` POST is
issued, and requires the POST itself to run with **no transaction
open** — a long-running planner transaction spanning a network call is
how a killed worker used to lose the only evidence that a POST had
happened, and how a slow node used to pin a Postgres backend for the
whole dispatch loop.

The atomicity B1's docstring worried about is preserved a different
way: ``dispatched_at`` is still written by the single stamping path,
which re-derives proof from the committed intent (a caller-owned store)
in the same transaction as the stamp. What changed is that the *intent*
no longer waits for the planner's commit to become durable — which was
precisely the window F06 names.

The cancellation fence (A2), enforced here
------------------------------------------
Every intent records the ``scrape_jobs.cancellation_generation`` it was
planned under. :meth:`DispatchIntentStore.reconcile` re-reads the job's
*current* generation and refuses the dispatch when they differ, raising
:class:`~app_shared.scrapyd.errors.StaleCancellationGenerationError`
before anything is claimed or POSTed. This is the read side of A2's
"once the database says CANCELLED, nothing can contradict it": the
persistence-side fence already discards late results, so this check is
what stops us *paying* for them.

Absence is evidence, and evidence has a bar (R11, 2026-09-09)
-------------------------------------------------------------
``POSTED`` is committed *before* the ``schedule.json`` POST is issued —
that is the whole point of the protocol — so there is a window in which
the row exists and the node has never heard of the job. The maintenance
sweep used to read one ``listjobs.json`` answer taken in that window as
proof the dispatch never happened, move the row to
``RECONCILED_MISSING``, and thereby authorize a re-POST *and* a second
C3 reservation for work that was at that instant being accepted. The
deterministic ``scrapyd_job_id`` does not save us: Scrapyd 1.6.0 passes
a client-supplied ``jobid`` to the scheduler as ``_job`` **without
deduplicating it**.

So negative answers now go through :meth:`DispatchIntentStore.record_absence`,
which applies :class:`AbsencePolicy` — an in-flight lease, a
corroboration quorum over a window, a horizon past which a node's
bounded history cannot testify — and parks anything short of proof in
``RECONCILED_AMBIGUOUS``, a state that authorizes nothing. Positive
answers are unchanged and unconditional: a sighting is always safe to
act on at any age. And the last gate before a recovery re-POST is
:func:`receiver_holds_execution`, which asks the receiver itself whether
it already holds the execution, because Scrapyd will not.

Scoping: every query goes through
:func:`app_shared.repository.scoped_select` with an explicit
``workspace_id`` (Principle II / ``scripts/check_workspace_scoping.py``),
never relying on the ``app.workspace_id`` GUC alone.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_shared.enums import DispatchIntentState, ScrapeJobStatus
from app_shared.models.dispatch import DispatchIntent
from app_shared.models.jobs import ScrapeJob
from app_shared.repository import scoped_select
from app_shared.scrapyd.errors import StaleCancellationGenerationError
from app_shared.scrapyd.identity import CommittedDispatch, DispatchIdentity

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime cycle
    from app_shared.scrapyd.reconcile import InflightReconciliation

logger = logging.getLogger(__name__)

__all__ = [
    "AbsenceOutcome",
    "AbsencePolicy",
    "AbsenceVerdict",
    "DispatchIntentStore",
    "MIN_CORROBORATING_DENIALS",
    "OPEN_QUESTION_STATES",
    "REPOST_AUTHORIZING_STATES",
    "ReconcileReport",
    "SCRAPYD_JOB_ID_NAMESPACE",
    "deterministic_scrapyd_job_id",
    "iter_dispatch_scrapyd_job_ids",
    "reconcile_inflight_intents",
    "receiver_holds_execution",
]

#: The states that mean "something may exist on a node and nobody has
#: settled it" — the sweep re-examines exactly these, retention never
#: ages them out, and NEITHER of them authorizes a re-POST (R11).
OPEN_QUESTION_STATES: tuple[DispatchIntentState, ...] = (
    DispatchIntentState.POSTED,
    DispatchIntentState.RECONCILED_AMBIGUOUS,
)

#: The states a re-POST may proceed from. Deliberately a named constant
#: rather than an inline check: R11's finding was that "may we send this
#: again?" was answered in three different places with three different
#: readings of the same enum. ``PLANNED`` = never sent;
#: ``RECONCILED_MISSING`` = looked for, corroborated absent.
#: ``RECONCILED_AMBIGUOUS`` is NOT here and must never be added — that is
#: the whole point of its existing.
REPOST_AUTHORIZING_STATES: tuple[DispatchIntentState, ...] = (
    DispatchIntentState.PLANNED,
    DispatchIntentState.RECONCILED_MISSING,
)

#: The uuid5 namespace every deterministic Scrapyd job id is minted under
#: (EPA B2 / F06). A fixed, hard-coded constant on purpose: the whole
#: point of the id is that **any** process, in **any** release, can
#: re-derive the same value from the identity alone — a namespace read
#: from configuration would make that promise environment-dependent, and
#: a re-POST under a changed namespace would double-run the batch it was
#: supposed to dedup against. Never change this value.
SCRAPYD_JOB_ID_NAMESPACE = uuid.UUID("6f3c1e2a-9d47-5b8e-a1c0-4d2f7b6e9a35")


def deterministic_scrapyd_job_id(identity: DispatchIdentity) -> uuid.UUID:
    """The Scrapyd ``jobid`` this identity always gets — ``uuid5`` over its key.

    Chosen by *us*, at plan time, and POSTed as ``schedule.json``'s
    ``jobid`` form field (Scrapyd 1.6 honours a client-supplied jobid
    verbatim). Two properties follow, and both are load-bearing:

    1. **Stable across processes and retries.** A re-POST authorized by
       :attr:`~app_shared.enums.DispatchIntentState.RECONCILED_MISSING`
       carries the same id, so a node that *did* receive the original
       request answers the duplicate instead of running the batch twice.
    2. **Computable without the row.** Recovery can name the run it is
       looking for from the identity alone, which is what makes
       ``list_jobs`` correlation a set-membership test rather than a
       heuristic over spider arguments.
    """
    return uuid.uuid5(SCRAPYD_JOB_ID_NAMESPACE, identity.key)


def iter_dispatch_scrapyd_job_ids(
    session: Session,
    *,
    workspace_id: uuid.UUID | str,
    scrape_job_id: uuid.UUID | str,
) -> list[str]:
    """Every Scrapyd job id recorded for ``scrape_job_id``, oldest first.

    The lookup behind
    :func:`app_shared.jobs.cancellation.iter_known_scrapyd_job_ids`.
    Returns ids from **any** state that has one, not just ``CONFIRMED``:
    a row that reached ``FAILED`` after Scrapyd had already answered is
    still a run somebody should try to stop, and cancelling an id that no
    longer exists is a harmless 200 from the node.
    """
    rows = (
        session.execute(
            scoped_select(DispatchIntent, workspace_id)
            .where(
                DispatchIntent.scrape_job_id == _as_uuid(scrape_job_id),
                DispatchIntent.scrapyd_job_id.is_not(None),
            )
            .order_by(DispatchIntent.created_at)
        )
        .scalars()
        .all()
    )
    seen: dict[str, None] = {}
    for row in rows:
        if row.scrapyd_job_id:
            seen.setdefault(str(row.scrapyd_job_id), None)
    return list(seen)


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# --- R11: when absence is evidence, and when it is merely absence -----------

#: The floor :meth:`DispatchIntentStore.mark_missing` enforces on its own,
#: independent of whatever ``AbsencePolicy`` the sweep was configured
#: with. A configured quorum may be higher; it may never be lower. Two,
#: because one answer cannot distinguish "never arrived" from "arrived
#: and is not indexed yet", and that ambiguity is what R11 is about.
MIN_CORROBORATING_DENIALS = 2

#: How many CONFIRMED rows one pass reads per node to date that node's
#: memory (see :func:`_confirmed_witnesses`). Bounded because the sweep
#: runs every 300s on a fleet-wide table; the newest few hundred are the
#: only ones that can date a memory 100 entries deep anyway.
_WITNESS_SCAN_LIMIT = 500


def _age_seconds(stamp: datetime | None, now: datetime) -> float | None:
    """``now - stamp`` in seconds, or ``None`` when there is no stamp.

    Tolerates a naive ``stamp`` (a hand-repaired row, or a fake session
    that skipped the ``TZDateTime`` round trip) by reading it as UTC:
    refusing to age such a row would push it down the ``None`` branch and
    make it permanently un-settleable, which is a worse failure than
    assuming the timezone every writer in this codebase actually uses.
    """
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp).total_seconds()


class AbsenceVerdict(str, Enum):
    """What one ``listjobs.json`` answer that omitted a run established."""

    #: The intent is not an open question (not ``POSTED`` /
    #: ``RECONCILED_AMBIGUOUS``). Nothing to settle.
    NOT_INFLIGHT = "not_inflight"
    #: The intent is younger than the in-flight lease: the
    #: ``schedule.json`` request may not have reached the node yet, so
    #: this answer is not about it. **Nothing is recorded** — a denial
    #: from before the request could have arrived is not weak evidence,
    #: it is no evidence, and counting it would let a fast enough pass
    #: cadence accumulate a quorum out of pure noise.
    IN_FLIGHT = "in_flight"
    #: We looked and did not see it, but the absence proves nothing: the
    #: node's history has demonstrably rolled, the intent is past the
    #: horizon that history can speak about, or this is the first
    #: uncorroborated denial. The row is ``RECONCILED_AMBIGUOUS``.
    #: **Never authorizes a re-POST.**
    AMBIGUOUS = "ambiguous"
    #: Corroborated absence: past the lease, node history intact, and
    #: ``quorum`` independent denials spanning the absence window. The
    #: row is ``RECONCILED_MISSING`` and a same-id re-POST is authorized.
    MISSING = "missing"


@dataclass(frozen=True)
class AbsenceOutcome:
    """The result of recording one negative observation about an intent."""

    verdict: AbsenceVerdict
    #: The intent's state *after* the observation was recorded.
    state: DispatchIntentState | None
    detail: str
    #: How many independent denials now stand against this intent.
    observations: int = 0

    @property
    def authorizes_repost(self) -> bool:
        """Only :attr:`AbsenceVerdict.MISSING` ever does."""
        return self.verdict is AbsenceVerdict.MISSING


@dataclass(frozen=True)
class AbsencePolicy:
    """The evidence bar a ``RECONCILED_MISSING`` verdict has to clear.

    Every number here exists because the pre-R11 reconciler had none of
    them: it scanned every ``POSTED`` row regardless of age and moved the
    first one a node failed to mention straight to the state that
    authorizes spending money on a second run.

    ``min_age_seconds``
        The in-flight lease. A ``POSTED`` row younger than this may have
        a ``schedule.json`` request still on the wire — the worker
        commits ``POSTED`` *before* it sends, which is the whole point of
        the protocol — so a node that does not list it has not yet been
        asked about it. Must comfortably exceed the dispatch client's own
        HTTP timeout.
    ``quorum`` / ``window_seconds``
        How many independent answers must deny the run, and how far apart
        the first and last must be. One answer cannot distinguish "never
        arrived" from "arrived a moment ago and has not been indexed";
        two, separated by a real interval, can.
    ``horizon_seconds``
        The age past which the node cannot testify at all. Scrapyd's
        ``MemoryJobStorage`` keeps 100 finished entries and loses them on
        restart, so beyond this an absent run is indistinguishable from a
        completed one whose history entry has gone. Such intents are
        ``RECONCILED_AMBIGUOUS`` forever, for an operator, rather than
        re-POSTed on evidence nobody has.
    """

    min_age_seconds: float = 120.0
    quorum: int = 2
    window_seconds: float = 120.0
    horizon_seconds: float = 86_400.0

    @classmethod
    def from_settings(cls, settings: Any) -> "AbsencePolicy":
        """Read the four knobs off ``settings``, falling back to defaults.

        ``getattr`` with a default rather than attribute access: the
        reconciler is also driven from tests and operator scripts with a
        ``SimpleNamespace`` that carries only the Scrapyd fields, and a
        missing knob must degrade to the conservative default rather than
        crash a sweep.
        """
        if settings is None:
            return cls()
        return cls(
            min_age_seconds=float(
                getattr(settings, "DISPATCH_RECONCILE_MIN_AGE_SECONDS", 120)
            ),
            quorum=int(getattr(settings, "DISPATCH_RECONCILE_ABSENCE_QUORUM", 2)),
            window_seconds=float(
                getattr(settings, "DISPATCH_RECONCILE_ABSENCE_WINDOW_SECONDS", 120)
            ),
            horizon_seconds=float(
                getattr(settings, "DISPATCH_RECONCILE_ABSENCE_HORIZON_SECONDS", 86_400)
            ),
        )


def receiver_holds_execution(
    client: Any,
    *,
    node_url: str,
    node_class: str,
    scrapyd_job_id: str,
) -> bool | None:
    """Does the receiver already hold an execution under this name? (R11)

    The receiver-side half of execution identity, and it has to be ours:
    **Scrapyd 1.6.0's ``Schedule.render_POST`` passes the client-supplied
    ``jobid`` through to the scheduler as ``_job`` without deduplicating
    it** (``webservice.py``; the ``@param("jobid", ...)`` declaration does
    nothing but default it). A second ``schedule.json`` carrying the same
    id therefore does not collapse onto the first — it queues a second
    run with a colliding name, which is strictly worse than a plain
    double-run because the two are then indistinguishable in
    ``listjobs.json``. The deterministic id makes a re-POST *traceable*;
    it does not make it *idempotent*. This check is what does.

    Called immediately before a recovery re-POST, against the one node
    the intent recorded, with no transaction open.

    Returns:
        ``True`` the node lists this id — do NOT POST, adopt the run.
        ``False`` the node answered and does not know it — the re-POST
        the ``RECONCILED_MISSING`` verdict authorized may proceed.
        ``None`` the node could not be asked. Absence of an answer is
        not an answer: the caller must refuse to POST, exactly as the
        sweep refuses to issue a verdict.
    """
    if not node_url:
        return None
    project = node_class.split(":", 1)[0] if node_class else None
    try:
        known = client.list_jobs(node_url, project)
    except Exception as exc:  # noqa: BLE001 - "unknown" is a verdict, not a crash
        logger.warning(
            "dispatch.receiver_check_unreachable node_url=%s jobid=%s error=%r",
            node_url,
            scrapyd_job_id,
            exc,
        )
        return None
    if known is None:
        return None
    return str(scrapyd_job_id) in {str(value) for value in known}


class DispatchIntentStore:
    """One job's ``dispatch_intents`` rows, for the duration of one transaction.

    Constructed by the planner with the job it is planning. Bound to a
    single ``(workspace_id, scrape_job_id)`` on purpose: an authority that
    could be pointed at any job would need every caller to re-assert the
    tenant boundary on every call, and one forgetting is a cross-tenant
    read.
    """

    def __init__(
        self,
        session: Session | None = None,
        *,
        workspace_id: uuid.UUID | str,
        scrape_job_id: uuid.UUID | str,
        authorized_cancellation_generation: int = 0,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise ValueError(
                "DispatchIntentStore needs either a session (caller-owned "
                "transaction) or a session_factory (store-owned short "
                "transactions); it was given neither"
            )
        self._session = session
        #: When set, every mutating method runs in its own short
        #: transaction and commits it (see the module docstring). The
        #: five-step dispatch protocol depends on this: the POST must be
        #: issued with no transaction open, which is only true if the
        #: ``POSTED`` write already committed and closed.
        self._session_factory = session_factory
        self._workspace_id = _as_uuid(workspace_id)
        self._scrape_job_id = _as_uuid(scrape_job_id)
        #: The job's ``cancellation_generation`` as read when this plan was
        #: authorized. Stamped onto every intent this store creates, and
        #: the fallback comparison for an identity with no row yet.
        self._authorized_generation = int(authorized_cancellation_generation or 0)

    @property
    def owns_transactions(self) -> bool:
        """True when this store commits its own writes (B2's protocol mode)."""
        return self._session_factory is not None

    @contextmanager
    def _txn(self) -> Iterator[Session]:
        """The session one store operation runs in.

        Caller-owned mode yields the caller's session untouched — no
        commit, no rollback, no close: the caller's transaction is the
        caller's business. Store-owned mode opens a session from the
        factory, commits it on success, rolls it back on failure and
        always closes it.

        **The factory's contract is that the session it returns can
        already see this workspace's rows** — i.e. it has had
        ``set_workspace_context`` applied, or it is the BYPASSRLS system
        session. Scoping is deliberately the injector's job rather than
        this store's: the sweep in :func:`reconcile_inflight_intents` is
        cross-tenant by construction and the planner's is not, and a
        store that re-asserted a GUC would be quietly wrong for one of
        them.
        """
        if self._session_factory is None:
            assert self._session is not None  # guaranteed by __init__
            yield self._session
            return

        # `with factory() as session` — the same seam shape
        # `app_shared.outbox.dispatcher.drain_outbox` uses, and satisfied
        # by a plain `sessionmaker` (a `Session` is itself a context
        # manager that closes on exit) as well as by a
        # `@contextmanager`-decorated opener.
        with self._session_factory() as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    # --- planning ------------------------------------------------------------

    def plan(
        self,
        identity: DispatchIdentity,
        *,
        match_ids: Iterable[object],
        batch_index: object = None,
        node_url: str = "",
    ) -> DispatchIntent:
        """Create (or return) the ``PLANNED`` intent for ``identity``.

        Get-or-create keyed on ``identity_key``, which is UNIQUE in the
        database: a replayed planning pass that re-derives the same
        identity re-uses the same row rather than racing a second one into
        a constraint violation.

        Step 1 of the five-step protocol (F06). Two things are decided
        *here*, before anything can be sent, and never afterwards:

        * ``scrapyd_job_id`` — :func:`deterministic_scrapyd_job_id` over
          the identity. The name of the remote run is a property of the
          work, not of the attempt, which is what lets a re-POST be safe.
        * ``node_url`` — the pool member ``select_node`` chose. Recovery
          asks *that* node whether the run exists.

        In store-owned mode this commits before returning, so the plan is
        durable before the POST it authorizes; in caller-owned mode it
        flushes and the planner owns the commit (see the module
        docstring).
        """
        with self._txn() as session:
            existing = self._load(identity, session)
            if existing is not None:
                if node_url and not existing.node_url:
                    # A row planned before B2 (or by a caller that did not
                    # know its node yet) learns its node without changing
                    # its identity or its id.
                    existing.node_url = node_url
                    session.flush()
                return existing

            intent = DispatchIntent(
                workspace_id=self._workspace_id,
                scrape_job_id=self._scrape_job_id,
                planning_generation=identity.planning_generation,
                strategy_method=identity.strategy_method,
                domain=identity.domain,
                mode=identity.mode,
                node_class=identity.node_class,
                match_ids_digest=identity.work_digest,
                match_ids=[str(match_id) for match_id in match_ids],
                identity_key=identity.key,
                identity_payload=identity.canonical_payload,
                node_url=node_url or "",
                scrapyd_job_id=deterministic_scrapyd_job_id(identity),
                state=DispatchIntentState.PLANNED,
                cancellation_generation_at_creation=self._authorized_generation,
                batch_index=None if batch_index is None else str(batch_index),
            )
            session.add(intent)
            session.flush()
            return intent

    # --- DispatchIntentAuthority ---------------------------------------------

    def reconcile(self, identity: DispatchIdentity) -> CommittedDispatch | None:
        """The committed dispatch for ``identity``, or ``None`` — fence first.

        Raises:
            StaleCancellationGenerationError: the job has been cancelled
                since this work was authorized (see the module docstring).
        """
        with self._txn() as session:
            intent = self._load(identity, session)
            authorized = (
                intent.cancellation_generation_at_creation
                if intent is not None
                else self._authorized_generation
            )
            self._assert_fence_current(identity, authorized, session)

            if intent is None or intent.state != DispatchIntentState.CONFIRMED:
                return None
            if not intent.scrapyd_job_id:
                # CONFIRMED without an id cannot happen through `confirm`,
                # but a hand-repaired row could look like this. Refusing
                # to answer is safer than answering with nothing.
                return None
            return CommittedDispatch(
                jobid=str(intent.scrapyd_job_id),
                identity_payload=str(intent.identity_payload),
                intent_id=str(intent.id),
                committed_at=(
                    intent.confirmed_at.isoformat()
                    if intent.confirmed_at is not None
                    else None
                ),
                source="dispatch_intents",
            )

    def reconcile_inflight(
        self,
        identity: DispatchIdentity,
        *,
        settings: Any,
        lister: Any = None,
        redis: Any = None,
    ) -> "InflightReconciliation | None":
        """Resolve a ``POSTED`` intent for ``identity`` before a retry claims a slot.

        The dispatch-path half of EPA B3b:
        :class:`~app_shared.scrapyd.client.ScrapydDispatchClient` step 0b
        calls this, through the ``DispatchIntentAuthority`` seam, so it
        never has to hold a session itself (the client stays a plain
        ``requests`` + ``redis`` object — see its module docstring).

        ``None`` when there is no durable intent for ``identity``, or it is
        not currently ``POSTED``: nothing to reconcile, and the caller's
        ordinary reconcile/claim/POST path applies unchanged. Otherwise
        delegates to :func:`app_shared.scrapyd.reconcile.reconcile_inflight`
        (EPA B3) against this store's own session and workspace, so the two
        writers are never in different transactions.

        The import is local to break an import cycle:
        :mod:`app_shared.scrapyd.reconcile` imports
        :class:`DispatchIntentStore` (to adopt a found run through the same
        :meth:`confirm` this dispatch path uses), so this module cannot
        import it back at module scope.
        """
        from app_shared.scrapyd.reconcile import (
            InflightReconciliation,
            InflightVerdict,
        )
        from app_shared.scrapyd.reconcile import reconcile_inflight as _reconcile_inflight

        policy = AbsencePolicy.from_settings(settings)
        with self._txn() as session:
            intent = self._load(identity, session)
            if intent is None or intent.state not in OPEN_QUESTION_STATES:
                return None

            if intent.state == DispatchIntentState.RECONCILED_AMBIGUOUS:
                # R11: the maintenance sweep already looked and could not
                # establish anything. The dispatch path must not quietly
                # get a second, softer answer to the same question — if it
                # asked the node again and the run were merely un-indexed,
                # `reconcile_inflight` would report ABSENT and roll the
                # row back to PLANNED, which is precisely the re-POST
                # authorization the ambiguous verdict withheld. Refuse.
                return InflightReconciliation(
                    verdict=InflightVerdict.AMBIGUOUS,
                    intent_id=str(intent.id),
                    scrapyd_job_id=None,
                    state=intent.state,
                    mechanism="none",
                    detail=(
                        "the reconciler looked and could not establish whether "
                        f"this dispatch reached the node ({intent.error_message}); "
                        "refusing to re-POST on an unresolved question"
                    ),
                )

            age = _age_seconds(intent.posted_at, datetime.now(timezone.utc))
            if age is not None and age < policy.min_age_seconds:
                # R11, the dispatch-path half of the same race: an
                # at-least-once redelivery arriving seconds behind the
                # original would ask the node about a `schedule.json`
                # request still on the wire, be told "no such job", and
                # take that as authorization to send it again.
                return InflightReconciliation(
                    verdict=InflightVerdict.AMBIGUOUS,
                    intent_id=str(intent.id),
                    scrapyd_job_id=None,
                    state=intent.state,
                    mechanism="none",
                    detail=(
                        f"the POST for this intent is {age:.0f}s old, inside the "
                        f"{policy.min_age_seconds:.0f}s in-flight lease; the node "
                        "cannot yet have been asked about it"
                    ),
                )

            return _reconcile_inflight(
                session,
                intent.id,
                workspace_id=self._workspace_id,
                settings=settings,
                lister=lister,
                redis=redis,
            )

    def planned_scrapyd_job_id(self, identity: DispatchIdentity) -> str | None:
        """The remote run's name for ``identity``, or ``None`` if unplanned.

        Read by :class:`~app_shared.scrapyd.client.ScrapydDispatchClient`
        just before the POST so that the id it sends and the id the row
        records are the same value by construction. They must be: an id
        we POSTed that disagrees with the row is a run
        :func:`reconcile_inflight_intents` will look for and not find,
        and "not found" is the one verdict that authorizes a re-POST.
        """
        intent = self._load_read_only(identity)
        if intent is None or not intent.scrapyd_job_id:
            return None
        return str(intent.scrapyd_job_id)

    def planned_node_url(self, identity: DispatchIdentity) -> str | None:
        """The node this identity was already placed on, or ``None``.

        EPA B6: placement is decided **once**, in the pass that created
        the row, and every later attempt at the same identity reads it
        back from here instead of re-running :func:`choose_node`. A retry
        that re-chose would be free to pick a different node than the one
        the (possibly already POSTed) run is sitting on, which is exactly
        the double-run `reconcile_inflight_intents` cannot untangle: it
        asks ONE node — the one the row records — whether the run exists.

        Returns ``None`` for an identity with no row yet (a genuinely new
        batch, which must choose) and for a pre-B2 row that recorded no
        node.
        """
        intent = self._load_read_only(identity)
        if intent is None or not intent.node_url:
            return None
        return str(intent.node_url)

    def record_post(self, identity: DispatchIdentity, *, node_url: str = "") -> str:
        """Mark the intent ``POSTED`` **before** the network call.

        Step 2 of the five-step protocol. Ordering matters: a worker
        killed mid-POST must leave evidence that a run may exist on the
        node. ``POSTED`` is the one genuinely ambiguous state, and
        recording it — *committed*, in store-owned mode, before the POST
        is issued — is how the ambiguity becomes something
        :func:`reconcile_inflight_intents` can settle instead of
        something nobody knows to look for.
        """
        with self._txn() as session:
            intent = self._load(identity, session)
            if intent is None:
                # No row yet (a caller that skipped `plan`): create one in
                # this same transaction rather than a nested one, so the
                # POSTED write is never split across two commits.
                intent = self._create(identity, session, match_ids=(), node_url=node_url)
            if node_url and intent.node_url != node_url:
                intent.node_url = node_url
            intent.state = DispatchIntentState.POSTED
            intent.posted_at = datetime.now(timezone.utc)
            session.flush()
            return str(intent.id)

    def confirm(self, identity: DispatchIdentity, scrapyd_job_id: str) -> None:
        """Record Scrapyd's answer and mark the intent ``CONFIRMED``.

        Step 4 of the five-step protocol. The id was already chosen at
        plan time and POSTed, and Scrapyd 1.6 echoes it back verbatim, so
        the normal case writes nothing new. A node that ignored the
        ``jobid`` field (an older build, or ``SCRAPYD_DETERMINISTIC_JOBID``
        off) answers with an id of its own; that answer is adopted when it
        parses as a UUID, and otherwise logged and discarded — the column
        is the *name of the run*, and a name we cannot store is worse than
        the deterministic one we already POSTed under.
        """
        with self._txn() as session:
            intent = self._load(identity, session)
            if intent is None:
                intent = self._create(identity, session, match_ids=())
            answered = _coerce_job_uuid(scrapyd_job_id)
            if answered is None:
                logger.warning(
                    "dispatch.confirm_non_uuid_jobid intent=%s answered=%r "
                    "keeping=%s",
                    intent.id,
                    str(scrapyd_job_id)[:100],
                    intent.scrapyd_job_id,
                )
            elif answered != intent.scrapyd_job_id:
                intent.scrapyd_job_id = answered
            now = datetime.now(timezone.utc)
            intent.state = DispatchIntentState.CONFIRMED
            intent.confirmed_at = now
            intent.error_message = None
            # R11: a confirmation IS a sighting — either a node listed the
            # run or Scrapyd answered ``status=ok`` for it. Recording when
            # is what makes a *later* absence a disappearance (the node's
            # 100-deep, restart-losing history) rather than proof that the
            # work never happened. Reaching CONFIRMED also retires any
            # absence the sweep had accumulated: the row is settled, and
            # leaving stale denials on it would mislead the next operator
            # to read it.
            intent.last_seen_at = now
            intent.absent_observations = 0
            intent.first_absent_at = None
            session.flush()

    def fail(self, identity: DispatchIdentity, error: str) -> None:
        """Mark the intent ``FAILED``; the Redis claim is released separately.

        The row is kept, never deleted: "tried and failed" must stay
        distinguishable from "never tried", which is the whole reason the
        pre-B1 release step (a bare ``DELETE`` of the Redis key) left
        operators unable to tell a wedged batch from an unplanned one.

        Only reachable when the failure is *known* to have happened
        before the POST left (the client's own release path). A worker
        that simply dies after :meth:`record_post` never gets here — its
        row stays ``POSTED`` for :func:`reconcile_inflight_intents`,
        which is exactly the distinction F06 asks for.
        """
        with self._txn() as session:
            intent = self._load(identity, session)
            if intent is None:
                return
            intent.state = DispatchIntentState.FAILED
            intent.error_message = error[:2000]
            session.flush()

    def current_state(self, identity: DispatchIdentity) -> DispatchIntentState | None:
        """This identity's durable state, or ``None`` when it has no row.

        R11: the planner needs this *before* it authorizes anything.
        A batch whose intent is an open question (``POSTED`` or
        ``RECONCILED_AMBIGUOUS``) must not be re-planned at all — not
        because the POST would be wrong (the guard and the reconcile
        gate both refuse it) but because the C3 grant is reserved
        **before** any of those gates run, and a reservation taken for a
        dispatch that is then refused is a second live hold against the
        same logical batch. One authorization per logical job means
        asking the ledger first.
        """
        intent = self._load_read_only(identity)
        return None if intent is None else intent.state

    def record_absence(
        self,
        identity: DispatchIdentity,
        *,
        now: datetime | None = None,
        policy: "AbsencePolicy | None" = None,
        node_url: str = "",
        history_intact: bool = True,
        detail: str = "",
    ) -> AbsenceOutcome:
        """Record one ``listjobs.json`` answer that did NOT list this run.

        R11's core. The pre-R11 reconciler had no method like this: it
        called :meth:`mark_missing` directly on the first denial, at any
        age, from any node, which made "one node did not mention it once"
        indistinguishable from "we established the work never left". The
        difference matters because only the second may authorize spending
        — a re-POST is a second run and a second cost authorization.

        The ladder, in the order the checks have to happen:

        1. **Not an open question** -> :attr:`AbsenceVerdict.NOT_INFLIGHT`.
           Nothing is written. A ``CONFIRMED``/``FAILED``/``PLANNED`` row
           is not the sweep's business.
        2. **Younger than the lease** -> :attr:`AbsenceVerdict.IN_FLIGHT`,
           and *nothing is recorded*. This is the exact race R11 names:
           the worker commits ``POSTED`` before it sends, so a pass that
           runs in that window asks the node about a request that has not
           arrived. The answer is not weak evidence; it is evidence about
           a different question.
        3. **The node's history is not intact** (a run it certainly
           accepted is no longer listed — it restarted, or the 100-deep
           finished buffer rolled) -> :attr:`AbsenceVerdict.AMBIGUOUS`.
           A recently disappeared history entry is not proof the work
           never happened.
        4. **Past the horizon** -> :attr:`AbsenceVerdict.AMBIGUOUS`, and
           permanently: no node can testify about a run that old.
        5. Otherwise the denial is recorded, and the intent becomes
           ``RECONCILED_MISSING`` only once ``quorum`` denials span
           ``window_seconds``. Until then it is ``RECONCILED_AMBIGUOUS``.

        Args:
            now: the pass clock. Injected, never read from the wall here,
                so a test can drive the lease and the window exactly.
            policy: the evidence bar; :class:`AbsencePolicy` defaults when
                omitted.
            node_url: the node that answered, for the audit trail.
            history_intact: False when the caller established that this
                node has forgotten runs it definitely accepted (see
                :func:`reconcile_inflight_intents`).
            detail: the caller's description of the answer.
        """
        policy = policy or AbsencePolicy()
        now = now or datetime.now(timezone.utc)
        with self._txn() as session:
            intent = self._load(identity, session)
            if intent is None or intent.state not in OPEN_QUESTION_STATES:
                return AbsenceOutcome(
                    verdict=AbsenceVerdict.NOT_INFLIGHT,
                    state=None if intent is None else intent.state,
                    detail="the intent is not an open question; nothing to settle",
                )

            age = _age_seconds(intent.posted_at, now)
            if age is None or age < policy.min_age_seconds:
                # Step 2. Deliberately BEFORE any write: an observation
                # taken inside the lease must leave no trace at all, or a
                # tight enough pass cadence would build a quorum out of
                # answers that were never about this request.
                return AbsenceOutcome(
                    verdict=AbsenceVerdict.IN_FLIGHT,
                    state=intent.state,
                    detail=(
                        f"intent is {'un-aged' if age is None else f'{age:.0f}s'} old, "
                        f"inside the {policy.min_age_seconds:.0f}s in-flight lease; "
                        "the schedule.json request may not have arrived yet"
                    ),
                    observations=int(intent.absent_observations or 0),
                )

            if not history_intact or intent.last_seen_at is not None:
                return self._ambiguous(
                    session,
                    intent,
                    detail
                    or (
                        f"node {node_url} has forgotten runs it accepted "
                        "(restart, or the 100-deep finished history rolled); "
                        "its silence about this one proves nothing"
                    ),
                )

            if age > policy.horizon_seconds:
                return self._ambiguous(
                    session,
                    intent,
                    f"intent is {age:.0f}s old, past the "
                    f"{policy.horizon_seconds:.0f}s horizon a node's bounded "
                    "finished history can testify about; needs an operator",
                )

            # Step 5. The denial counts.
            if intent.first_absent_at is None:
                intent.first_absent_at = now
            intent.absent_observations = int(intent.absent_observations or 0) + 1
            observations = int(intent.absent_observations)
            spanned = _age_seconds(intent.first_absent_at, now) or 0.0
            if observations >= policy.quorum and spanned >= policy.window_seconds:
                intent.state = DispatchIntentState.RECONCILED_MISSING
                intent.error_message = (
                    detail
                    or f"node {node_url} answered listjobs.json and does not know it"
                )[:1900] + (
                    f" [{observations} denials over {spanned:.0f}s]"
                )
                session.flush()
                return AbsenceOutcome(
                    verdict=AbsenceVerdict.MISSING,
                    state=intent.state,
                    detail=str(intent.error_message),
                    observations=observations,
                )

            return self._ambiguous(
                session,
                intent,
                f"{observations} denial(s) over {spanned:.0f}s; "
                f"{policy.quorum} spanning {policy.window_seconds:.0f}s are "
                "required before absence authorizes a re-POST",
                observations=observations,
            )

    def _ambiguous(
        self,
        session: Session,
        intent: DispatchIntent,
        detail: str,
        observations: int = 0,
    ) -> AbsenceOutcome:
        """Park an intent in ``RECONCILED_AMBIGUOUS`` and say why.

        A single writer for the state, so "we looked and learned nothing"
        always reads the same way in the table and can never be spelled
        as ``RECONCILED_MISSING`` by a caller in a hurry.
        """
        intent.state = DispatchIntentState.RECONCILED_AMBIGUOUS
        intent.error_message = detail[:2000]
        session.flush()
        return AbsenceOutcome(
            verdict=AbsenceVerdict.AMBIGUOUS,
            state=intent.state,
            detail=detail,
            observations=observations or int(intent.absent_observations or 0),
        )

    def mark_missing(self, identity: DispatchIdentity, detail: str = "") -> None:
        """Move an open-question intent to ``RECONCILED_MISSING`` — re-POST allowed.

        Step 5's only positive outcome, and the ONE transition in this
        module that authorizes spending: the re-POST it clears is a
        second Scrapyd run and, upstream, a second C3 reservation.

        **The evidence gate is enforced here, not only in the caller**
        (R11). Before this, the method moved any ``POSTED`` row on the
        strength of its argument alone, so the sweep's judgement was the
        only thing between an in-flight request and a duplicate run — and
        that judgement did not exist. It now refuses unless the row's own
        absence ledger says the denial was corroborated: at least two
        recorded denials, and the ledger is only ever written by
        :meth:`record_absence`, which will not record one inside the
        in-flight lease. A caller that wants the ladder (and the
        ``RECONCILED_AMBIGUOUS`` outcomes) should call
        :meth:`record_absence`; this stays the single writer of the
        terminal transition.

        The re-POST that this state authorizes carries the **same**
        ``scrapyd_job_id``, and — since Scrapyd 1.6.0 does not dedup
        ``_job`` — must additionally pass
        :func:`receiver_holds_execution` immediately before it is sent.
        """
        with self._txn() as session:
            intent = self._load(identity, session)
            if intent is None or intent.state not in OPEN_QUESTION_STATES:
                return
            if int(intent.absent_observations or 0) < MIN_CORROBORATING_DENIALS:
                logger.warning(
                    "dispatch.mark_missing_refused intent=%s observations=%s "
                    "state=%s reason=uncorroborated",
                    intent.id,
                    intent.absent_observations,
                    intent.state,
                )
                return
            intent.state = DispatchIntentState.RECONCILED_MISSING
            intent.error_message = (detail or "no such job on any node in the pool")[
                :2000
            ]
            session.flush()

    # --- internals -----------------------------------------------------------

    def _create(
        self,
        identity: DispatchIdentity,
        session: Session,
        *,
        match_ids: Iterable[object],
        node_url: str = "",
    ) -> DispatchIntent:
        """Insert a ``PLANNED`` row for ``identity`` in ``session``.

        The body of :meth:`plan`, factored out so the other transitions
        can create a missing row **inside their own transaction** rather
        than opening a nested one.
        """
        intent = DispatchIntent(
            workspace_id=self._workspace_id,
            scrape_job_id=self._scrape_job_id,
            planning_generation=identity.planning_generation,
            strategy_method=identity.strategy_method,
            domain=identity.domain,
            mode=identity.mode,
            node_class=identity.node_class,
            match_ids_digest=identity.work_digest,
            match_ids=[str(match_id) for match_id in match_ids],
            identity_key=identity.key,
            identity_payload=identity.canonical_payload,
            node_url=node_url or "",
            scrapyd_job_id=deterministic_scrapyd_job_id(identity),
            state=DispatchIntentState.PLANNED,
            cancellation_generation_at_creation=self._authorized_generation,
            batch_index=None,
        )
        session.add(intent)
        session.flush()
        return intent

    def _load_read_only(self, identity: DispatchIdentity) -> DispatchIntent | None:
        """:meth:`_load` in whichever session mode this store is in.

        A read needs a transaction too; in store-owned mode it gets its
        own short one (committed and closed like every other), so a
        lookup never leaves a transaction hanging over the POST.
        """
        with self._txn() as session:
            return self._load(identity, session)

    def _load(
        self, identity: DispatchIdentity, session: Session | None = None
    ) -> DispatchIntent | None:
        target = session if session is not None else self._session
        assert target is not None  # every path supplies one
        return target.execute(
            scoped_select(DispatchIntent, self._workspace_id).where(
                DispatchIntent.scrape_job_id == self._scrape_job_id,
                DispatchIntent.identity_key == identity.key,
            )
        ).scalar_one_or_none()

    def _assert_fence_current(
        self,
        identity: DispatchIdentity,
        authorized: int,
        session: Session | None = None,
    ) -> None:
        """A2's fence: refuse work authorized before a cancellation."""
        target = session if session is not None else self._session
        assert target is not None  # every path supplies one
        job = target.execute(
            scoped_select(ScrapeJob, self._workspace_id).where(
                ScrapeJob.id == self._scrape_job_id
            )
        ).scalar_one_or_none()
        if job is None:
            # The job is not visible in this workspace. Never POST for it.
            raise StaleCancellationGenerationError(
                f"scrape job {self._scrape_job_id} is not visible in workspace "
                f"{self._workspace_id}; refusing to dispatch {identity.key!r}"
            )
        current = int(job.cancellation_generation or 0)
        if current != int(authorized or 0):
            raise StaleCancellationGenerationError(
                f"dispatch intent {identity.key!r} was authorized under cancellation "
                f"generation {authorized}, but scrape job {self._scrape_job_id} is now "
                f"at generation {current}; refusing to dispatch cancelled work"
            )
        if job.status == ScrapeJobStatus.CANCELLED:
            # Belt and braces: the generation bump and the status change
            # are written together, so this can only fire if one of them
            # was applied by hand. Refuse anyway — the status is the
            # human-readable half of the same fence.
            raise StaleCancellationGenerationError(
                f"scrape job {self._scrape_job_id} is CANCELLED; refusing to dispatch "
                f"{identity.key!r}"
            )


def _coerce_job_uuid(value: object) -> uuid.UUID | None:
    """``value`` as a UUID, or ``None`` when it is not one.

    Scrapyd 1.6 mints ``uuid1().hex`` (32 hex chars, no dashes) when it
    is not given a ``jobid``; :class:`uuid.UUID` accepts that spelling,
    so a node-minted answer round-trips into the column unchanged.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass(frozen=True)
class ReconcileReport:
    """What one :func:`reconcile_inflight_intents` pass established."""

    #: Open-question rows (``POSTED`` / ``RECONCILED_AMBIGUOUS``) examined.
    examined: int = 0
    #: Rows a node listed — advanced to ``CONFIRMED``. Never re-POSTed.
    confirmed: int = 0
    #: Rows whose absence was CORROBORATED — advanced to
    #: ``RECONCILED_MISSING``. These, and only these, may be re-POSTed
    #: (with the same ``scrapyd_job_id``, and only after
    #: :func:`receiver_holds_execution` says the node does not already
    #: hold the run).
    missing: int = 0
    #: Rows left ``POSTED`` because a node could not be reached. No
    #: verdict; they are examined again next pass.
    unreachable: int = 0
    #: R11: rows still inside the in-flight lease. The node was asked (a
    #: sighting is always safe to act on) but a denial was DISCARDED, not
    #: recorded — the ``schedule.json`` request may not have arrived yet.
    #: The row keeps its state and is examined again next pass.
    in_flight: int = 0
    #: R11: rows a node denied where the denial proves nothing — the
    #: node's history has rolled, the intent is past the horizon, or the
    #: denial is not yet corroborated. Now ``RECONCILED_AMBIGUOUS``.
    #: **Never re-POSTed.**
    ambiguous: int = 0
    #: ``intent_id`` values now in ``RECONCILED_MISSING``, for the caller
    #: that wants to re-POST them immediately rather than next tick.
    reposted_candidates: tuple[str, ...] = field(default_factory=tuple)


def reconcile_inflight_intents(
    session_factory: Callable[[], Any],
    client: Any,
    *,
    workspace_id: uuid.UUID | str | None = None,
    scrape_job_id: uuid.UUID | str | None = None,
    limit: int = 200,
    now: datetime | None = None,
    settings: Any = None,
    policy: AbsencePolicy | None = None,
) -> ReconcileReport:
    """Settle every open-question dispatch intent against the node it names.

    Step 5 of the five-step protocol, and the reason step 3 is allowed to
    die without taking the system's knowledge with it. For each
    ``POSTED`` or ``RECONCILED_AMBIGUOUS`` row this asks
    ``client.list_jobs(node_url, project)`` — the node the row itself
    recorded, and the project half of its ``node_class`` — and then:

    * the node lists ``scrapyd_job_id``  -> :meth:`DispatchIntentStore.confirm`
      (``CONFIRMED``). **Never re-POST**: the run exists. A sighting is
      acted on at ANY age — it is the negative answer that needs a
      policy, never the positive one.
    * the node cannot be reached (``list_jobs`` raises or returns
      ``None``) -> the row is left as it was. Absence of an answer is
      not evidence of absence; it is examined again next pass.
    * the node answers and does not list it -> the answer goes to
      :meth:`DispatchIntentStore.record_absence`, which decides between
      "not evidence" (``RECONCILED_AMBIGUOUS``, or discarded entirely
      inside the in-flight lease) and "evidence"
      (``RECONCILED_MISSING``, the only state a re-POST may proceed
      from). See :class:`AbsencePolicy` for the bar and R11 for why a
      bar is needed at all.

    What R11 changed, and why (2026-09-09)
    --------------------------------------
    This function used to ``del now`` — it discarded its own clock — and
    scan every ``POSTED`` row with no minimum age, calling
    ``mark_missing`` on the first node answer that omitted the job. Every
    piece of that was load-bearing in the wrong direction, because the
    protocol deliberately commits ``POSTED`` **before** the
    ``schedule.json`` POST is issued:

        worker commits POSTED -> this sweep lists the node before the
        schedule request arrives -> the job is (correctly) not there ->
        RECONCILED_MISSING -> the original POST then succeeds

    and the row now says a re-POST is authorized for work that is
    running. A re-POST is not free even with a deterministic id: Scrapyd
    1.6.0's schedule handler hands ``jobid`` to the scheduler as ``_job``
    **without deduplicating it**, so the node queues a second run under a
    colliding name; and upstream, the re-plan takes a second C3
    reservation against the workspace's budget. One logical job, two
    executions, two cost authorizations.

    Three things close it, and all three are needed:

    1. **A lease.** ``now`` is real again and a row younger than
       ``policy.min_age_seconds`` yields no negative verdict at all.
    2. **Corroboration, in an explicit ambiguous state.** One denial is
       recorded, not acted on; ``RECONCILED_AMBIGUOUS`` is where a row
       waits, and it authorizes nothing.
    3. **A node-history check.** Both nodes run ``MemoryJobStorage``
       (100 finished entries, lost on restart). Before declaring a run
       absent, this pass verifies the node still lists every run it
       CONFIRMED *after* this one was posted. If even one of those has
       disappeared, the node has forgotten something newer than the
       intent under test, so its silence about the intent proves nothing
       — a recently disappeared history entry is not evidence the work
       never happened.

    Each row is settled through a :class:`DispatchIntentStore` built on
    ``session_factory``, so each verdict commits in its own short
    transaction and a failure on row 40 cannot roll back rows 1-39.

    Args:
        session_factory: opens one session per short transaction, already
            able to see the rows it will touch (the BYPASSRLS system
            sessionmaker for the cross-tenant sweep). The scan itself
            uses one too, and closes it before any verdict is written —
            the node calls must not run with a transaction open.
        client: anything exposing ``list_jobs(node_url, project) ->
            set[str]`` (:class:`~app_shared.scrapyd.client.ScrapydDispatchClient`
            in production, a fake node in tests).
        workspace_id: restrict the sweep to one tenant. ``None`` sweeps
            every workspace — a registered system sweep, which is why the
            scan query is annotated ``# noqa: workspace-scope``.
        scrape_job_id: restrict the sweep to one job.
        limit: maximum rows examined in one pass.
        now: the pass clock. Defaults to the wall clock; injected by
            tests and by the Celery task so every age in one pass is
            measured against the same instant.
        settings: source of the four ``DISPATCH_RECONCILE_ABSENCE_*``
            knobs when ``policy`` is not given.
        policy: the evidence bar, pre-built. Overrides ``settings``.

    Returns:
        ReconcileReport: counts plus the ids now safe to re-POST.
    """
    now = now or datetime.now(timezone.utc)
    policy = policy or AbsencePolicy.from_settings(settings)

    # --- scan: read the open-question rows, then CLOSE the transaction ---
    # The node calls below must not run with a transaction open (the same
    # rule step 3 obeys), so the scan is its own short read. The CONFIRMED
    # witnesses are read here too, in the same transaction, for the same
    # reason -- and because a witness read after the listing would be
    # racing the very thing it is meant to date-stamp.
    with session_factory() as scan:
        query = select(DispatchIntent).where(  # noqa: workspace-scope - system sweep
            DispatchIntent.state.in_(OPEN_QUESTION_STATES)
        )
        if workspace_id is not None:
            query = query.where(DispatchIntent.workspace_id == _as_uuid(workspace_id))
        if scrape_job_id is not None:
            query = query.where(DispatchIntent.scrape_job_id == _as_uuid(scrape_job_id))
        rows = (
            scan.execute(query.order_by(DispatchIntent.posted_at).limit(limit))
            .scalars()
            .all()
        )
        pending = [
            (
                row.workspace_id,
                row.scrape_job_id,
                str(row.id),
                str(row.node_url or ""),
                str(row.node_class or ""),
                str(row.scrapyd_job_id),
                row.posted_at,
                identity_from_intent_row(row),
            )
            for row in rows
        ]
        witnesses = _confirmed_witnesses(
            scan, {node_url for _w, _j, _i, node_url, *_rest in pending if node_url}
        )
        scan.commit()

    examined = confirmed = missing = unreachable = in_flight = ambiguous = 0
    candidates: list[str] = []
    #: One listing per node per pass. Two intents on the same node must
    #: be judged against the SAME answer -- otherwise the history check
    #: and the denials it gates could disagree within one sweep.
    listings: dict[tuple[str, str | None], set[str] | None] = {}

    for ws_id, job_id, intent_id, node_url, node_class, jobid, posted_at, identity in (
        pending
    ):
        examined += 1
        project = node_class.split(":", 1)[0] if node_class else None
        cache_key = (node_url, project)
        if cache_key not in listings:
            try:
                answered = client.list_jobs(node_url, project)
            except Exception as exc:  # noqa: BLE001 - unreachable is a non-verdict
                logger.warning(
                    "dispatch.reconcile_node_unreachable intent=%s node_url=%s error=%r",
                    intent_id,
                    node_url,
                    exc,
                )
                answered = None
            listings[cache_key] = (
                None if answered is None else {str(value) for value in answered}
            )
        known = listings[cache_key]
        if known is None:
            unreachable += 1
            continue

        store = DispatchIntentStore(
            None,
            workspace_id=ws_id,
            scrape_job_id=job_id,
            session_factory=session_factory,
        )
        if jobid in known:
            store.confirm(identity, jobid)
            confirmed += 1
            logger.info(
                "dispatch.reconcile_confirmed intent=%s jobid=%s node_url=%s",
                intent_id,
                jobid,
                node_url,
            )
            continue

        outcome = store.record_absence(
            identity,
            now=now,
            policy=policy,
            node_url=node_url,
            history_intact=_history_is_intact(
                known, witnesses.get(node_url, ()), since=posted_at
            ),
            detail=f"node {node_url} answered listjobs.json and does not know {jobid}",
        )
        if outcome.verdict is AbsenceVerdict.MISSING:
            missing += 1
            candidates.append(intent_id)
            logger.warning(
                "dispatch.reconcile_missing intent=%s jobid=%s node_url=%s detail=%s",
                intent_id,
                jobid,
                node_url,
                outcome.detail,
            )
        elif outcome.verdict is AbsenceVerdict.IN_FLIGHT:
            in_flight += 1
            logger.info(
                "dispatch.reconcile_in_flight intent=%s jobid=%s node_url=%s detail=%s",
                intent_id,
                jobid,
                node_url,
                outcome.detail,
            )
        elif outcome.verdict is AbsenceVerdict.AMBIGUOUS:
            ambiguous += 1
            logger.warning(
                "dispatch.reconcile_ambiguous intent=%s jobid=%s node_url=%s detail=%s",
                intent_id,
                jobid,
                node_url,
                outcome.detail,
            )

    return ReconcileReport(
        examined=examined,
        confirmed=confirmed,
        missing=missing,
        unreachable=unreachable,
        in_flight=in_flight,
        ambiguous=ambiguous,
        reposted_candidates=tuple(candidates),
    )


def _confirmed_witnesses(
    session: Any, node_urls: set[str]
) -> dict[str, tuple[tuple[str, datetime | None], ...]]:
    """Runs each node is KNOWN to have accepted, newest first (R11).

    A witness is a ``CONFIRMED`` intent on that node: the node itself
    told us, at ``confirmed_at``, that it had this run. It is the only
    thing available that can date a node's memory from the outside, and
    dating that memory is what turns "the node did not mention our job"
    into evidence or into nothing.

    Cross-tenant by construction — a node pool is fleet-wide, and a
    workspace's own rows cannot date a node that serves every workspace.
    """
    if not node_urls:
        return {}
    rows = (
        session.execute(
            select(DispatchIntent)  # noqa: workspace-scope - system sweep
            .where(
                DispatchIntent.state == DispatchIntentState.CONFIRMED,
                DispatchIntent.node_url.in_(sorted(node_urls)),
            )
            .order_by(DispatchIntent.confirmed_at.desc())
            .limit(_WITNESS_SCAN_LIMIT)
        )
        .scalars()
        .all()
    )
    grouped: dict[str, list[tuple[str, datetime | None]]] = {}
    for row in rows:
        grouped.setdefault(str(row.node_url or ""), []).append(
            (str(row.scrapyd_job_id), row.confirmed_at)
        )
    return {node: tuple(values) for node, values in grouped.items()}


def _history_is_intact(
    known: set[str],
    witnesses: Iterable[tuple[str, datetime | None]],
    *,
    since: datetime | None,
) -> bool:
    """Can this node's silence about a run posted at ``since`` be trusted?

    Only witnesses confirmed **at or after** ``since`` are consulted, and
    that cut-off is the whole idea. A node running ``MemoryJobStorage``
    with ``finished_to_keep = 100`` legitimately forgets old runs, so an
    ancient witness going missing says nothing. A run the node accepted
    *after* ours going missing says a great deal: the node's memory does
    not reach back as far as our intent, either because it restarted or
    because the finished buffer has rolled past that point. In that case
    our own run could have been forgotten the same way, and its absence
    is not evidence.

    With no qualifying witness there is nothing to disprove intactness
    with, so this answers ``True`` and the age horizon in
    :class:`AbsencePolicy` is what bounds the claim instead.
    """
    if since is None:
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    for jobid, confirmed_at in witnesses:
        if confirmed_at is None:
            continue
        stamp = (
            confirmed_at.replace(tzinfo=timezone.utc)
            if confirmed_at.tzinfo is None
            else confirmed_at
        )
        if stamp < since:
            continue
        if jobid not in known:
            return False
    return True



def identity_from_intent_row(intent: DispatchIntent) -> DispatchIdentity:
    """Rebuild a row's :class:`DispatchIdentity` without importing B3's module.

    A thin local alias for
    :func:`app_shared.scrapyd.reconcile.identity_from_intent`, imported
    lazily for the same import-cycle reason
    :meth:`DispatchIntentStore.reconcile_inflight` documents.
    """
    from app_shared.scrapyd.reconcile import identity_from_intent

    return identity_from_intent(intent)
