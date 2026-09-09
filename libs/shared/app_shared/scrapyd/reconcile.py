"""``reconcile_inflight`` — the crash-after-POST recovery gate (EPA B3).

The one genuinely ambiguous state in B1's ledger is ``POSTED``: the
intent row says "a ``schedule.json`` POST was issued for this identity",
and nothing says whether Scrapyd ever received it. A worker killed
between :meth:`~app_shared.jobs.dispatch_intents.DispatchIntentStore.record_post`
and :meth:`~app_shared.jobs.dispatch_intents.DispatchIntentStore.confirm`
leaves exactly that. The Redis sentinel then ages out (120 s) and the
*only* remaining evidence is the row.

This module turns that ambiguity into a decision, and — this is the
whole point — refuses to guess. It never POSTs, never cancels, and never
returns "safe to re-POST" unless it positively established that no run
matching the intent exists on the node.

Why a new module rather than a method on the client
---------------------------------------------------
``ScrapydDispatchClient`` is the *dispatch* path: claim, POST, commit.
Reconciliation is the *recovery* path, runs on a different schedule (a
sweep, an operator command), and needs a database session the client
deliberately does not hold. Keeping it separate also keeps the client a
plain ``requests`` + ``redis`` object, as its own docstring insists.

The two correlation mechanisms (B3 Step 0 findings)
---------------------------------------------------
Both deployed nodes run **Scrapyd 1.6.0** (``scrapyd>=1.5,<2`` in
``apps/scrapers*/pyproject.toml``, resolved to 1.6.0 in ``uv.lock`` and
baked into the image by ``uv sync --locked``). Reading that exact build's
``scrapyd/webservice.py``:

* ``Schedule.render_POST`` declares
  ``@param("jobid", required=False, default=lambda: uuid.uuid1().hex)``
  and passes it straight through as ``_job``, answering
  ``{"jobid": jobid}``. A **client-supplied jobid is honoured verbatim**
  (Scrapyd ``.. versionchanged:: 1.2.0``). This is what
  ``SCRAPYD_DETERMINISTIC_JOBID`` (default **on** since this B3 Step 0
  finding — see ``config.py``) turns on in :mod:`app_shared.scrapyd.client`,
  sending the intent's own id.
* ``ListJobs.render_GET`` returns custom spider ``args`` **only for
  pending jobs** (``.. versionchanged:: 1.5.0``). ``running`` and
  ``finished`` entries carry ``id``/``project``/``spider``/timestamps and
  nothing else.

So the two mechanisms are *not* equivalent:

``SCRAPYD_DETERMINISTIC_JOBID=on`` (:data:`_MECHANISM_JOBID`)
    The remote run's id **is** the intent id, so a plain ``listjobs.json``
    lookup answers in every bucket — pending, running and finished alike.

``SCRAPYD_DETERMINISTIC_JOBID=off`` (:data:`_MECHANISM_ARGS`, the
fallback for a node that rejects the ``jobid`` field or predates it)
    The only correlator is the spider args, which exist **only while the
    job is still queued**. Once it starts running the link is gone, and
    this module answers :attr:`InflightVerdict.AMBIGUOUS` rather than
    inventing one. That asymmetry was the concrete argument for flipping
    the flag on, which B3 Step 0 confirmed both deployed nodes support —
    the flag now defaults **on**.

Absence is evidence, but bounded
--------------------------------
Both nodes run ``jobstorage = scrapyd.jobstorage.MemoryJobStorage``
(the stock default; neither ``scrapyd.conf`` overrides it) with
``finished_to_keep = 100``. The finished history is therefore capped at
100 entries **and lost on every container restart**. A run that finished
long ago, or finished before a redeploy, is indistinguishable from one
that never happened. :attr:`InflightVerdict.ABSENT` is reported only when
every node answered and none of them knows the job — and callers must
treat it as "no evidence of a run", not "proof of no run". It is still
strictly better than the pre-B3 alternative, which was to re-POST with no
lookup at all.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.orm import Session

from app_shared.enums import DispatchIntentState
from app_shared.models.dispatch import DispatchIntent
from app_shared.repository import scoped_select
from app_shared.scrapyd.identity import DispatchIdentity, encode_guard_value

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime cycle
    from app_shared.jobs.dispatch_intents import DispatchIntentStore

__all__ = [
    "InflightReconciliation",
    "InflightVerdict",
    "RequestsJobLister",
    "ScrapydJobLister",
    "identity_from_intent",
    "reconcile_inflight",
]

logger = logging.getLogger(__name__)

#: Correlate by the deterministic client-supplied jobid — works in every
#: ``listjobs.json`` bucket. Requires ``SCRAPYD_DETERMINISTIC_JOBID``.
_MECHANISM_JOBID = "deterministic_jobid"
#: Correlate by the pending queue's spider ``args`` — the only mechanism
#: available with the flag off, and only while the job is still queued.
_MECHANISM_ARGS = "listjobs_args"

#: ``node_class`` is ``{project}:{spider}``; the project half selects the pool.
_BROWSER_PROJECT = "price_monitor_browser"


class InflightVerdict(str, Enum):
    """What reconciliation established about a ``POSTED`` intent."""

    #: The intent was not in ``POSTED`` — nothing to reconcile. Whether a
    #: retry may proceed is then B1's ordinary business (a ``PLANNED``
    #: intent may; a ``CONFIRMED`` one is answered from the row).
    NOT_INFLIGHT = "not_inflight"
    #: A run matching this intent exists on a node. The intent has been
    #: advanced to ``CONFIRMED`` with the real ``scrapyd_job_id``.
    #: **Never re-POST.**
    CONFIRMED = "confirmed"
    #: Every node answered and none knows this job. The intent has been
    #: rolled back to ``PLANNED`` so the ordinary dispatch path may run
    #: it. See "Absence is evidence, but bounded" above.
    ABSENT = "absent"
    #: Could not be established — a node was unreachable, or the flag is
    #: off and the job has already left the pending queue (where its
    #: spider args lived). The intent stays ``POSTED``. **Never re-POST**;
    #: escalate.
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class InflightReconciliation:
    """The outcome of one :func:`reconcile_inflight` call."""

    verdict: InflightVerdict
    intent_id: str
    #: Set for :attr:`InflightVerdict.CONFIRMED` (and for a
    #: ``NOT_INFLIGHT`` intent that was already ``CONFIRMED``).
    scrapyd_job_id: str | None
    #: The intent's state *after* reconciliation.
    state: DispatchIntentState
    #: Which correlator was used, or why none could be.
    mechanism: str
    detail: str

    @property
    def may_repost(self) -> bool:
        """Whether the caller is cleared to run the ordinary dispatch path.

        True when the intent is back at ``PLANNED`` — i.e. this call
        established absence, or the intent was never in flight to begin
        with — or at ``RECONCILED_MISSING``, the equivalent verdict the
        EPA B2 maintenance sweep
        (:func:`app_shared.jobs.dispatch_intents.reconcile_inflight_intents`)
        writes when every node denied the job. Both mean "looked, not
        there"; the re-POST that follows carries the SAME
        ``scrapyd_job_id``, so a node that did receive the original
        request dedups it. ``CONFIRMED`` and ``AMBIGUOUS`` are both hard
        stops: the first because the run exists, the second because we do
        not know.
        """
        return self.state in (
            DispatchIntentState.PLANNED,
            DispatchIntentState.RECONCILED_MISSING,
        )


class ScrapydJobLister(Protocol):
    """Read-only ``listjobs.json`` access. ``None`` means *node unreachable*.

    A Protocol rather than a client method so reconciliation can be
    driven from a fake in tests and from a real HTTP session in
    production without either one importing the other. The distinction
    between ``None`` (no answer) and ``{}``-shaped payload (an answer
    listing nothing) is load-bearing: only the latter can support
    :attr:`InflightVerdict.ABSENT`.
    """

    def list_jobs(  # noqa: D102 - protocol stub
        self, node_url: str, project: str | None = None
    ) -> dict[str, Any] | None: ...


class RequestsJobLister:
    """The production :class:`ScrapydJobLister` — a plain authenticated GET.

    ``listjobs.json`` is one of the five endpoints that are safe to call
    against a live node: it schedules nothing, cancels nothing and
    mutates nothing.
    """

    def __init__(
        self, settings: Any, *, session: Any = None, timeout: float = 15.0
    ) -> None:
        self._settings = settings
        self._session = session
        self._timeout = timeout

    def list_jobs(
        self, node_url: str, project: str | None = None
    ) -> dict[str, Any] | None:
        import requests

        getter = self._session.get if self._session is not None else requests.get
        params = {"project": project} if project else {}
        try:
            response = getter(
                f"{node_url.rstrip('/')}/listjobs.json",
                params=params,
                auth=(
                    self._settings.SCRAPYD_USERNAME,
                    self._settings.SCRAPYD_PASSWORD,
                ),
                timeout=self._timeout,
            )
            if response.status_code >= 400:
                return None
            payload = response.json()
        except Exception:  # noqa: BLE001 - unreachable is a verdict, not a crash
            return None
        return payload if isinstance(payload, dict) else None


def identity_from_intent(intent: DispatchIntent) -> DispatchIdentity:
    """Rebuild the :class:`DispatchIdentity` a stored row was written for.

    Every identity component is a column on the row, so the identity is
    reconstructible rather than re-derivable-from-the-plan — which
    matters precisely because recovery happens when the plan that
    produced it is long gone.

    The rebuilt key is checked against the stored ``identity_key``. They
    can only disagree if a row was hand-edited or a component's spelling
    drifted; either way, reconciling against a name we cannot reproduce
    would be worse than refusing.

    Raises:
        ValueError: the rebuilt identity does not reproduce
            ``intent.identity_key``.
    """
    identity = DispatchIdentity(
        scrape_job_id=str(intent.scrape_job_id),
        planning_generation=int(intent.planning_generation or 0),
        strategy_method=str(intent.strategy_method),
        domain=str(intent.domain),
        mode=str(getattr(intent.mode, "value", intent.mode)),
        node_class=str(intent.node_class),
        work_digest=str(intent.match_ids_digest),
    )
    if identity.key != intent.identity_key:
        raise ValueError(
            f"dispatch intent {intent.id} does not reproduce its own identity key "
            f"({identity.key!r} != {intent.identity_key!r}); refusing to reconcile"
        )
    return identity


def _node_urls_for(intent: DispatchIntent, settings: Any) -> list[str]:
    """The pool this intent's ``node_class`` names.

    The intent records the node *class*, never the selected URL (B1: a
    pool resize must not mint a new identity), so recovery has to ask the
    whole pool. That is also what makes reconciliation correct when the
    original node has since been replaced — item 8's node-failure replan.
    """
    project = str(intent.node_class).split(":", 1)[0]
    if project == _BROWSER_PROJECT:
        return list(settings.SCRAPYD_BROWSER_URLS)
    return list(settings.SCRAPYD_HTTP_URLS)


def _iter_entries(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    for bucket in ("pending", "running", "finished"):
        for entry in payload.get(bucket) or ():
            if isinstance(entry, dict):
                entries.append((bucket, entry))
    return entries


def _match_ids_of(entry: dict[str, Any]) -> set[str]:
    """The ``match_ids`` spider arg of a *pending* entry, as a set.

    The dispatch client serializes the batch's match ids to the spider's
    comma-separated form (a list would be collapsed to one value by
    ``schedule.json``), so this splits it back. Order is irrelevant —
    the work digest is order-insensitive for the same reason.
    """
    args = entry.get("args")
    if not isinstance(args, dict):
        return set()
    raw = args.get("match_ids")
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(value) for value in raw}
    return {part.strip() for part in str(raw).split(",") if part.strip()}


def _correlate(
    intent: DispatchIntent,
    payload: dict[str, Any],
    *,
    deterministic: bool,
) -> tuple[str | None, bool]:
    """Find this intent's run in one node's listing.

    Returns ``(scrapyd_job_id, correlatable)``. ``correlatable`` is False
    when the listing contains entries this mechanism simply cannot speak
    about — i.e. the flag is off and a running/finished job is present,
    whose spider args ``listjobs.json`` does not expose (Step 0). That is
    what turns a miss into :attr:`InflightVerdict.AMBIGUOUS` instead of
    :attr:`InflightVerdict.ABSENT`.
    """
    entries = _iter_entries(payload)
    if deterministic:
        # The remote id IS the id WE chose at plan time (EPA B2:
        # `dispatch_intents.scrapyd_job_id`, a uuid5 over the identity),
        # so every bucket is searchable. `intent.id` is the pre-B2
        # fallback for a row planned before that column was populated.
        wanted = str(intent.scrapyd_job_id or intent.id)
        for _bucket, entry in entries:
            if str(entry.get("id")) == wanted:
                return wanted, True
        return None, True

    expected_job = str(intent.scrape_job_id)
    expected_matches = {str(value) for value in (intent.match_ids or ())}
    opaque = False
    for bucket, entry in entries:
        if bucket != "pending":
            # No `args` on running/finished entries — cannot speak about it.
            opaque = True
            continue
        args = entry.get("args") if isinstance(entry.get("args"), dict) else {}
        if str(args.get("scrape_job_id")) != expected_job:
            continue
        if expected_matches and _match_ids_of(entry) != expected_matches:
            continue
        return str(entry.get("id")), True
    return None, not opaque


def reconcile_inflight(
    session: Session,
    intent_id: uuid.UUID | str,
    *,
    workspace_id: uuid.UUID | str,
    settings: Any,
    lister: ScrapydJobLister | None = None,
    redis: Any = None,
) -> InflightReconciliation:
    """Decide what happened to a ``POSTED`` dispatch intent. **Never POSTs.**

    The recovery half of B1's ledger, and the mechanism EPA B3 item 7
    exists to prove: a worker that died between the POST and the
    ``CONFIRMED`` write leaves a row that says "a run may exist", and the
    only safe next step is to *look*, on the pool the intent's
    ``node_class`` names, using whichever correlator the deployment
    supports (see the module docstring).

    Writes ride the caller's transaction — this function flushes and
    never commits, exactly like
    :class:`~app_shared.jobs.dispatch_intents.DispatchIntentStore`, so the
    reconciled state and whatever the caller does about it stay atomic.

    ``redis``, when given, has its guard healed on a ``CONFIRMED``
    verdict, so the next delivery of the same identity is answered on the
    cheap path. Best-effort; a Redis failure never changes the verdict.

    Args:
        session: the caller's transaction.
        intent_id: ``dispatch_intents.intent_id``.
        workspace_id: the tenant, asserted explicitly (Principle II) —
            never inferred from the row being looked up.
        settings: supplies the node pools, the credentials, and
            ``SCRAPYD_DETERMINISTIC_JOBID``.
        lister: read-only ``listjobs.json`` access; defaults to
            :class:`RequestsJobLister` over ``settings``.
        redis: optional guard cache to heal on confirmation.

    Returns:
        InflightReconciliation: whose :attr:`~InflightReconciliation.may_repost`
        is the only thing a caller should branch on to decide whether the
        ordinary dispatch path may run.

    Raises:
        LookupError: no such intent in this workspace.
        ValueError: the row does not reproduce its own identity key.
    """
    intent = session.execute(
        scoped_select(DispatchIntent, workspace_id).where(
            DispatchIntent.id == _as_uuid(intent_id)
        )
    ).scalar_one_or_none()
    if intent is None:
        raise LookupError(
            f"dispatch intent {intent_id} is not visible in workspace {workspace_id}"
        )

    if intent.state != DispatchIntentState.POSTED:
        return InflightReconciliation(
            verdict=InflightVerdict.NOT_INFLIGHT,
            intent_id=str(intent.id),
            scrapyd_job_id=intent.scrapyd_job_id,
            state=intent.state,
            mechanism="none",
            detail=f"intent is {intent.state}, not POSTED; nothing to reconcile",
        )

    # Reconstruct (and self-check) the name the POST was issued under.
    identity = identity_from_intent(intent)

    deterministic = bool(getattr(settings, "SCRAPYD_DETERMINISTIC_JOBID", False))
    mechanism = _MECHANISM_JOBID if deterministic else _MECHANISM_ARGS
    if lister is None:
        lister = RequestsJobLister(settings)

    unreachable: list[str] = []
    opaque_nodes: list[str] = []
    for node_url in _node_urls_for(intent, settings):
        payload = lister.list_jobs(node_url, _project_of(intent))
        if payload is None:
            unreachable.append(node_url)
            continue
        found, correlatable = _correlate(intent, payload, deterministic=deterministic)
        if found is not None:
            return _confirm(session, intent, identity, found, redis, mechanism, settings)
        if not correlatable:
            opaque_nodes.append(node_url)

    if unreachable or opaque_nodes:
        # Not established. The row stays POSTED: an intent nobody could
        # resolve must keep looking like the open question it is.
        reason = (
            f"nodes unreachable: {len(unreachable)}"
            if unreachable
            else "the run has left the pending queue and listjobs.json exposes "
            "spider args only for pending jobs — enable SCRAPYD_DETERMINISTIC_JOBID "
            "to make this correlatable"
        )
        logger.warning(
            "dispatch.reconcile_inflight_ambiguous intent=%s identity=%s reason=%s",
            intent.id,
            identity.canonical_payload,
            reason,
        )
        return InflightReconciliation(
            verdict=InflightVerdict.AMBIGUOUS,
            intent_id=str(intent.id),
            scrapyd_job_id=None,
            state=intent.state,
            mechanism=mechanism,
            detail=reason,
        )

    # Every node answered and none of them knows this job. Roll the intent
    # back to PLANNED so the ordinary dispatch path owns it again — the
    # same state a crash *before* the POST leaves (matrix item 6).
    intent.state = DispatchIntentState.PLANNED
    intent.posted_at = None
    intent.error_message = None
    session.flush()
    logger.info(
        "dispatch.reconcile_inflight_absent intent=%s identity=%s mechanism=%s",
        intent.id,
        identity.canonical_payload,
        mechanism,
    )
    return InflightReconciliation(
        verdict=InflightVerdict.ABSENT,
        intent_id=str(intent.id),
        scrapyd_job_id=None,
        state=intent.state,
        mechanism=mechanism,
        detail="no node knows this job; intent rolled back to PLANNED",
    )


def _project_of(intent: DispatchIntent) -> str:
    return str(intent.node_class).split(":", 1)[0]


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _confirm(
    session: Session,
    intent: DispatchIntent,
    identity: DispatchIdentity,
    scrapyd_job_id: str,
    redis: Any,
    mechanism: str,
    settings: Any,
) -> InflightReconciliation:
    """Adopt a run we found as this intent's outcome.

    Goes through the real :class:`DispatchIntentStore` rather than
    setting columns here, so recovery and dispatch write ``CONFIRMED``
    through exactly one piece of code.

    Deliberately *not* fenced on ``cancellation_generation``: a run that
    demonstrably exists must be recorded even when the job has since been
    cancelled — that is precisely how
    :func:`app_shared.jobs.cancellation.iter_known_scrapyd_job_ids` learns
    an id to stop. The fence's job is to stop us *paying* for new work,
    and this function starts none.

    Imports :class:`DispatchIntentStore` locally (EPA B3b): once
    ``ScrapydDispatchClient.schedule()`` step 0b calls this module through
    :meth:`~app_shared.jobs.dispatch_intents.DispatchIntentStore.reconcile_inflight`,
    a module-level import here would close the loop
    (``dispatch_intents`` -> this module -> ``dispatch_intents``) before
    either module finished defining its classes.
    """
    from app_shared.jobs.dispatch_intents import DispatchIntentStore

    store = DispatchIntentStore(
        session,
        workspace_id=intent.workspace_id,
        scrape_job_id=intent.scrape_job_id,
        authorized_cancellation_generation=int(
            intent.cancellation_generation_at_creation or 0
        ),
    )
    store.confirm(identity, scrapyd_job_id)
    if redis is not None:
        try:
            redis.set(
                identity.key,
                encode_guard_value(
                    identity,
                    jobid=scrapyd_job_id,
                    intent_id=str(intent.id),
                    committed_at=datetime.now(timezone.utc).isoformat(),
                ),
                ex=settings.SCRAPYD_DISPATCH_GUARD_TTL_SECONDS,
            )
        except Exception:  # noqa: BLE001 - the durable answer already stands
            logger.warning(
                "dispatch.reconcile_guard_heal_failed intent=%s", intent.id, exc_info=True
            )
    logger.info(
        "dispatch.reconcile_inflight_confirmed intent=%s jobid=%s mechanism=%s",
        intent.id,
        scrapyd_job_id,
        mechanism,
    )
    return InflightReconciliation(
        verdict=InflightVerdict.CONFIRMED,
        intent_id=str(intent.id),
        scrapyd_job_id=str(scrapyd_job_id),
        state=DispatchIntentState.CONFIRMED,
        mechanism=mechanism,
        detail=f"run {scrapyd_job_id} found via {mechanism}; adopted, no re-POST",
    )
