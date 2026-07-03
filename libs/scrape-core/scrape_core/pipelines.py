"""Batched persistence item pipeline (contracts/persistence-pipeline.md, FR-016).

``BatchedPersistencePipeline`` buffers ``ScrapeResult`` items and
flushes in small batches — never one commit per item — on **either**
threshold (buffer size or elapsed time), plus a final flush at
``close_spider`` so a partial last batch is never lost. Every flush
routes through the single reactor-safe DB seam
(:mod:`scrape_core.db`, ``run_in_thread`` + ``workspace_txn``); no DB
call ever runs on the reactor thread (Principle V, US5).
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
from app_shared.ids import new_uuid7
from app_shared.models.observations import MatchCurrentPrice, PriceObservation, RequestAttempt

from scrape_core.db import run_in_thread, workspace_txn
from scrape_core.items import ScrapeResult

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
