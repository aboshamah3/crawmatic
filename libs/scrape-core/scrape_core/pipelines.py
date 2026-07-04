"""Batched persistence item pipeline (contracts/persistence-pipeline.md, FR-016).

``BatchedPersistencePipeline`` buffers ``ScrapeResult`` items and
flushes in small batches — never one commit per item — on **either**
threshold (buffer size or elapsed time), plus a final flush at
``close_spider`` so a partial last batch is never lost. Every flush
routes through the single reactor-safe DB seam
(:mod:`scrape_core.db`, ``run_in_thread`` + ``workspace_txn``); no DB
call ever runs on the reactor thread (Principle V, US5).

SPEC-08 T052 (FR-017/018/019, SC-007, ``contracts/lifecycle-counters.md``)
wires this pipeline into the jobs-orchestration result path: every item
that carries a non-null ``scrape_job_id`` also terminalizes its
``scrape_job_targets`` row (COMPLETED on success, FAILED with
``error_code`` otherwise) via ``app_shared.jobs.targets.mark_target``,
in the SAME ``workspace_txn`` transaction as the observation/attempt
writes — no extra reactor hop, no second ``run_in_thread``. Once that
transaction commits, ``_flush_batch`` enqueues ``SCRAPE_FINALIZE_JOBS``
(``app_shared.messaging.enqueue``, queue ``maintenance``) once per
distinct affected ``scrape_job_id`` so ``finalize_jobs`` resolves
counters/status event-driven, without depending on the SPEC-13 beat.
This keeps ``scrape-core`` import-clean: only ``app_shared.jobs.targets``
+ ``app_shared.messaging`` are added, neither of which imports fastapi/
apps.workers/scrapy/twisted/playwright.

SPEC-09 US3 T029 (FR-012/015, SC-007, ``contracts/recompute-triggers.md``
trigger (a)) adds, after that same commit, one ``PRICE_ANALYSIS_RECOMPUTE``
enqueue per distinct affected ``(workspace_id, scrape_job_id,
product_variant_id)`` in the batch. For items carrying a non-null
``scrape_job_id`` a Redis ``SET NX`` key
(``analysis:enqueued:{scrape_job_id}:{product_variant_id}``, TTL
``Settings.PRICE_ANALYSIS_DEDUP_TTL_SECONDS``) is claimed first so many
completed matches of one variant within one job collapse to a single
recompute; ad-hoc items with no ``scrape_job_id`` enqueue directly, no
dedup key. This adds only ``app_shared.redis_client`` (already used
elsewhere in ``app_shared``) to the import closure — still no fastapi/
apps.workers.

US5 hardening (T041) — this module has exactly **one** call site for
``run_in_thread`` (:func:`BatchedPersistencePipeline._flush`), and all
three flush triggers route through it, never a direct/synchronous DB
call on the reactor thread:

1. **Size-based**: :meth:`BatchedPersistencePipeline.process_item`
   calls :meth:`~BatchedPersistencePipeline._flush` once the buffer
   reaches ``_max_items``.
2. **Time-based**: the ``LoopingCall`` started in
   :meth:`~BatchedPersistencePipeline.open_spider` ticks
   :meth:`~BatchedPersistencePipeline._time_based_flush`, which calls
   :meth:`~BatchedPersistencePipeline._flush` when the buffer is
   non-empty.
3. **Final flush**: :meth:`~BatchedPersistencePipeline.close_spider`
   calls :meth:`~BatchedPersistencePipeline._flush` on any remaining
   partial buffer and awaits every in-flight ``Deferred`` (including
   this one) before the spider actually closes.

There is no ``time.sleep`` and no direct/synchronous ``session.commit()``
anywhere on the reactor thread — the batch build in :func:`_flush_batch`
is pure Python + SQLAlchemy object construction that only touches the
database (and, post-commit, Redis/the Celery producer for the
``SCRAPE_FINALIZE_JOBS``/``PRICE_ANALYSIS_RECOMPUTE`` enqueues) once it is
already running inside the ``run_in_thread`` thread-pool thread, never on
the reactor thread itself. ``_flush`` also swaps
``self._buffer`` for a fresh list *before* dispatching the batch, so
the buffer is emptied immediately regardless of whether that flush
later succeeds or fails (see :func:`_flush`'s docstring and
:meth:`BatchedPersistencePipeline._on_flush_failure`) — a failed flush
loses only its own batch, never blocks or wedges subsequent flushes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from twisted.internet.defer import Deferred, DeferredList
from twisted.internet.task import LoopingCall
from twisted.python.failure import Failure

from app_shared.catalog.upsert import dedup_last_wins
from app_shared.config import get_settings
from app_shared.enums import ScrapeErrorCode, ScrapeTargetStatus
from app_shared.ids import new_uuid7
from app_shared.jobs.targets import mark_target
from app_shared.limiter.locks import release_match_lock
from app_shared.messaging import enqueue
from app_shared.models.observations import MatchCurrentPrice, PriceObservation, RequestAttempt
from app_shared.redis_client import get_redis_client
from app_shared.task_names import PRICE_ANALYSIS_RECOMPUTE, SCRAPE_FINALIZE_JOBS

from scrape_core.db import run_in_thread, workspace_txn
from scrape_core.items import ScrapeResult
from scrape_core.observability import log_event

__all__ = ["BatchedPersistencePipeline"]

logger = logging.getLogger(__name__)

# match_current_prices columns overwritten by ON CONFLICT DO UPDATE (never
# id/workspace_id/match_id/created_at — the conflict arbiter + immutable
# identity columns).
_CURRENT_PRICE_UPDATABLE_COLUMNS: tuple[str, ...] = (
    "product_id",
    "product_variant_id",
    "competitor_id",
    "price",
    "old_price",
    "currency",
    "stock_status",
    "comparable",
    "observation_id",
    "success",
    "error_code",
    "extraction_method",
    "extraction_confidence",
    "scraped_at",
)


def _current_price_key(row: dict[str, Any]) -> tuple[Any, Any]:
    return (row["workspace_id"], row["match_id"])


def _flush_batch(workspace_id: Any, batch: list[ScrapeResult]) -> None:
    """Persist one batch in a single transaction (runs inside ``run_in_thread``).

    Bulk-inserts a ``PriceObservation`` + a ``RequestAttempt`` row per
    item, then upserts ``match_current_prices`` for the batch's
    **successful** items only — a failure/rejected item is simply absent
    from that insert, so ``ON CONFLICT`` never fires for it and the
    current price is never overwritten with a bad value (FR-014).

    SPEC-08 T052: for every item that carries a non-null
    ``scrape_job_id``, also terminalizes its ``scrape_job_targets`` row
    (COMPLETED on ``item.success``, FAILED with ``item.error_code``, or
    — SPEC-11 US2 — SKIPPED for a ``LOCKED_ALREADY_RUNNING`` lock
    collision) via ``mark_target`` — in this SAME transaction, no second
    ``run_in_thread``/reactor hop. Once the transaction commits, enqueues
    ``SCRAPE_FINALIZE_JOBS`` (``maintenance`` queue) exactly once per
    distinct affected ``scrape_job_id`` in the batch, so counters/status
    finalize event-driven (FR-017/018/019, SC-007).

    SPEC-11 US2 (T023, ``contracts/match-lock.md`` "Ownership lifecycle"
    step 3): once that same transaction has committed, releases each
    item's in-flight match lock (``release_match_lock``, a Lua
    compare-and-delete keyed by its fencing token) — still inside this
    SAME off-reactor flush, no new reactor hop. An item with no
    ``match_lock_token`` (e.g. the SKIPPED/not-dispatched path never
    acquired a lock) is skipped — no release attempted. Release errors
    are logged and swallowed inside ``release_match_lock`` itself (D3) —
    never fails the flush.

    SPEC-11 US4 (T032, ``contracts/observability.md``): each release
    attempt also emits one structured ``dedup.release`` JSON log line
    (workspace_id, match_id, released) right after ``release_match_lock``
    returns, so lock releases are observable the same way the spider's
    rate-limit/lock-collision events are (T031). The ``RATE_LIMITED``/
    ``LOCKED_ALREADY_RUNNING`` codes reaching this module via
    ``ScrapeResult.error_code`` already flow through the single
    ``mark_target`` writer above (T026's broadened gate) — no new
    persistence path is added here.
    """
    observations: list[PriceObservation] = []
    attempts: list[RequestAttempt] = []
    current_price_rows: list[dict[str, Any]] = []

    for item in batch:
        observation_id = new_uuid7()
        moment = item.scraped_at or datetime.now(UTC)

        observations.append(
            PriceObservation(
                id=observation_id,
                workspace_id=item.workspace_id,
                scraped_at=moment,
                match_id=item.match_id,
                product_id=item.product_id,
                product_variant_id=item.product_variant_id,
                scrape_job_id=item.scrape_job_id,
                price=item.price,
                old_price=item.old_price,
                currency=item.currency,
                stock_status=item.stock_status,
                raw_title=item.raw_title,
                success=item.success,
                comparable=item.comparable,
                error_code=item.error_code,
                error_message=item.error_message,
                extraction_method=item.extraction_method,
                extraction_confidence=item.extraction_confidence,
                selector_used=item.selector_used,
            )
        )
        attempts.append(
            RequestAttempt(
                workspace_id=item.workspace_id,
                created_at=moment,
                scrape_job_id=item.scrape_job_id,
                match_id=item.match_id,
                attempt_number=item.attempt_number,
                url=item.url,
                access_method=item.access_method,
                proxy_provider_id=item.proxy_provider_id,
                proxy_country=item.proxy_country,
                status_code=item.status_code,
                response_time_ms=item.response_time_ms,
                success=item.success,
                error_code=item.error_code,
                error_message=item.error_message,
            )
        )
        if item.success:
            current_price_rows.append(
                {
                    "workspace_id": item.workspace_id,
                    "match_id": item.match_id,
                    "product_id": item.product_id,
                    "product_variant_id": item.product_variant_id,
                    "competitor_id": item.competitor_id,
                    "price": item.price,
                    "old_price": item.old_price,
                    "currency": item.currency,
                    "stock_status": item.stock_status,
                    "comparable": item.comparable,
                    "observation_id": observation_id,
                    "success": item.success,
                    "error_code": item.error_code,
                    "extraction_method": item.extraction_method,
                    "extraction_confidence": item.extraction_confidence,
                    "scraped_at": moment,
                }
            )

    affected_job_ids: dict[Any, None] = {}  # insertion-ordered de-dup set

    with workspace_txn(workspace_id) as session:
        session.add_all(observations)
        session.add_all(attempts)

        if current_price_rows:
            # A batch may carry more than one successful observation for the
            # same match (e.g. a retried attempt within one run) -- collapse
            # to the last-wins row per (workspace_id, match_id) so the single
            # multi-row INSERT never targets the same conflict arbiter twice
            # (Postgres rejects that within one statement).
            deduped = dedup_last_wins(current_price_rows, key_fn=_current_price_key)
            stmt = pg_insert(MatchCurrentPrice).values(list(deduped))
            set_ = {col: stmt.excluded[col] for col in _CURRENT_PRICE_UPDATABLE_COLUMNS}
            set_["updated_at"] = func.now()
            stmt = stmt.on_conflict_do_update(
                index_elements=["workspace_id", "match_id"], set_=set_
            )
            session.execute(stmt)

        # T052: terminalize each item's target in this SAME transaction --
        # no second run_in_thread/reactor hop. An item with no
        # scrape_job_id (e.g. a non-orchestrated/ad-hoc scrape) marks
        # nothing.
        for item in batch:
            if item.scrape_job_id is None:
                continue
            if item.success:
                target_status = ScrapeTargetStatus.COMPLETED
            elif item.error_code == ScrapeErrorCode.LOCKED_ALREADY_RUNNING:
                # SPEC-11 US2 (contracts/match-lock.md, data-model.md §5):
                # a lock-collision attempt is SKIPPED, distinct from every
                # other failure outcome -- not FAILED. `mark_target`'s
                # error_code stamp is currently gated to `status ==
                # FAILED` only, so this writes status=SKIPPED with the
                # error_code still dropped until that gate is broadened
                # (US3 T026, tasks.md) -- a known, tracked gap, not a new
                # persistence path.
                target_status = ScrapeTargetStatus.SKIPPED
            else:
                target_status = ScrapeTargetStatus.FAILED
            mark_target(
                session,
                workspace_id=item.workspace_id,
                scrape_job_id=item.scrape_job_id,
                match_id=item.match_id,
                status=target_status,
                error_code=None if item.success else item.error_code,
            )
            affected_job_ids[item.scrape_job_id] = None

    # SPEC-11 US2 (T023): release each item's match lock only AFTER the
    # transaction above has committed -- still inside this same
    # off-reactor flush (no second run_in_thread/reactor hop). An item
    # with no match_lock_token never acquired a lock -- skipped, no
    # release attempted. Release errors are logged + swallowed inside
    # `release_match_lock` itself (D3) -- never fails the flush.
    redis = get_redis_client()
    for item in batch:
        if item.match_lock_token is None:
            continue
        released = release_match_lock(redis, key=item.match_lock_key, token=item.match_lock_token)
        # SPEC-11 US4 (T032, contracts/observability.md): one `dedup.release`
        # per lock-release attempt -- `released=False` is not itself an
        # error (a stale/foreign token is a correct no-op, US2 AS3; a
        # swallowed Redis error inside `release_match_lock` also reports
        # `False` here, D3), just an observable outcome.
        log_event(
            logger,
            "dedup.release",
            workspace_id=item.workspace_id,
            match_id=item.match_id,
            released=released,
        )

    # Only after the transaction above has committed cleanly -- enqueue one
    # SCRAPE_FINALIZE_JOBS per distinct affected job so finalize_jobs()
    # never races the target rows it is about to aggregate.
    for job_id in affected_job_ids:
        enqueue(SCRAPE_FINALIZE_JOBS, queue="maintenance")

    # SPEC-09 US3 T029 (contracts/recompute-triggers.md trigger (a)): also
    # after the same commit, enqueue one PRICE_ANALYSIS_RECOMPUTE per
    # distinct affected (workspace_id, scrape_job_id, product_variant_id)
    # in the batch. For items that belong to a job, claim a Redis SET NX
    # dedup key first so many completed matches of one variant within one
    # job collapse to a single recompute (SC-007) -- a contention reducer,
    # not a correctness guard, since recompute_variant is idempotent.
    # Ad-hoc items with no scrape_job_id enqueue directly, no dedup key.
    dedup_ttl = get_settings().PRICE_ANALYSIS_DEDUP_TTL_SECONDS
    seen_variant_jobs: set[tuple[Any, Any, Any]] = set()
    for item in batch:
        key = (item.workspace_id, item.scrape_job_id, item.product_variant_id)
        if key in seen_variant_jobs:
            continue
        seen_variant_jobs.add(key)

        if item.scrape_job_id is not None:
            redis_key = f"analysis:enqueued:{item.scrape_job_id}:{item.product_variant_id}"
            if not get_redis_client().set(redis_key, "1", nx=True, ex=dedup_ttl):
                continue  # another completed match of this variant already enqueued this job

        enqueue(
            PRICE_ANALYSIS_RECOMPUTE,
            queue="price_analysis",
            kwargs={
                "workspace_id": str(item.workspace_id),
                "product_variant_id": str(item.product_variant_id),
                "product_id": str(item.product_id),
                "scrape_job_id": None if item.scrape_job_id is None else str(item.scrape_job_id),
            },
        )


class BatchedPersistencePipeline:
    """Scrapy item pipeline: buffer ``ScrapeResult`` items, flush in small batches."""

    def __init__(self, max_items: int, interval_seconds: float) -> None:
        self._max_items = max_items
        self._interval_seconds = interval_seconds
        self._buffer: list[ScrapeResult] = []
        self._pending: list[Deferred] = []
        self._looping_call: LoopingCall | None = None

    @classmethod
    def from_crawler(cls, crawler: Any) -> "BatchedPersistencePipeline":
        # T041 guard rail: thresholds are always read from config
        # (`Settings.SCRAPE_FLUSH_MAX_ITEMS`/`SCRAPE_FLUSH_INTERVAL_SECONDS`,
        # env/DB-tunable), never hardcoded literals here -- a Scrapy-level
        # settings override (`crawler.settings`) still wins if present, so
        # a spider/project can tune it without touching this module.
        settings = get_settings()
        max_items = crawler.settings.getint(
            "SCRAPE_FLUSH_MAX_ITEMS", settings.SCRAPE_FLUSH_MAX_ITEMS
        )
        interval_seconds = crawler.settings.getfloat(
            "SCRAPE_FLUSH_INTERVAL_SECONDS", settings.SCRAPE_FLUSH_INTERVAL_SECONDS
        )
        return cls(max_items=max_items, interval_seconds=interval_seconds)

    def open_spider(self, spider: Any) -> None:
        self._looping_call = LoopingCall(self._time_based_flush)
        # now=False: the first tick fires after interval_seconds, not
        # immediately on an (initially empty) buffer.
        self._looping_call.start(self._interval_seconds, now=False)

    def process_item(self, item: Any, spider: Any) -> Any:
        if not isinstance(item, ScrapeResult):
            return item
        self._buffer.append(item)
        if len(self._buffer) >= self._max_items:
            self._flush()
        return item

    def close_spider(self, spider: Any) -> Deferred:
        if self._looping_call is not None and self._looping_call.running:
            self._looping_call.stop()
        if self._buffer:
            self._flush()
        # Wait for every in-flight (and this final) flush before the spider
        # actually closes, so a partial last batch is never lost.
        return DeferredList(list(self._pending), consumeErrors=True)

    def _time_based_flush(self) -> None:
        if self._buffer:
            self._flush()

    def _flush(self) -> Deferred:
        """Swap out the current buffer and dispatch it off the reactor thread.

        Swapping ``self._buffer`` for a fresh list is the whole
        concurrency story here: the reactor is single-threaded, so this
        happens atomically with respect to further ``process_item``
        calls — no lock needed, and a size- and a time-triggered flush
        can never race on the same items.
        """
        batch = self._buffer
        self._buffer = []
        workspace_id = batch[0].workspace_id

        deferred = run_in_thread(_flush_batch, workspace_id, batch)
        deferred.addErrback(self._on_flush_failure, batch=batch)
        self._pending.append(deferred)
        deferred.addBoth(self._forget_pending, deferred=deferred)
        return deferred

    def _forget_pending(self, result: Any, *, deferred: Deferred) -> Any:
        if deferred in self._pending:
            self._pending.remove(deferred)
        return result

    def _on_flush_failure(self, failure: Failure, *, batch: list[ScrapeResult]) -> None:
        # A persistence failure must never crash the reactor/spider run --
        # log it and move on; the affected items are lost from this flush
        # (no retry queue in this MVP slice) but every other flush proceeds.
        logger.error(
            "BatchedPersistencePipeline: flush failed for %d item(s): %s",
            len(batch),
            failure.getErrorMessage(),
        )
        return None
