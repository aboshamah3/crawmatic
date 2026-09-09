"""Authenticated, idempotent Scrapyd ``schedule.json`` dispatch client.

Framework-agnostic (plain ``requests`` + ``redis``, no scrapy/twisted): it
POSTs ``schedule.json`` on a ``SCRAPYD_HTTP_URLS`` node with HTTP basic auth
(``SCRAPYD_USERNAME`` / ``SCRAPYD_PASSWORD``) and forwards the spider args
through unchanged, returning the Scrapyd ``jobid``.

Idempotency ordering (the important bit — see :meth:`ScrapydDispatchClient.schedule`)
-------------------------------------------------------------------------------------
A retried at-least-once Celery dispatch must never double-run a batch, yet a
dispatch that *failed* (401 / network error) must never leave a poisoned key
that suppresses a later legitimate retry. Both are reconciled with a
**reconcile -> claim -> POST -> commit -> release** sequence keyed on
:attr:`~app_shared.scrapyd.identity.DispatchIdentity.key` — a digest over the
identity's canonical payload, **never** the batch's position in the plan
(EPA B1; ``batch_index`` survives only as the legacy positional key's input
and a traceability label on the call site — it is not itself POSTed to
Scrapyd — see :mod:`app_shared.scrapyd.identity` for why the positional key
was a wedge):

0. **Reconcile the durable intent FIRST** — before touching Redis. The
   ``dispatch_intents`` row, not the guard, is the authority on whether this
   exact identity already reached Scrapyd. This ordering is what makes
   "sentinel expiry alone NEVER authorizes a re-POST" true: an aged-out guard
   over a ``CONFIRMED`` intent resolves to the committed jobid and the guard is
   *healed*, not re-POSTed. The same read enforces A2's cancellation fence — an
   intent authorized under a superseded ``cancellation_generation`` raises
   :class:`~app_shared.scrapyd.errors.StaleCancellationGenerationError` and
   nothing is claimed or POSTed.

   **0b.** When the intent instead exists and is ``POSTED`` — a worker died
   between the POST and the ``CONFIRMED`` write, and the sentinel has since
   aged out — the aged-out sentinel is likewise not authorization. This is
   the crash-after-POST window EPA B3 built
   :func:`app_shared.scrapyd.reconcile.reconcile_inflight` to close (EPA
   B3b wires it in here): the durable intent is handed to it, on the pool
   the intent's ``node_class`` names. A found run is *adopted* — the intent
   is marked ``CONFIRMED`` with the real jobid and the guard healed — and
   this method returns without ever POSTing. When every node answered and
   none knows the job, the intent is rolled back to ``PLANNED`` and control
   falls through to claim + POST exactly as a crash *before* the POST would
   (item 6). When reconciliation could not be established (a node was
   unreachable, or the correlator cannot see it — see
   ``SCRAPYD_DETERMINISTIC_JOBID``), this raises
   :class:`ScrapydDispatchError` — **never** a blind re-POST.
1. **Claim** the slot with Redis ``SET key <pending-sentinel> NX``. If the claim
   fails, the key already exists:
   - if it holds a committed guard whose ``identity_payload`` matches -> return
     its jobid as a **no-op** (never re-schedule);
   - if it still holds the sentinel, a concurrent dispatch is in flight -> raise
     (do not double-POST);
   - if it holds anything else (a pre-B1 bare jobid, a corrupted value), it
     proves nothing about this identity — step 0 already established there is
     no durable commitment, so the value is replaced under a compare-and-set
     (re-read, then swap only if it has not moved).
2. **POST**, with the intent marked ``POSTED`` immediately beforehand so a
   worker that dies mid-flight leaves durable evidence that a POST may have
   been issued.
3. **Commit**: only after Scrapyd returns ``status=ok`` is the intent marked
   ``CONFIRMED`` with its ``scrapyd_job_id`` and the sentinel overwritten with
   the committed guard value.
4. **Release**: on *any* failure before commit (401, network error, non-ok
   response) the intent is marked ``FAILED`` and the Redis key is ``DELETE``d,
   so the claim never outlives a failed attempt and a legitimate retry can
   proceed — while the *attempt* stays durably distinguishable from "never
   tried".

The sentinel is therefore only ever visible while a POST is genuinely in
flight; a crash mid-flight leaves at most a short-lived sentinel plus a
``POSTED`` intent, never a permanent jobid for a run that never started.

Durable-intent writes ride the caller's transaction: the store is handed the
planner's session and is flushed, never committed, here. The planner owns the
commit (``tasks_jobs.dispatch_job`` commits what was earned even when a later
batch fails), which keeps the intent's state and the target's ``dispatched_at``
stamp atomic with one another.

``intents=None`` degrades to the Redis-only guard — the posture of the thin
SPEC-07 ``dispatch.generic_price_spider`` task, which has no session.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

import requests

from app_shared.config import Settings, get_settings
from app_shared.redis_client import get_redis_client
from app_shared.scrapyd.errors import (
    ScrapydAuthError,
    ScrapydDispatchError,
    StaleCancellationGenerationError,
)
from app_shared.scrapyd.identity import (
    PENDING_SENTINEL as _PENDING_SENTINEL,
)
from app_shared.scrapyd.identity import (
    CommittedDispatch,
    DispatchIdentity,
    DispatchIntentAuthority,
    build_dispatch_identity,
    decode_guard_value,
    encode_guard_value,
)

__all__ = [
    "CommittedDispatch",
    "DispatchIdentity",
    "DispatchIntegrityError",
    "ScrapydAuthError",
    "ScrapydDispatchClient",
    "ScrapydDispatchError",
    "StaleCancellationGenerationError",
    "build_dispatch_identity",
    "dispatch_key",
]


class DispatchIntegrityError(ScrapydDispatchError):
    """Refused to stamp `dispatched_at` without a proven committed dispatch.

    Raised by `app.workers.tasks_dispatch.stamp_targets_dispatched` (EPA
    B2, the single stamping path) when neither the Redis guard nor the
    durable `dispatch_intents` row (`get_committed_dispatch`, B1) carries
    an `identity_payload` that matches this batch's exact
    `DispatchIdentity` -- i.e. nothing proves this precise unit of work
    ever reached Scrapyd. Stamping anyway would be the read-side twin of
    B1's aliasing bug: a target marked dispatched with no POST behind it
    is invisible to `redispatch_pending_jobs` (which only picks up
    `dispatched_at IS NULL`) and to `recover_stalled_batches` (which only
    reaps a target already stamped), so it would simply never run again.
    Every raise here also logs `dispatch_integrity_error identity=...
    found=...` for alerting -- see the caller's docstring.
    """

# Conservative default; a dispatch POST that hangs must not block a worker.
_DEFAULT_TIMEOUT_SECONDS = 30.0

# TTL on the in-flight sentinel: only ever needs to outlive one POST
# (timeout 30s). Bounded so a crash between claim and release can no
# longer leave a permanent sentinel that blocks every later dispatch of
# that batch.
_SENTINEL_TTL_SECONDS = 120

logger = logging.getLogger(__name__)

#: Projects asked to cancel a Scrapyd job id. ``cancel.json`` is
#: per-project and a jobid does not name its project, so both deployed
#: projects are tried. Kept as a module constant (not config) because it
#: mirrors the two spider projects this client can ever schedule.
_CANCELLABLE_PROJECTS = ("price_monitor", "price_monitor_browser")

#: **Deterministic Scrapyd jobid (B1 -> B3 Step 0).**
#:
#: Scrapyd's ``schedule.json`` accepts an optional ``jobid`` form field and
#: uses it verbatim instead of minting a uuid4. Passing ``intent_id`` there
#: would make the remote run's identity equal to our durable intent's, which
#: closes the last gap in the reconcile story: today a crash between the POST
#: and the ``CONFIRMED`` write leaves an intent stuck at ``POSTED`` with no
#: ``scrapyd_job_id``, and the only way to find the orphaned run is to list the
#: node and guess. With a deterministic jobid the answer is computable.
#:
#: **Capability-detected, not assumed.** Support was added in Scrapyd 1.2 and
#: some builds/middlewares reject unknown form fields, so this is opt-in via
#: ``SCRAPYD_DETERMINISTIC_JOBID`` rather than sent unconditionally. A node
#: that ignores the field simply answers with its own jobid, which is
#: recorded as before — the field is additive, never load-bearing.
#:
#: **B3 Step 0 resolved this (LIVE node check):** both deployed Scrapyd nodes
#: are 1.6.0 (``apps/scrapers/pyproject.toml`` and
#: ``apps/scrapers-browser/pyproject.toml`` pin ``scrapyd>=1.5,<2``,
#: ``uv.lock`` resolves that to 1.6.0) and honour a client-supplied ``jobid``
#: on ``schedule.json`` — it answers ``{"status":"ok","jobid":"<that same
#: uuid>"}`` and ``listjobs.json`` shows it. ``SCRAPYD_DETERMINISTIC_JOBID``
#: therefore defaults **on** (see ``config.py``) and the orphan-reconcile path
#: is a lookup, not a node listing.
_DETERMINISTIC_JOBID_SETTING = "SCRAPYD_DETERMINISTIC_JOBID"


class _RedisLike(Protocol):
    """The tiny Redis surface this client needs (``decode_responses=True``)."""

    def set(  # noqa: D102 - protocol stub
        self, name: str, value: str, *, nx: bool = ..., ex: int | None = ...
    ) -> bool | None: ...

    def get(self, name: str) -> str | None: ...  # noqa: D102 - protocol stub

    def delete(self, *names: str) -> int: ...  # noqa: D102 - protocol stub


def dispatch_key(scrape_job_id: str, batch_index: int | str) -> str:
    """Positional idempotency key for one (job, batch) dispatch. **Legacy.**

    Superseded by :attr:`app_shared.scrapyd.identity.DispatchIdentity.key`
    (EPA B1): ``batch_index`` is a *position* in a re-derivable plan, so
    this key aliases whenever the planned set changes — see the identity
    module's docstring for the wedge that caused. Retained only so a
    ``schedule(...)`` call made without an ``identity`` (the thin SPEC-07
    task shape, and any in-flight guard written by a pre-B1 worker) keeps
    the exact behaviour it had.
    """
    return f"dispatched:{scrape_job_id}:{batch_index}"


class ScrapydDispatchClient:
    """Schedules ``generic_price_spider`` runs on an authenticated Scrapyd node."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        redis_client: _RedisLike | None = None,
        session: requests.Session | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        intents: DispatchIntentAuthority | None = None,
        lister: Any = None,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._redis = redis_client if redis_client is not None else get_redis_client()
        self._session = session
        self._timeout = timeout
        #: The durable ``dispatch_intents`` authority (B1). ``None`` for
        #: callers with no session — the Redis guard then stands alone,
        #: exactly as it did before B1.
        self._intents = intents
        #: Read-only ``listjobs.json`` access for step 0b's crash-after-POST
        #: reconciliation (EPA B3b). ``None`` lets ``reconcile_inflight``
        #: fall back to its own production default
        #: (:class:`~app_shared.scrapyd.reconcile.RequestsJobLister`);
        #: tests inject a fake here.
        self._lister = lister

    def schedule(
        self,
        project: str,
        spider: str,
        *,
        workspace_id: str,
        scrape_job_id: str,
        match_ids: Any,
        mode: str,
        batch_index: int | str,
        node_url: str | None = None,
        identity: DispatchIdentity | None = None,
        jobid: str | uuid.UUID | None = None,
        authorization_id: str | uuid.UUID | None = None,
        budget_decision_version: str | None = None,
        entitlement_version: str | None = None,
        breaker_decision: str | None = None,
    ) -> str:
        """Schedule ``spider`` in ``project`` on Scrapyd; return the ``jobid``.

        Idempotent per ``identity`` — see the module docstring for the
        reconcile/claim/POST/commit/release ordering that guarantees a
        retried dispatch never double-runs a batch, a *failed* dispatch
        never poisons the key, and a replanned subset is never suppressed
        by the batch it came from.

        ``identity`` (EPA B1) is the canonical name of this dispatch. It
        is optional only for the thin SPEC-07 task and pre-B1 callers;
        when it is ``None`` the legacy positional
        ``dispatched:{job}:{batch_index}`` key is used and the durable
        intent is not consulted. ``batch_index`` is used to build that
        legacy key and survives as a traceability label on the call site
        that names it — it is **never** itself POSTed to Scrapyd, and is
        never part of the idempotency decision when an ``identity`` is
        given.

        ``node_url`` (SPEC-08 FR-012, FR-014) targets a specific
        deterministically-selected Scrapyd node; when ``None`` this falls
        back to ``SCRAPYD_HTTP_URLS[0]``.

        ``jobid`` (EPA B2 / F06) is the remote run's name, **chosen by the
        caller at plan time** — the intent's ``scrapyd_job_id``, a
        ``uuid5`` over the identity. Scrapyd 1.6 honours a client-supplied
        ``jobid`` verbatim, which is what makes a re-POST after
        ``RECONCILED_MISSING`` safe: the duplicate carries the same name,
        so a node that did receive the original request dedups it instead
        of running the batch twice. Passing it explicitly overrides the
        older ``SCRAPYD_DETERMINISTIC_JOBID``-gated fallback (which sent
        the intent row's own id); ``None`` keeps that fallback unchanged
        for callers that have not been migrated.

        ``authorization_id`` (EPA C3/C4) is the caller's C3 grant for this
        dispatch. Unlike ``batch_index`` it IS forwarded to the spider —
        see :meth:`_post_schedule` — so every physical operation the
        spider's own network-ledger boundary (C4's downloader middleware,
        one layer up from this module — ``app_shared`` never names it by
        dotted path, since that dependency runs one way only) opens can
        be traced back to the grant it was authorized under. ``None``
        (the default) omits the field from the POST entirely, exactly
        like the conditional ``jobid`` field below — never sent as the
        literal string ``"None"``.

        ``budget_decision_version`` / ``entitlement_version`` /
        ``breaker_decision`` (EPA Phase C F3) are the three DECISION
        facts that grant was issued on, forwarded the same way and for
        the same reason: C1 gave ``network_operations`` a column for each
        and nothing populated them, so the ledger recorded which grant an
        operation ran under but not what that grant had decided. All
        three are optional and omitted when absent.

        Raises:
            StaleCancellationGenerationError: the durable intent was
                authorized under a superseded ``cancellation_generation``
                (A2's fence). Nothing is claimed and nothing is POSTed.
            ScrapydDispatchError: a concurrent dispatch of this identity
                is already in flight, or Scrapyd refused the schedule.
        """
        key = identity.key if identity is not None else dispatch_key(
            scrape_job_id, batch_index
        )

        # --- 0. reconcile the durable intent, BEFORE any Redis work ----------
        # The guard's TTL is not evidence: a key that aged out says nothing
        # about whether the POST happened. Only the intent row does — and
        # this same read is where A2's cancellation fence is enforced.
        committed = self._reconcile(identity)
        if committed is not None:
            self._heal_guard(key, committed)
            return committed.jobid

        # --- 0b. the durable intent may be POSTED with neither a commitment
        # nor a refusal -- the crash-after-POST window. An aged-out sentinel
        # is not evidence either way; only `reconcile_inflight` (EPA B3b) may
        # authorize what happens next. A no-op (``None``) here means the
        # intent is not POSTED (or there is no durable side at all), so the
        # ordinary claim/POST path below applies unchanged.
        reconciliation = self._reconcile_posted(identity)
        if reconciliation is not None:
            if reconciliation.scrapyd_job_id is not None:
                # A run was found on the node and adopted as this intent's
                # outcome (CONFIRMED; the guard was healed inside
                # `reconcile_inflight`). Never re-POST.
                return reconciliation.scrapyd_job_id
            if not reconciliation.may_repost:
                # AMBIGUOUS: a node was unreachable, or -- flag off -- the
                # run has already left the pending queue where its args
                # lived. Refuse to guess: no claim, no POST.
                raise ScrapydDispatchError(
                    f"cannot verify whether {key!r} was already dispatched "
                    f"to Scrapyd ({reconciliation.detail}); refusing to "
                    "guess and re-POST"
                )
            # ABSENT (every node answered and none knows the job) --
            # `reconcile_inflight` already rolled the intent back to
            # PLANNED. Fall through to claim + POST exactly as a crash
            # BEFORE the POST would (item 6).

        # The remote run's name. When the caller did not name it, ask the
        # durable authority for the one it minted at plan time (EPA B2) —
        # never fall back to something *else*, because an id we POST that
        # disagrees with the id the row records is an intent recovery can
        # no longer find.
        if jobid is None:
            jobid = self._planned_jobid(identity)

        # --- 1. claim --------------------------------------------------------
        # SET NX before the network call: whoever wins the claim owns the POST.
        if not self._redis.set(
            key, _PENDING_SENTINEL, nx=True, ex=_SENTINEL_TTL_SECONDS
        ):
            existing = self._redis.get(key)
            decoded = decode_guard_value(existing)
            if decoded is not None and (
                identity is None
                or decoded.identity_payload == identity.canonical_payload
            ):
                # Already scheduled for THIS identity — return the
                # persisted jobid, do NOT re-POST.
                return decoded.jobid
            if identity is None and existing and existing != _PENDING_SENTINEL:
                # Pre-B1 shape: a bare jobid under a positional key. Only
                # trusted on the legacy path, where the key is all there is.
                return existing
            if existing == _PENDING_SENTINEL or existing is None:
                # Sentinel still present -> a concurrent dispatch is mid-flight.
                # (`None` means it vanished between SET NX and GET — another
                # worker released it; treat that as in-flight too rather than
                # racing it.)
                raise ScrapydDispatchError(
                    f"dispatch already in progress for {key!r}; not double-scheduling"
                )
            # A value that proves nothing about this identity (legacy or
            # corrupt) and no durable commitment behind it. Replace it
            # under a compare-and-set; if it moved, back off rather than
            # racing whoever moved it.
            if not self._replace_stale_guard(key, observed=existing):
                raise ScrapydDispatchError(
                    f"dispatch already in progress for {key!r}; not double-scheduling"
                )

        # --- 2. POST ---------------------------------------------------------
        intent_id = self._record_post(identity, node_url)
        try:
            jobid = self._post_schedule(
                project,
                spider,
                workspace_id=workspace_id,
                scrape_job_id=scrape_job_id,
                match_ids=match_ids,
                mode=mode,
                node_url=node_url,
                identity=identity,
                intent_id=intent_id,
                jobid=jobid,
                authorization_id=authorization_id,
                budget_decision_version=budget_decision_version,
                entitlement_version=entitlement_version,
                breaker_decision=breaker_decision,
            )
        except BaseException as exc:
            # 4. release: never leave a poisoned key behind a failed
            # attempt — but DO leave a durable record that it happened.
            self._fail(identity, exc)
            self._redis.delete(key)
            raise

        # --- 3. commit -------------------------------------------------------
        # Durable first, then the guard: the intent is the authority, so it
        # must never be the thing that is missing after a crash between the
        # two writes. A guard without a CONFIRMED intent is re-derivable
        # (step 0 heals it); a CONFIRMED intent without a guard is not.
        self._confirm(identity, jobid)
        # TTL-bounded (SCRAPYD_DISPATCH_GUARD_TTL_SECONDS): a permanent key
        # suppressed every later legitimate re-dispatch of the same
        # batch_index, which is what wedged DEFERRED targets forever
        # (PLAN_AMAZON_NOON_PRICING Phase 1). Expiry is now merely a cache
        # miss — step 0 re-derives the answer from `dispatch_intents`.
        self._redis.set(
            key,
            self._guard_value(identity, jobid, intent_id),
            ex=self._settings.SCRAPYD_DISPATCH_GUARD_TTL_SECONDS,
        )
        return jobid

    def cancel(self, scrapyd_job_id: str, *, node_url: str | None = None) -> bool:
        """Best-effort ``cancel.json`` for one Scrapyd job id.

        Called by :func:`app_shared.jobs.cancellation.cancel_and_reconcile_job`
        step 2, for every id
        :func:`~app_shared.jobs.cancellation.iter_known_scrapyd_job_ids`
        reports. Returns ``True`` when Scrapyd acknowledged; raises on a
        transport/HTTP failure so the caller's own logging can record
        *which* node could not be reached. Cancelling a remote run is a
        cost optimization, never a correctness requirement — the fence
        already guarantees its results are refused.

        Scrapyd's ``cancel.json`` is per-project, and a jobid alone does
        not name its project, so every configured project is asked; the
        first acknowledgement wins.

        ``node_url`` defaults to ``SCRAPYD_HTTP_URLS[0]`` because the
        cancellation path knows the jobid but not which node answered
        with it. That is a known, accepted limitation of best-effort
        cancellation on a multi-node pool: a run on another node keeps
        going and its results are refused by the fence. Recording the
        node on the intent (so this becomes exact) is B3's to decide,
        alongside the deterministic-jobid question above.
        """
        base = (node_url or self._settings.SCRAPYD_HTTP_URLS[0]).rstrip("/")
        auth = (self._settings.SCRAPYD_USERNAME, self._settings.SCRAPYD_PASSWORD)
        poster = self._session.post if self._session is not None else requests.post
        acknowledged = False
        for project in _CANCELLABLE_PROJECTS:
            response = poster(
                f"{base}/cancel.json",
                data={"project": project, "job": str(scrapyd_job_id)},
                auth=auth,
                timeout=self._timeout,
            )
            if response.status_code >= 400:
                continue
            payload = response.json()
            if isinstance(payload, dict) and payload.get("status") == "ok":
                acknowledged = True
                break
        return acknowledged

    # --- durable-intent seams ------------------------------------------------
    #
    # Each is a one-line no-op when `intents` is None, so the legacy
    # Redis-only posture reads as "the same code with the authority
    # removed" rather than as a second, forked implementation.

    def _reconcile(self, identity: DispatchIdentity | None) -> CommittedDispatch | None:
        if identity is None or self._intents is None:
            return None
        return self._intents.reconcile(identity)

    def _reconcile_posted(self, identity: DispatchIdentity | None) -> Any:
        """Step 0b (EPA B3b): resolve a ``POSTED`` intent before claiming.

        Duck-typed (``getattr``, not the ``DispatchIntentAuthority``
        Protocol) so an authority that predates this method — a hand-rolled
        test double, or any future implementer that has not caught up —
        degrades to "nothing to reconcile" rather than raising
        ``AttributeError``. The one shipping implementation,
        :class:`~app_shared.jobs.dispatch_intents.DispatchIntentStore`,
        always has it: it owns the session
        :func:`app_shared.scrapyd.reconcile.reconcile_inflight` needs, which
        this client deliberately does not hold (see the module docstring).

        Returns ``None`` when there is no durable intent for ``identity``,
        or it is not currently ``POSTED`` — i.e. steps 1-4 proceed exactly
        as before this method existed.
        """
        if identity is None or self._intents is None:
            return None
        reconcile_posted = getattr(self._intents, "reconcile_inflight", None)
        if not callable(reconcile_posted):
            return None
        return reconcile_posted(
            identity, settings=self._settings, lister=self._lister, redis=self._redis
        )

    def _planned_jobid(self, identity: DispatchIdentity | None) -> str | None:
        """The ``scrapyd_job_id`` the authority chose for ``identity``, if any.

        Duck-typed (``getattr``) for the same reason
        :meth:`_reconcile_posted` is: an authority that predates EPA B2 —
        a hand-rolled test double, the legacy ``intents=None`` posture —
        simply has no opinion, and the older
        ``SCRAPYD_DETERMINISTIC_JOBID``-gated fallback in
        :meth:`_post_schedule` still applies for it.
        """
        if identity is None or self._intents is None:
            return None
        planned = getattr(self._intents, "planned_scrapyd_job_id", None)
        if not callable(planned):
            return None
        return planned(identity)

    def _record_post(
        self, identity: DispatchIdentity | None, node_url: str | None = None
    ) -> str | None:
        """Step 2: mark the intent ``POSTED`` — committed, before the POST.

        ``node_url`` is forwarded so the durable row records **which**
        node the request is about to go to (EPA B2): ``node_class`` names
        the pool, but recovery has to ask a specific member. Forwarded
        with ``getattr`` capability detection so an authority that
        predates the keyword (a hand-rolled test double) still works.
        """
        if identity is None or self._intents is None:
            return None
        if node_url:
            try:
                return self._intents.record_post(identity, node_url=node_url)
            except TypeError:
                # Pre-B2 authority without the keyword — degrade, never fail.
                pass
        return self._intents.record_post(identity)

    def _confirm(self, identity: DispatchIdentity | None, jobid: str) -> None:
        if identity is None or self._intents is None:
            return
        self._intents.confirm(identity, jobid)

    def _fail(self, identity: DispatchIdentity | None, exc: BaseException) -> None:
        if identity is None or self._intents is None:
            return
        self._intents.fail(identity, f"{type(exc).__name__}: {exc}")

    # --- Redis guard mechanics ----------------------------------------------

    def _guard_value(
        self, identity: DispatchIdentity | None, jobid: str, intent_id: str | None
    ) -> str:
        """The value written on commit — JSON for B1, a bare jobid for legacy.

        The legacy shape is preserved deliberately: a pre-B1 worker still
        reading these keys must not be handed JSON it would return to
        Celery as a jobid.
        """
        if identity is None:
            return jobid
        return encode_guard_value(
            identity,
            jobid=jobid,
            intent_id=intent_id,
            committed_at=datetime.now(timezone.utc).isoformat(),
        )

    def _heal_guard(self, key: str, committed: CommittedDispatch) -> None:
        """Re-write a guard the durable intent proves should exist.

        Called when step 0 answers from ``dispatch_intents`` — typically
        because the guard's TTL elapsed. Restoring it keeps the *next*
        duplicate delivery on the cheap Redis path instead of hitting the
        database again. Best-effort: a Redis failure here must not turn a
        correctly-suppressed re-POST into an exception.
        """
        payload = json.dumps(
            {
                "jobid": committed.jobid,
                "identity_payload": committed.identity_payload,
                "intent_id": committed.intent_id,
                "committed_at": committed.committed_at,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            self._redis.set(
                key, payload, ex=self._settings.SCRAPYD_DISPATCH_GUARD_TTL_SECONDS
            )
        except Exception:  # noqa: BLE001 - the durable answer already stands
            logger.warning(
                "dispatch.guard_heal_failed key=%s jobid=%s",
                key,
                committed.jobid,
                exc_info=True,
            )

    def _replace_stale_guard(self, key: str, *, observed: str | None) -> bool:
        """Compare-and-set a guard value that proves nothing about this identity.

        The minimal Redis surface this client speaks (``set``/``get``/
        ``delete``) has no native CAS, so this re-reads, deletes only if
        the value has not moved, and re-claims with ``SET NX``. The window
        between the re-read and the delete is real but harmless: the
        durable intent (step 0) is the authority, and the loser of the
        race simply sees the claim fail and reports "in progress".
        """
        if self._redis.get(key) != observed:
            return False
        self._redis.delete(key)
        return bool(
            self._redis.set(key, _PENDING_SENTINEL, nx=True, ex=_SENTINEL_TTL_SECONDS)
        )

    def daemon_status(self, node_url: str) -> dict | None:
        """GET ``/daemonstatus.json`` — ``None`` on any transport/HTTP/parse error.

        The reaper treats ``None`` as 'node dead' (reap-eligible) and a
        healthy payload with ``pending + running > 0`` as 'queued, not
        stalled'. It must therefore never raise: a probe that blew up
        would abort the whole sweep on the very node it exists to
        classify.
        """
        base = node_url.rstrip("/")
        getter = self._session.get if self._session is not None else requests.get
        try:
            response = getter(
                f"{base}/daemonstatus.json",
                auth=(self._settings.SCRAPYD_USERNAME, self._settings.SCRAPYD_PASSWORD),
                timeout=self._timeout,
            )
            if response.status_code >= 400:
                return None
            payload = response.json()
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def list_jobs(self, node_url: str, project: str | None = None) -> set[str]:
        """Every job id ``node_url`` knows — pending, running and finished.

        The read half of EPA B2's step 5. ``reconcile_inflight_intents``
        asks this about the node a ``POSTED`` intent recorded, and the
        answer decides the row's fate: the id is present -> ``CONFIRMED``;
        the id is absent -> ``RECONCILED_MISSING`` (the only state from
        which a re-POST is authorized, and it re-POSTs the same id).

        **An empty set means "the node answered and knows nothing", and
        that distinction is the whole contract.** A node that cannot be
        reached, answers an HTTP error, or returns something that is not
        a JSON object raises :class:`ScrapydDispatchError` — it must never
        collapse into "no such job", because that would authorize a
        re-POST on the strength of a network failure. Contrast
        :meth:`daemon_status`, which deliberately returns ``None`` rather
        than raising: there, "node dead" IS the answer the caller wants.

        Bounded knowledge, deliberately not hidden: both deployed nodes
        run ``MemoryJobStorage`` with ``finished_to_keep = 100``, so a run
        that finished long ago — or before a container restart — is simply
        gone from the listing. Absence here is "no evidence of a run", and
        the same-id re-POST is what makes acting on it safe.

        Args:
            node_url: the node to ask; the one the intent recorded.
            project: the Scrapyd project (the ``node_class`` prefix).
                ``listjobs.json`` needs it to scope the listing; ``None``
                asks for everything the node has.

        Raises:
            ScrapydDispatchError: the node did not answer usefully.
        """
        base = node_url.rstrip("/") if node_url else ""
        if not base:
            raise ScrapydDispatchError(
                "cannot list jobs: the dispatch intent recorded no node_url"
            )
        getter = self._session.get if self._session is not None else requests.get
        params = {"project": project} if project else {}
        try:
            response = getter(
                f"{base}/listjobs.json",
                params=params,
                auth=(self._settings.SCRAPYD_USERNAME, self._settings.SCRAPYD_PASSWORD),
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as a dispatch error
            raise ScrapydDispatchError(
                f"listjobs.json on {base} was unreachable: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code == 401:
            raise ScrapydAuthError(
                f"Scrapyd rejected the listjobs.json credentials on {base} (HTTP 401)"
            )
        if response.status_code >= 400:
            raise ScrapydDispatchError(
                f"listjobs.json on {base} returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - re-raised as a dispatch error
            raise ScrapydDispatchError(
                f"listjobs.json on {base} returned unparseable JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ScrapydDispatchError(
                f"listjobs.json on {base} returned {type(payload).__name__}, not an object"
            )

        known: set[str] = set()
        for bucket in ("pending", "running", "finished"):
            for entry in payload.get(bucket) or ():
                if isinstance(entry, dict) and entry.get("id") is not None:
                    known.add(str(entry["id"]))
        return known

    def _post_schedule(
        self,
        project: str,
        spider: str,
        *,
        workspace_id: str,
        scrape_job_id: str,
        match_ids: Any,
        mode: str,
        node_url: str | None = None,
        identity: DispatchIdentity | None = None,
        intent_id: str | None = None,
        jobid: str | uuid.UUID | None = None,
        authorization_id: str | uuid.UUID | None = None,
        budget_decision_version: str | None = None,
        entitlement_version: str | None = None,
        breaker_decision: str | None = None,
    ) -> str:
        base = (node_url or self._settings.SCRAPYD_HTTP_URLS[0]).rstrip("/")
        url = f"{base}/schedule.json"
        auth = (self._settings.SCRAPYD_USERNAME, self._settings.SCRAPYD_PASSWORD)
        # A list/tuple of match ids MUST be serialized to the spider's
        # comma-separated form here: `requests` urlencodes a list value as
        # repeated form fields, and Scrapyd's schedule.json keeps only ONE
        # of them — every multi-match batch silently collapsed to a single
        # target (found 2026-08-02; the real reason full-catalog runs only
        # advanced ~1 target per domain per dispatch).
        if isinstance(match_ids, (list, tuple)):
            match_ids = ",".join(str(match_id) for match_id in match_ids)
        # Spider args otherwise forwarded UNCHANGED (US4 scenario 3).
        data = {
            "project": project,
            "spider": spider,
            "workspace_id": workspace_id,
            "scrape_job_id": scrape_job_id,
            "match_ids": match_ids,
            "mode": mode,
        }
        # The remote run's name. EPA B2 makes this an explicit argument:
        # the caller chose it at plan time (the intent's `scrapyd_job_id`,
        # a uuid5 over the identity) and committed it BEFORE the POST, so
        # a re-POST authorized by `RECONCILED_MISSING` carries the same
        # value and Scrapyd dedups it. When the caller did not supply one
        # we fall back to the older `SCRAPYD_DETERMINISTIC_JOBID`-gated
        # behaviour of sending the intent row's id; a node that ignores
        # the field just answers with its own jobid either way.
        if jobid is not None:
            data["jobid"] = str(jobid)
        elif intent_id is not None and getattr(
            self._settings, _DETERMINISTIC_JOBID_SETTING, False
        ):
            data["jobid"] = str(intent_id)
        # EPA C4b: the caller's C3 grant, forwarded so the spider's own
        # network-ledger boundary can stamp every physical operation with
        # the authorization it was dispatched under. Omitted entirely when
        # `None` (mirrors the conditional `jobid` field above) — an absent
        # form field, never the literal string `"None"`.
        if authorization_id is not None:
            data["authorization_id"] = str(authorization_id)
        # EPA Phase C F3: the three DECISION facts behind that grant, so
        # C1's `network_operations.entitlement_version` /
        # `budget_decision_version` / `breaker_decision` stop being NULL
        # in production. Same conditional shape as `authorization_id`:
        # omitted entirely when absent, never the string `"None"`.
        for field, value in (
            ("budget_decision_version", budget_decision_version),
            ("entitlement_version", entitlement_version),
            ("breaker_decision", breaker_decision),
        ):
            if value is not None:
                data[field] = str(value)

        poster = self._session.post if self._session is not None else requests.post
        response = poster(url, data=data, auth=auth, timeout=self._timeout)

        if response.status_code == 401:
            raise ScrapydAuthError(
                "Scrapyd rejected the dispatch credentials (HTTP 401); "
                "no run was scheduled"
            )
        if response.status_code >= 400:
            raise ScrapydDispatchError(
                f"Scrapyd schedule.json returned HTTP {response.status_code}"
            )

        payload = response.json()
        if payload.get("status") != "ok" or not payload.get("jobid"):
            raise ScrapydDispatchError(
                f"Scrapyd schedule.json did not return an ok jobid: {payload!r}"
            )
        return str(payload["jobid"])
