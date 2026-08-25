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
from app_shared.enums import ScrapeTargetStatus
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
    client = ScrapydDispatchClient()
    return client.schedule(
        _SCRAPYD_PROJECT,
        _GENERIC_PRICE_SPIDER,
        workspace_id=workspace_id,
        scrape_job_id=scrape_job_id,
        match_ids=match_ids,
        mode=mode,
        batch_index=batch_index,
        identity=identity,
    )


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
