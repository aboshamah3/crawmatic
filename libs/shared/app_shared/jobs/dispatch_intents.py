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
Each of :meth:`plan`, :meth:`record_post`, :meth:`confirm`, :meth:`fail`
and :meth:`mark_missing` opens its **own short transaction** from the
injected factory and commits it before returning. This is not a
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
    "DispatchIntentStore",
    "ReconcileReport",
    "SCRAPYD_JOB_ID_NAMESPACE",
    "deterministic_scrapyd_job_id",
    "iter_dispatch_scrapyd_job_ids",
    "reconcile_inflight_intents",
]

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
        from app_shared.scrapyd.reconcile import reconcile_inflight as _reconcile_inflight

        with self._txn() as session:
            intent = self._load(identity, session)
            if intent is None or intent.state != DispatchIntentState.POSTED:
                return None

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
            intent.state = DispatchIntentState.CONFIRMED
            intent.confirmed_at = datetime.now(timezone.utc)
            intent.error_message = None
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

    def mark_missing(self, identity: DispatchIdentity, detail: str = "") -> None:
        """Move a ``POSTED`` intent to ``RECONCILED_MISSING`` — re-POST allowed.

        Step 5's only positive outcome. Written **only** by
        :func:`reconcile_inflight_intents`, and only after every node in
        the pool answered ``listjobs.json`` and none of them listed this
        intent's ``scrapyd_job_id``. A node that could not be reached
        leaves the row ``POSTED``: absence of an answer is not an answer.

        The re-POST that this state authorizes carries the **same**
        ``scrapyd_job_id``, so a node that did in fact receive the
        original request dedups it.
        """
        with self._txn() as session:
            intent = self._load(identity, session)
            if intent is None or intent.state != DispatchIntentState.POSTED:
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

    #: ``POSTED`` rows examined.
    examined: int = 0
    #: Rows a node listed — advanced to ``CONFIRMED``. Never re-POSTed.
    confirmed: int = 0
    #: Rows every node denied — advanced to ``RECONCILED_MISSING``. These,
    #: and only these, may be re-POSTed (with the same ``scrapyd_job_id``).
    missing: int = 0
    #: Rows left ``POSTED`` because a node could not be reached. No
    #: verdict; they are examined again next pass.
    unreachable: int = 0
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
) -> ReconcileReport:
    """Settle every ``POSTED`` dispatch intent against the node it names.

    Step 5 of the five-step protocol, and the reason step 3 is allowed to
    die without taking the system's knowledge with it. For each ``POSTED``
    row this asks ``client.list_jobs(node_url, project)`` — the node the
    row itself recorded, and the project half of its ``node_class`` — and
    then:

    * the node lists ``scrapyd_job_id``  -> :meth:`DispatchIntentStore.confirm`
      (``CONFIRMED``). **Never re-POST**: the run exists.
    * the node answers and does not list it -> :meth:`DispatchIntentStore.mark_missing`
      (``RECONCILED_MISSING``). This is the *only* state from which a
      re-POST is authorized, and it re-POSTs the **same** id.
    * the node cannot be reached (``list_jobs`` raises or returns
      ``None``) -> the row is left ``POSTED``. Absence of an answer is
      not evidence of absence; it is examined again next pass.

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
        now: injectable clock (unused for selection today; accepted so
            callers can pass their pass clock without a signature churn).

    Returns:
        ReconcileReport: counts plus the ids now safe to re-POST.
    """
    del now  # accepted for caller symmetry; selection is state-only today

    # --- scan: read the POSTED rows, then CLOSE the transaction ----------
    # The node calls below must not run with a transaction open (the same
    # rule step 3 obeys), so the scan is its own short read.
    with session_factory() as scan:
        query = select(DispatchIntent).where(  # noqa: workspace-scope - system sweep
            DispatchIntent.state == DispatchIntentState.POSTED
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
                identity_from_intent_row(row),
            )
            for row in rows
        ]
        scan.commit()

    examined = confirmed = missing = unreachable = 0
    candidates: list[str] = []

    for ws_id, job_id, intent_id, node_url, node_class, jobid, identity in pending:
        examined += 1
        project = node_class.split(":", 1)[0] if node_class else None
        try:
            known = client.list_jobs(node_url, project)
        except Exception as exc:  # noqa: BLE001 - unreachable is a non-verdict
            logger.warning(
                "dispatch.reconcile_node_unreachable intent=%s node_url=%s error=%r",
                intent_id,
                node_url,
                exc,
            )
            unreachable += 1
            continue
        if known is None:
            unreachable += 1
            continue

        store = DispatchIntentStore(
            None,
            workspace_id=ws_id,
            scrape_job_id=job_id,
            session_factory=session_factory,
        )
        if jobid in {str(value) for value in known}:
            store.confirm(identity, jobid)
            confirmed += 1
            logger.info(
                "dispatch.reconcile_confirmed intent=%s jobid=%s node_url=%s",
                intent_id,
                jobid,
                node_url,
            )
            continue
        store.mark_missing(
            identity,
            f"node {node_url} answered listjobs.json and does not know {jobid}",
        )
        missing += 1
        candidates.append(intent_id)
        logger.warning(
            "dispatch.reconcile_missing intent=%s jobid=%s node_url=%s",
            intent_id,
            jobid,
            node_url,
        )

    return ReconcileReport(
        examined=examined,
        confirmed=confirmed,
        missing=missing,
        unreachable=unreachable,
        reposted_candidates=tuple(candidates),
    )


def identity_from_intent_row(intent: DispatchIntent) -> DispatchIdentity:
    """Rebuild a row's :class:`DispatchIdentity` without importing B3's module.

    A thin local alias for
    :func:`app_shared.scrapyd.reconcile.identity_from_intent`, imported
    lazily for the same import-cycle reason
    :meth:`DispatchIntentStore.reconcile_inflight` documents.
    """
    from app_shared.scrapyd.reconcile import identity_from_intent

    return identity_from_intent(intent)
