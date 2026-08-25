"""The durable authority behind dispatch idempotency (EPA B1, READY-002).

:class:`DispatchIntentStore` is the SQLAlchemy implementation of
:class:`app_shared.scrapyd.identity.DispatchIntentAuthority` — the thing
:class:`~app_shared.scrapyd.client.ScrapydDispatchClient` consults
*before* it touches Redis, and writes to on every state transition.

Transaction ownership
---------------------
This store **never commits**. It writes into the caller's session and
flushes so the rows are visible to later reads in the same transaction;
the planner owns the commit. That is not a stylistic choice — it is what
makes the intent's ``state`` and the target's ``dispatched_at`` stamp
atomic with each other. ``tasks_jobs.dispatch_job`` deliberately commits
"what was earned" when a later batch fails, and an intent row that
committed independently of that stamp would leave the two disagreeing
about whether a batch was dispatched.

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

import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from app_shared.enums import DispatchIntentState, ScrapeJobStatus
from app_shared.models.dispatch import DispatchIntent
from app_shared.models.jobs import ScrapeJob
from app_shared.repository import scoped_select
from app_shared.scrapyd.errors import StaleCancellationGenerationError
from app_shared.scrapyd.identity import CommittedDispatch, DispatchIdentity

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime cycle
    from app_shared.scrapyd.reconcile import InflightReconciliation

__all__ = ["DispatchIntentStore", "iter_dispatch_scrapyd_job_ids"]


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
        session: Session,
        *,
        workspace_id: uuid.UUID | str,
        scrape_job_id: uuid.UUID | str,
        authorized_cancellation_generation: int = 0,
    ) -> None:
        self._session = session
        self._workspace_id = _as_uuid(workspace_id)
        self._scrape_job_id = _as_uuid(scrape_job_id)
        #: The job's ``cancellation_generation`` as read when this plan was
        #: authorized. Stamped onto every intent this store creates, and
        #: the fallback comparison for an identity with no row yet.
        self._authorized_generation = int(authorized_cancellation_generation or 0)

    # --- planning ------------------------------------------------------------

    def plan(
        self,
        identity: DispatchIdentity,
        *,
        match_ids: Iterable[object],
        batch_index: object = None,
    ) -> DispatchIntent:
        """Create (or return) the ``PLANNED`` intent for ``identity``.

        Get-or-create keyed on ``identity_key``, which is UNIQUE in the
        database: a replayed planning pass that re-derives the same
        identity re-uses the same row rather than racing a second one into
        a constraint violation. Called by the planner in the SAME
        transaction that advances the strategy cursor, so a committed plan
        and the intents it produced can never disagree.
        """
        existing = self._load(identity)
        if existing is not None:
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
            state=DispatchIntentState.PLANNED,
            cancellation_generation_at_creation=self._authorized_generation,
            batch_index=None if batch_index is None else str(batch_index),
        )
        self._session.add(intent)
        self._session.flush()
        return intent

    # --- DispatchIntentAuthority ---------------------------------------------

    def reconcile(self, identity: DispatchIdentity) -> CommittedDispatch | None:
        """The committed dispatch for ``identity``, or ``None`` — fence first.

        Raises:
            StaleCancellationGenerationError: the job has been cancelled
                since this work was authorized (see the module docstring).
        """
        intent = self._load(identity)
        authorized = (
            intent.cancellation_generation_at_creation
            if intent is not None
            else self._authorized_generation
        )
        self._assert_fence_current(identity, authorized)

        if intent is None or intent.state != DispatchIntentState.CONFIRMED:
            return None
        if not intent.scrapyd_job_id:
            # CONFIRMED without an id cannot happen through `confirm`, but
            # a hand-repaired row could look like this. Refusing to answer
            # is safer than answering with nothing.
            return None
        return CommittedDispatch(
            jobid=str(intent.scrapyd_job_id),
            identity_payload=str(intent.identity_payload),
            intent_id=str(intent.id),
            committed_at=(
                intent.confirmed_at.isoformat() if intent.confirmed_at is not None else None
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
        intent = self._load(identity)
        if intent is None or intent.state != DispatchIntentState.POSTED:
            return None

        from app_shared.scrapyd.reconcile import reconcile_inflight as _reconcile_inflight

        return _reconcile_inflight(
            self._session,
            intent.id,
            workspace_id=self._workspace_id,
            settings=settings,
            lister=lister,
            redis=redis,
        )

    def record_post(self, identity: DispatchIdentity) -> str:
        """Mark the intent ``POSTED`` **before** the network call.

        Ordering matters: a worker killed mid-POST must leave evidence
        that a run may exist on the node. ``POSTED`` is the one genuinely
        ambiguous state, and recording it is how the ambiguity becomes
        something B3 can reconcile instead of something nobody knows to
        look for.
        """
        intent = self._load(identity) or self.plan(identity, match_ids=())
        intent.state = DispatchIntentState.POSTED
        intent.posted_at = datetime.now(timezone.utc)
        self._session.flush()
        return str(intent.id)

    def confirm(self, identity: DispatchIdentity, scrapyd_job_id: str) -> None:
        """Record Scrapyd's ``jobid`` and mark the intent ``CONFIRMED``."""
        intent = self._load(identity) or self.plan(identity, match_ids=())
        intent.scrapyd_job_id = str(scrapyd_job_id)
        intent.state = DispatchIntentState.CONFIRMED
        intent.confirmed_at = datetime.now(timezone.utc)
        intent.error_message = None
        self._session.flush()

    def fail(self, identity: DispatchIdentity, error: str) -> None:
        """Mark the intent ``FAILED``; the Redis claim is released separately.

        The row is kept, never deleted: "tried and failed" must stay
        distinguishable from "never tried", which is the whole reason the
        pre-B1 release step (a bare ``DELETE`` of the Redis key) left
        operators unable to tell a wedged batch from an unplanned one.
        """
        intent = self._load(identity)
        if intent is None:
            return
        intent.state = DispatchIntentState.FAILED
        intent.error_message = error[:2000]
        self._session.flush()

    # --- internals -----------------------------------------------------------

    def _load(self, identity: DispatchIdentity) -> DispatchIntent | None:
        return self._session.execute(
            scoped_select(DispatchIntent, self._workspace_id).where(
                DispatchIntent.scrape_job_id == self._scrape_job_id,
                DispatchIntent.identity_key == identity.key,
            )
        ).scalar_one_or_none()

    def _assert_fence_current(self, identity: DispatchIdentity, authorized: int) -> None:
        """A2's fence: refuse work authorized before a cancellation."""
        job = self._session.execute(
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
