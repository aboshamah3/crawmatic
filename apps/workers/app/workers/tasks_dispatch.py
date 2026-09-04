"""Thin Celery task that dispatches ``generic_price_spider`` to Scrapyd.

This is a *thin* wrapper: it delegates all auth + idempotency to
``app_shared.scrapyd.ScrapydDispatchClient``. Full scheduler/orchestration
(job batching, target-state) is a later spec — US4 only needs the authenticated,
idempotent single-batch dispatch.

``stamp_targets_dispatched`` (EPA B2) also lives here — the SINGLE place
that ever writes ``ScrapeJobTarget.dispatched_at``/``.dispatch_intent_id``.
It is imported and called by ``app.workers.tasks_jobs`` (``dispatch_job``,
``recover_stalled_batches``), the two real planner-owned stamping sites;
this module's own thin task has no session and never stamps anything.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.workers.celery_app import app
from app_shared.costauth import (
    FLEET_PROVIDER_BROWSER,
    FLEET_PROVIDER_PROXY,
    AuthorizationPurpose,
    AuthorizationRequest,
    CostAuthorizationDenied,
    CostAuthorizationService,
    estimate_bytes,
    estimate_reservation_micro_units,
)
from app_shared.enums import ScrapeProfileMode, ScrapeTargetStatus
from app_shared.jobs.dispatch_intents import DispatchIntentStore
from app_shared.maintenance.scoping import MaintenanceScope, maintenance_task
from app_shared.models.jobs import ScrapeJobTarget
from app_shared.scrapyd import ScrapydDispatchClient, build_dispatch_identity
from app_shared.scrapyd.client import DispatchIntegrityError
from app_shared.scrapyd.identity import (
    DispatchIdentity,
    decode_guard_value,
    get_committed_dispatch,
)

logger = logging.getLogger(__name__)

# The Scrapy project + spider deployed to the Scrapyd HTTP node (apps/scrapers).
_SCRAPYD_PROJECT = "price_monitor"
_GENERIC_PRICE_SPIDER = "generic_price_spider"

#: The strategy method / planning generation this task can claim to know.
#:
#: It knows neither. This is the SPEC-07 *thin* task: it is handed a
#: match list and a mode by whoever called it and has no session, so it
#: cannot read the job's durable strategy-chain state. Rather than invent
#: a generation (which would make every retry look like a new plan) it
#: pins both to a constant and lets the identity's **work digest** carry
#: the whole discriminating burden — which is still strictly better than
#: the positional `batch_index` key this replaces, because the digest is
#: over the actual match ids. Callers that need generation-aware
#: idempotency use the real planner (`tasks_jobs.dispatch_job`).
_THIN_TASK_GENERATION = 0
_THIN_TASK_STRATEGY_METHOD = "thin-dispatch-task"

#: The domain this task authorizes against (EPA C3). It matches the `"-"`
#: placeholder its dispatch identity already uses for the same reason:
#: the thin task genuinely does not know which domain it is scraping, and
#: naming one it cannot verify would be a false statement to the
#: authorization gate. Under C2's rule table an unregistered domain is
#: `UNKNOWN`, so this resolves to a denial by default — see the call
#: site's comment for why that is the intended posture.
_FALLBACK_DOMAIN = "-"


def _match_id_list(match_ids: object) -> list[str]:
    """Normalize the task's ``match_ids`` argument to a list of id strings.

    Celery hands this through as whatever the caller enqueued — a list, or
    the spider's comma-separated string form. The identity's digest is
    order-insensitive but not *format*-insensitive, so both spellings must
    reduce to the same list or a retry enqueued in the other shape would
    mint a second identity for the same work.
    """
    if isinstance(match_ids, str):
        return [part.strip() for part in match_ids.split(",") if part.strip()]
    if isinstance(match_ids, (list, tuple, set, frozenset)):
        return [str(match_id) for match_id in match_ids]
    return [str(match_ids)]


@maintenance_task(scope=MaintenanceScope.WORKSPACE)
@app.task(name="dispatch.generic_price_spider")
def dispatch_generic_price_spider(
    workspace_id: str,
    scrape_job_id: str,
    match_ids: object,
    mode: str,
    batch_index: int,
) -> str:
    """Schedule one batch of ``generic_price_spider`` and return the jobid.

    Idempotent per :class:`~app_shared.scrapyd.identity.DispatchIdentity`
    (EPA B1) — i.e. per *the work itself*, not per
    ``(scrape_job_id, batch_index)``: an at-least-once Celery retry
    returns the already-persisted jobid without re-scheduling the batch,
    while a genuinely different match set is no longer suppressed by
    having landed on the same ``batch_index``.

    ``batch_index`` is still forwarded to the spider; it is simply not
    part of the idempotency decision any more.
    """
    ids = _match_id_list(match_ids)
    identity = build_dispatch_identity(
        scrape_job_id=scrape_job_id,
        planning_generation=_THIN_TASK_GENERATION,
        strategy_method=_THIN_TASK_STRATEGY_METHOD,
        # This task is not told which domain it is scraping (the spider
        # resolves that per match). A constant keeps the field present and
        # unambiguous instead of silently empty.
        domain="-",
        mode=mode,
        node_class=f"{_SCRAPYD_PROJECT}:{_GENERIC_PRICE_SPIDER}",
        match_ids=ids,
    )

    # EPA C3, paid dispatch site 3 (FALLBACK). This task POSTs a real
    # batch to Scrapyd, so it is a paid dispatch and must clear the gate
    # exactly like the planner's own loop does.
    #
    # It is the ONE site that cannot name its domain: the thin task is
    # handed a match list and resolves domains per-match inside the
    # spider, which is why its dispatch identity already carries the
    # `"-"` placeholder above. Authorizing against `_FALLBACK_DOMAIN`
    # rather than inventing a domain keeps that honest, and it is
    # deliberately fail-CLOSED under C2's rule table: an unregistered
    # domain resolves to `UNKNOWN`, whose broad-crawl gate this FALLBACK
    # purpose does not clear, so the thin task is denied unless an
    # operator has explicitly certified the `-` scope. The planner-owned
    # path (`tasks_jobs.dispatch_job`), which DOES know each batch's
    # domain, is the sanctioned way to dispatch; this task is a legacy
    # SPEC-07 seam and denying it by default is the correct posture, not
    # a regression.
    denial_detail = None
    costauth = CostAuthorizationService(default_workspace_id=workspace_id)
    is_browser = str(mode) == str(ScrapeProfileMode.BROWSER)
    requests = max(1, len(ids))
    try:
        grant = costauth.authorize(
            AuthorizationRequest(
                workspace_id=uuid.UUID(str(workspace_id)),
                domain=_FALLBACK_DOMAIN,
                transport="BROWSER" if is_browser else "PROXY",
                provider=FLEET_PROVIDER_BROWSER if is_browser else FLEET_PROVIDER_PROXY,
                estimated_bytes=estimate_bytes(requests),
                estimated_cost_micro_units=estimate_reservation_micro_units(
                    transport="BROWSER" if is_browser else "PROXY",
                    requests=requests,
                ),
                purpose=AuthorizationPurpose.FALLBACK,
                estimated_requests=requests,
                estimated_browser_seconds=requests * 30 if is_browser else 0,
                scrape_job_id=uuid.UUID(str(scrape_job_id)),
                dedupe_key=identity.key,
            )
        )
    except CostAuthorizationDenied as denial:
        denial_detail = denial
        grant = None

    if grant is None:
        logger.warning(
            "cost_authorization.denied site=tasks_dispatch.dispatch_generic_price_spider "
            "reason=%s workspace_id=%s scrape_job_id=%s detail=%s",
            denial_detail.reason.value if denial_detail else "UNKNOWN",
            workspace_id,
            scrape_job_id,
            denial_detail.detail if denial_detail else "",
        )
        # Raised, not swallowed: this task's caller receives a jobid and
        # would read `None`/`""` as "scheduled". A denial is not a
        # dispatch, and Celery's retry/alerting is the right place for it
        # to land.
        raise denial_detail if denial_detail is not None else RuntimeError(
            "cost authorization denied"
        )

    client = ScrapydDispatchClient()
    try:
        return client.schedule(
            _SCRAPYD_PROJECT,
            _GENERIC_PRICE_SPIDER,
            workspace_id=workspace_id,
            scrape_job_id=scrape_job_id,
            match_ids=match_ids,
            mode=mode,
            batch_index=batch_index,
            identity=identity,
            # EPA C4b: stamp this dispatch's C3 grant onto the spider so
            # its own network-ledger boundary can trace every physical
            # operation back to it.
            authorization_id=grant.authorization_id,
            # EPA Phase C F3: and the three facts it decided on.
            budget_decision_version=grant.budget_decision_version,
            entitlement_version=grant.entitlement_version,
            breaker_decision=grant.breaker_decision,
        )
    except Exception:
        # Failure before dispatch: return the whole hold now rather than
        # waiting out its lease. CAS-idempotent, so a Celery retry cannot
        # double-credit the budget.
        costauth.release(grant.authorization_id)
        raise


# --- EPA B2: the single dispatched_at/dispatch_intent_id stamping path -----


@dataclass(frozen=True)
class DispatchedBatch:
    """One already-POSTed batch's identity + the target rows it covers.

    What :func:`stamp_targets_dispatched` needs and nothing more:
    ``identity`` names the dispatch canonically (EPA B1 —
    :class:`~app_shared.scrapyd.identity.DispatchIdentity`); ``targets``
    are the already-loaded ORM :class:`~app_shared.models.jobs.
    ScrapeJobTarget` rows whose ``match_id`` fell inside this batch,
    filtered by the caller (``tasks_jobs.dispatch_job`` /
    ``recover_stalled_batches``) from the target list it already queried
    — this function never issues a query of its own.
    """

    workspace_id: uuid.UUID
    scrape_job_id: uuid.UUID
    identity: DispatchIdentity
    targets: Sequence[ScrapeJobTarget]


def _describe_found(redis: Any, intents: DispatchIntentStore, identity: DispatchIdentity) -> str:
    """Best-effort description of whatever guard/intent WAS found, for the alert log.

    Never raises: this only decorates the log line
    ``DispatchIntegrityError`` is about to carry, so a failure reading
    the "what we actually found" side must not itself replace the real
    error.
    """
    try:
        raw = redis.get(identity.key) if redis is not None else None
        decoded = decode_guard_value(raw)
        if decoded is not None:
            return decoded.identity_payload
    except Exception:  # noqa: BLE001 - decorative only, never the real error
        pass
    try:
        existing = intents._load(identity)  # noqa: SLF001 - read-only peek for the log line
        if existing is not None:
            return existing.identity_payload
    except Exception:  # noqa: BLE001 - decorative only, never the real error
        pass
    return "none"


def stamp_targets_dispatched(
    session: Session,
    redis: Any,
    *,
    batch: DispatchedBatch,
    stamp: datetime | None = None,
) -> None:
    """Stamp ``batch.targets`` dispatched — the SINGLE writer of the two columns.

    F-2 (2026-08-22)'s contract is preserved unchanged: the state model
    stays ``status=PENDING`` + ``dispatched_at`` — no ``DISPATCHED`` enum
    member exists. What EPA B2 adds is the *proof* requirement: this
    function stamps ``dispatched_at`` and ``dispatch_intent_id`` ONLY
    when :func:`~app_shared.scrapyd.identity.get_committed_dispatch` (B1)
    finds a guard/intent whose ``identity_payload`` matches
    ``batch.identity`` exactly — the Redis guard first (fast path), the
    durable ``dispatch_intents`` row second (consulted via a fresh
    :class:`~app_shared.jobs.dispatch_intents.DispatchIntentStore` bound
    to ``session`` — the SAME transaction the caller stamps in, so the
    read and the stamp are atomic with each other). Anything else —
    nothing found, or a value describing *different* work — raises
    :class:`~app_shared.scrapyd.client.DispatchIntegrityError` and logs
    ``dispatch_integrity_error identity=<payload> found=<...>`` for
    alerting, leaving every target in ``batch.targets`` untouched.

    A target that was ``DEFERRED`` (a requeue-cap handback, SPEC-11) is
    flipped back to ``PENDING`` with its ``error_code`` cleared — an
    ordinary in-flight row again — exactly as the pre-B2 inline stamping
    loops did.

    ``stamp`` defaults to ``datetime.now(timezone.utc)``; callers with
    their own (test-patchable) clock — ``tasks_jobs.dispatch_job`` /
    ``recover_stalled_batches`` — pass one explicitly so a single instant
    is shared by every target in the batch, matching prior behaviour.
    """
    intents = DispatchIntentStore(
        session,
        workspace_id=batch.workspace_id,
        scrape_job_id=batch.scrape_job_id,
    )
    committed = get_committed_dispatch((redis, intents), batch.identity)
    if committed is None:
        found = _describe_found(redis, intents, batch.identity)
        logger.error(
            "dispatch_integrity_error identity=%s found=%s",
            batch.identity.canonical_payload,
            found,
        )
        raise DispatchIntegrityError(
            f"no committed dispatch matches identity {batch.identity.key!r} "
            f"({batch.identity.canonical_payload!r}); refusing to stamp "
            "dispatched_at for a batch nothing confirmed reached Scrapyd"
        )

    intent_id = uuid.UUID(str(committed.intent_id)) if committed.intent_id else None
    effective_stamp = stamp if stamp is not None else datetime.now(timezone.utc)
    for target in batch.targets:
        if target.status is ScrapeTargetStatus.DEFERRED:
            target.status = ScrapeTargetStatus.PENDING
            target.error_code = None
        target.dispatched_at = effective_stamp
        target.dispatch_intent_id = intent_id
