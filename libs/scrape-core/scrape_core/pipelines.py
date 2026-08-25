"""Batched persistence item pipeline (contracts/persistence-pipeline.md, FR-016).

``BatchedPersistencePipeline`` buffers ``ScrapeResult`` items and
flushes in small batches — never one commit per item — on **either**
threshold (buffer size or elapsed time), plus a final flush at
``close_spider`` so a partial last batch is never lost. Every flush
routes through the single reactor-safe DB seam
(:mod:`scrape_core.db`, ``run_in_thread`` + ``workspace_txn``); no DB
call ever runs on the reactor thread (Principle V, US5).

SPEC-08 T052 (FR-017/018/019, SC-007, ``contracts/lifecycle-counters.md``)
wires this pipeline into the jobs-orchestration result path. Every item is
persisted, while an item carrying a non-null ``scrape_job_id`` transitions its
``scrape_job_targets`` row only on success, explicit defer/skip, or an
explicitly chain-complete failure. Intermediate failure attempts remain
non-terminal until their chain owner emits the final outcome. Transitions use
``app_shared.jobs.targets.mark_target`` in the SAME ``workspace_txn``
transaction as the observation/attempt writes — no extra reactor hop, no
second ``run_in_thread``. Once that transaction commits, ``finalize_jobs``
resolves counters/status event-driven, without depending on the SPEC-13 beat.

2026-08-15 (audit risk H1): the two follow-ups this flush produces —
``SCRAPE_FINALIZE_JOBS`` once per distinct affected ``scrape_job_id``,
and ``PRICE_ANALYSIS_RECOMPUTE`` once per distinct affected
``(workspace_id, scrape_job_id, product_variant_id)`` (SPEC-09 US3 T029,
FR-012/015, SC-007, ``contracts/recompute-triggers.md`` trigger (a)) —
are no longer post-commit ``app_shared.messaging.enqueue`` calls. They
are ``outbox_messages`` rows written **inside the same
``workspace_txn`` transaction** as the observations/attempts/target
terminalisation, and published to Celery afterwards by the outbox
dispatcher. The broker therefore still never sits on the persistence
path (this is a plain INSERT), but a Redis blip or a spider process
dying right after ``COMMIT`` can no longer strand committed
observations with no analysis and a job that never finalizes.

The Redis ``SET NX`` key
(``analysis:enqueued:{scrape_job_id}:{product_variant_id}``, TTL
``Settings.PRICE_ANALYSIS_DEDUP_TTL_SECONDS``) is retained as a pure
contention reducer so many completed matches of one variant within one
job collapse to a single recompute; it now fails **open** on a Redis
error, because durability no longer depends on it. Import closure is
unchanged apart from ``app_shared.outbox`` (plain SQLAlchemy) — still no
fastapi/apps.workers/scrapy in ``app_shared``.

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
database (and Redis, for the lock releases and the best-effort analysis
dedup claim) once it is
already running inside the ``run_in_thread`` thread-pool thread, never on
the reactor thread itself. ``_flush`` also swaps
``self._buffer`` for a fresh list *before* dispatching the batch, so
the buffer is emptied immediately regardless of whether that flush
later succeeds or fails (see :func:`_flush`'s docstring and
:meth:`BatchedPersistencePipeline._on_flush_failure`) — a failed flush
loses only its own batch, never blocks or wedges subsequent flushes.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from twisted.internet.defer import Deferred, DeferredList
from twisted.internet.task import LoopingCall
from twisted.python.failure import Failure

from app_shared.config import get_settings
from app_shared.enums import (
    MatchClassificationState,
    MethodType,
    ScrapeErrorCode,
    ScrapeTargetStatus,
    StockStatus,
)
from app_shared.ids import new_uuid7
from app_shared.jobs.cancellation import LATE_AFTER_CANCEL_REASON, cancelled_scrape_job_ids
from app_shared.jobs.targets import mark_target
from app_shared.limiter.locks import release_match_lock
from app_shared.models.observations import MatchCurrentPrice, PriceObservation, RequestAttempt
from app_shared.models.jobs import ScrapeJobTarget
from app_shared.outbox import write_outbox_message
from app_shared.redis_client import get_redis_client
from app_shared.strategy.stats_buffer import record_attempt
from app_shared.strategy.methods import is_method_health_failure

from scrape_core.defer_budget import consume_defer_budget
from app_shared.task_names import (
    PRICE_ANALYSIS_RECOMPUTE,
    SCRAPE_DISPATCH_JOB,
    SCRAPE_FINALIZE_JOBS,
)

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

# 2026-08-09 (PLAN_AMAZON_PRICE_FIX §B, problem 4): the columns an
# out-of-stock FAILURE is allowed to overwrite on an existing
# match_current_prices row. Deliberately a strict subset of
# _CURRENT_PRICE_UPDATABLE_COLUMNS with every price/extraction column
# removed (price, old_price, currency, comparable, observation_id,
# extraction_method, extraction_confidence, and the identity columns) --
# the row keeps its last known price, which the plugin renders next to the
# "unavailable" badge, and FR-014 ("a failure observation never overwrites
# the current price") still holds literally.
_CURRENT_PRICE_OUT_OF_STOCK_UPDATABLE_COLUMNS: tuple[str, ...] = (
    "stock_status",
    "success",
    "error_code",
    "scraped_at",
)


def _current_price_key(row: dict[str, Any]) -> tuple[Any, Any]:
    return (row["workspace_id"], row["match_id"])


# READY-013-g. `match_current_prices` is a projection of the observation
# stream, and jobs do not finish in the order they started: a slow
# spider, a retried attempt or a re-queued batch routinely lands a result
# *after* a newer scrape of the same match has already been projected.
# Nothing below is about cancellation (that is the A2 fence, which
# refuses a cancelled job's result outright) -- this is the ordinary,
# never-cancelled straggler, and without a guard it wins the projection
# purely by committing last, silently walking the customer-visible price
# backwards in time.
#
# The guard is a compare-and-set evaluated by Postgres *inside* the
# UPDATE, deliberately not a read-then-write in Python: two workers
# flushing concurrently would both read the same stored row, both
# conclude they are newer, and the later COMMIT would still win. As an
# `ON CONFLICT ... DO UPDATE ... WHERE`, a losing writer's UPDATE simply
# matches no row -- no error, no retry, no lost newer truth.
#
# Tie-break at an identical `scraped_at` (two attempts stamped the same
# instant): the larger `observation_id` wins. Ids are uuid7, so that is
# "the observation issued later", and it is total and deterministic
# rather than statement-order-dependent. The out-of-stock statement
# carries no `observation_id` (it must not overwrite that column,
# FR-014), so `excluded.observation_id IS NOT NULL` makes it lose every
# tie instead of comparing against NULL -- also deterministic, and in the
# safe direction (the row keeps its last known price).
def _monotonic_conflict_where(stmt: Any) -> Any:
    excluded = stmt.excluded
    return or_(
        MatchCurrentPrice.scraped_at < excluded.scraped_at,
        and_(
            MatchCurrentPrice.scraped_at == excluded.scraped_at,
            excluded.observation_id.isnot(None),
            or_(
                MatchCurrentPrice.observation_id.is_(None),
                MatchCurrentPrice.observation_id < excluded.observation_id,
            ),
        ),
    )


def _observation_order(row: dict[str, Any]) -> tuple[Any, bool, Any]:
    """Sort key mirroring `_monotonic_conflict_where`, for the batch-local collapse.

    The same ordering has to be applied twice: once by Postgres, against
    the row already stored, and once here, against the other rows in this
    very batch (which never reach the conflict arbiter -- Postgres
    rejects two rows hitting one arbiter inside a single statement, so
    the batch must collapse to one row per match *before* it is sent).
    Collapsing by list position instead would let a stale item that
    merely sits later in the buffer beat a fresher one.
    """
    observation_id = row.get("observation_id")
    return (row["scraped_at"], observation_id is not None, observation_id)


def _dedup_newest_wins(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse to one row per match, keeping the newest observation.

    Stable in first-appearance order (like ``dedup_last_wins``, which
    this replaces here) so the emitted statement's row order stays
    deterministic -- only *which* row survives each key changes.
    """
    position_by_key: dict[tuple[Any, Any], int] = {}
    result: list[dict[str, Any]] = []
    for row in rows:
        key = _current_price_key(row)
        position = position_by_key.get(key)
        if position is None:
            position_by_key[key] = len(result)
            result.append(row)
        elif _observation_order(row) > _observation_order(result[position]):
            result[position] = row
    return result


#: EPA B6 (folded-in item 2). Distinct from the offline batch classifier's
#: own `scripts/classify_match_set.py::CLASSIFIER_VERSION` ("1") -- this
#: writer's evidence is per-attempt and automatic, from the live scrape
#: path, never a full-match-set audit run's evidence, so the two must
#: never be silently conflated by sharing one version tag.
_NEEDS_REVIEW_CLASSIFIER_VERSION = "live-scrape-b6-needs-review-1"


def _write_needs_review_classifications(
    session: Any, match_ids: list[Any], *, effective_at: datetime
) -> None:
    """Append-only NEEDS_REVIEW write for the live scrape path (EPA B4/B6).

    B4's adapter sets `metadata={"needs_review": True}` on an
    `Ambiguous`/`IdentityIncompatible` variant resolution
    (`scrape_core.adapters.variant_resolution`), carried through as
    `ScrapeResult.needs_review` by the spiders' `_build_result` ->
    `build_scrape_result`. Nothing wrote it to the versioned
    `match_audit_classifications` sidecar (A6, `b8f3d61c9e02`) on the
    live scrape path until this function.

    Mirrors A6's own writer, `scripts/classify_match_set.py::
    apply_classifications`, exactly: bulk-supersede whichever row is
    currently CURRENT for each affected match (`superseded_at IS
    NULL`), then bulk-insert one fresh `NEEDS_REVIEW` row per match --
    one batched SELECT, one batched UPDATE, one executemany-batched
    INSERT, never a per-match round trip. Called from inside
    `_flush_batch`'s existing `workspace_txn` transaction -- no second
    `run_in_thread`/reactor hop, same as every other write in that
    function.

    Unconditional per match (never checks whether the current row is
    already `NEEDS_REVIEW`): a second needs-review attempt is still a
    new piece of evidence, and A6's model is append-only by design (see
    `app_shared.models.match_audit.MatchAuditClassification`'s
    docstring) -- superseding an identical-state row and inserting a
    fresh one is the correct behavior, not redundant churn.

    Does NOT reclassify a match back to `ACTIVE`/other states on a
    later success -- that reconciliation is the offline batch
    classifier's job (`scripts/classify_match_set.py`), deliberately
    out of scope for this live, single-purpose writer.
    """
    existing_rows = session.execute(
        text(
            "SELECT match_id FROM match_audit_classifications "
            "WHERE superseded_at IS NULL AND match_id = ANY(:match_ids)"
        ),
        {"match_ids": match_ids},
    ).all()
    to_supersede = [row.match_id for row in existing_rows]
    if to_supersede:
        session.execute(
            text(
                "UPDATE match_audit_classifications SET superseded_at = :now "
                "WHERE superseded_at IS NULL AND match_id = ANY(:match_ids)"
            ),
            {"now": effective_at, "match_ids": to_supersede},
        )

    insert_params = [
        {
            "id": new_uuid7(),
            "match_id": match_id,
            "state": MatchClassificationState.NEEDS_REVIEW.value,
            "classifier_version": _NEEDS_REVIEW_CLASSIFIER_VERSION,
            "evidence": json.dumps({"source": "live_scrape_pipeline"}),
            "reviewer": None,
            "effective_at": effective_at,
        }
        for match_id in match_ids
    ]
    session.execute(
        text(
            """
            INSERT INTO match_audit_classifications
                (id, match_id, state, classifier_version, evidence, reviewer,
                 effective_at, superseded_at)
            VALUES
                (:id, :match_id, :state, :classifier_version,
                 CAST(:evidence AS jsonb), :reviewer, :effective_at, NULL)
            """
        ),
        insert_params,
    )


def _flush_batch(workspace_id: Any, batch: list[ScrapeResult]) -> None:
    """Persist one batch in a single transaction (runs inside ``run_in_thread``).

    Bulk-inserts a ``PriceObservation`` + a ``RequestAttempt`` row per
    item, then upserts ``match_current_prices`` for the batch's
    **successful** items only — a failure/rejected item is simply absent
    from that insert, so ``ON CONFLICT`` never fires for it and the
    current price is never overwritten with a bad value (FR-014).

    2026-08-09 (PLAN_AMAZON_PRICE_FIX §B, problem 4) — one narrow
    exception: a **failure item carrying ``stock_status =
    OUT_OF_STOCK``** (the spider sniffed an "unavailable" marker on a page
    with no price) upserts its row too, through a second statement whose
    ``ON CONFLICT`` update set is restricted to
    ``stock_status``/``success``/``error_code``/``scraped_at``
    (+``updated_at``). No price/currency/extraction column is ever
    written, so the last known price survives beside the unavailable
    badge and FR-014 still holds; every other failure remains absent from
    both statements exactly as before.

    SPEC-08 T052: for every item that carries a non-null
    ``scrape_job_id``, also terminalizes its ``scrape_job_targets`` row
    (COMPLETED on ``item.success``, FAILED with ``item.error_code``, or
    — SPEC-11 US2 — SKIPPED for a ``LOCKED_ALREADY_RUNNING`` lock
    collision) via ``mark_target`` — in this SAME transaction, no second
    ``run_in_thread``/reactor hop. In that SAME transaction it also
    records one outbox message for ``SCRAPE_FINALIZE_JOBS``
    (``maintenance`` queue) per distinct affected ``scrape_job_id`` in
    the batch, so counters/status finalize event-driven
    (FR-017/018/019, SC-007) and can no longer be lost between COMMIT and
    the broker (audit H1).

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
    out_of_stock_rows: list[dict[str, Any]] = []
    # Which of the two upserts owns a given (workspace_id, match_id) --
    # decided by the NEWEST observation for that match (READY-013-g), not
    # by batch order; see the split just below the loop.
    winning_row_kind: dict[tuple[Any, Any], tuple[tuple[Any, bool, Any], str]] = {}

    def _claim(row: dict[str, Any], kind: str) -> None:
        key = _current_price_key(row)
        order = _observation_order(row)
        current = winning_row_kind.get(key)
        if current is None or order > current[0]:
            winning_row_kind[key] = (order, kind)

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
                strategy_method_id=item.strategy_method_id,
                scrape_profile_id=item.scrape_profile_id,
                scrape_profile_version=item.scrape_profile_version,
                adapter_key=item.adapter_key,
                attempt_number=item.attempt_number,
                url=item.url,
                final_url=item.final_url,
                identity_validation_result=item.identity_validation_result,
                terminal_for_target=item.terminal_for_target,
                access_method=item.access_method,
                proxy_provider_id=item.proxy_provider_id,
                proxy_country=item.proxy_country,
                status_code=item.status_code,
                response_time_ms=item.response_time_ms,
                success=item.success,
                error_code=item.error_code,
                error_message=item.error_message,
                # EPA B6: transport-observed byte accounting -- both
                # default `None` ("not measured for this attempt"), never
                # coerced to 0 (see the columns' own docstrings).
                main_document_bytes=item.main_document_bytes,
                subresource_bytes=item.subresource_bytes,
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
            _claim(current_price_rows[-1], "success")
        elif item.stock_status == StockStatus.OUT_OF_STOCK:
            # 2026-08-09 (problem 4): a failure that knows *why* there was
            # no price -- the product is unavailable on the competitor's
            # site. Without this the OOS state never left price_observations
            # and the plugin rendered an indistinguishable blank. The row
            # carries only what an out-of-stock failure legitimately knows;
            # the price/extraction columns are present solely so a
            # first-ever INSERT satisfies the NOT NULL `comparable` and
            # leaves the rest NULL -- ON CONFLICT updates none of them
            # (_CURRENT_PRICE_OUT_OF_STOCK_UPDATABLE_COLUMNS).
            out_of_stock_rows.append(
                {
                    "workspace_id": item.workspace_id,
                    "match_id": item.match_id,
                    "product_id": item.product_id,
                    "product_variant_id": item.product_variant_id,
                    "competitor_id": item.competitor_id,
                    "comparable": item.comparable,
                    "stock_status": item.stock_status,
                    "success": item.success,
                    "error_code": item.error_code,
                    "scraped_at": moment,
                }
            )
            _claim(out_of_stock_rows[-1], "out_of_stock")

    # One match can produce both kinds within a single batch (a retried
    # attempt that finally found a price after an out-of-stock miss, or the
    # reverse). The collapse below only works *within* a list, so the
    # loser kind is dropped here first -- otherwise the two INSERTs would
    # both hit the same conflict arbiter and the second one would win
    # purely by statement order. READY-013-g: the winner is the match's
    # newest observation, not whichever kind appeared last in the batch.
    if current_price_rows and out_of_stock_rows:
        current_price_rows = [
            row
            for row in current_price_rows
            if winning_row_kind[_current_price_key(row)][1] == "success"
        ]
        out_of_stock_rows = [
            row
            for row in out_of_stock_rows
            if winning_row_kind[_current_price_key(row)][1] == "out_of_stock"
        ]

    affected_job_ids: dict[Any, None] = {}  # insertion-ordered de-dup set

    with workspace_txn(workspace_id) as session:
        # --- EPA A2 cancellation fence, read side ------------------------
        #
        # A job can be cancelled while its spiders are still in flight.
        # `cancel_and_reconcile_job` commits the fence (job status
        # CANCELLED + a bumped `cancellation_generation`) BEFORE it
        # touches Scrapyd or Redis, precisely so that a run it could not
        # stop still cannot contradict it here.
        #
        # So: one scoped `IN` query for the whole batch, and every item
        # belonging to a cancelled job is refused. Refused, not dropped —
        # its `request_attempts` row is still written, carrying
        # `late_after_cancel`, so the outcome is auditable and the money
        # already spent on the fetch is still accounted for. What does
        # NOT happen is the `price_observations` /
        # `match_current_prices` write: a cancelled job must never
        # produce prices, and a target that was closed by a human must
        # never be re-opened by a straggler.
        fenced_job_ids = cancelled_scrape_job_ids(
            session, {item.scrape_job_id for item in batch if item.scrape_job_id is not None}
        )
        fenced_items: set[int] = set()
        if fenced_job_ids:
            fenced_match_keys: set[tuple[Any, Any]] = set()
            for index, (item, attempt) in enumerate(zip(batch, attempts, strict=True)):
                if item.scrape_job_id not in fenced_job_ids:
                    continue
                fenced_items.add(index)
                fenced_match_keys.add((item.workspace_id, item.match_id))
                # The rejection, recorded on the attempt itself.
                #
                # `success = False` because success on a `request_attempts`
                # row means "this attempt produced the customer-visible
                # outcome it was fetched for", and this one produced no
                # observation and no link — which is also how
                # `admin_usage.py`'s `link_ok`/`protected_ok` aggregate
                # reads it. The billed credit is derived from
                # `price_observations`, not from this flag, so nothing is
                # mis-billed either way. `error_code` stays unset on
                # purpose: this is a persistence refusal, not a scrape
                # failure, and `LATE_AFTER_CANCEL_REASON` says so without
                # polluting the `ScrapeErrorCode` vocabulary that the
                # strategy/health statistics score.
                #
                # `terminal_for_target = False` because that flag is
                # provenance — "this attempt is what closed the target" —
                # and that is not what happened: the cancellation
                # terminalized the target (as CANCELLED, through
                # `mark_target`, before this batch was ever flushed) and
                # this attempt merely arrived afterwards. It used to be
                # forced to True here, which made a late straggler
                # indistinguishable from the attempt that actually decided
                # a target's outcome (EPA Phase A review F-6).
                attempt.success = False
                attempt.error_message = LATE_AFTER_CANCEL_REASON
                attempt.terminal_for_target = False
            observations = [
                observation
                for index, observation in enumerate(observations)
                if index not in fenced_items
            ]
            # The current-price rows carry no job id, so they are filtered
            # by (workspace_id, match_id) instead. In the theoretical case
            # where one batch holds the same match for both a cancelled
            # and a live job, this over-filters by one row — the safe
            # direction: a price that is merely late is recoverable on the
            # next scrape, a price written for a cancelled job is not.
            current_price_rows = [
                row
                for row in current_price_rows
                if _current_price_key(row) not in fenced_match_keys
            ]
            out_of_stock_rows = [
                row
                for row in out_of_stock_rows
                if _current_price_key(row) not in fenced_match_keys
            ]
            log_event(
                logger,
                "persistence.late_after_cancel",
                workspace_id=workspace_id,
                rejected=len(fenced_items),
                jobs=len(fenced_job_ids),
            )

        session.add_all(observations)
        session.add_all(attempts)

        if current_price_rows:
            # A batch may carry more than one successful observation for the
            # same match (e.g. a retried attempt within one run) -- collapse
            # to the last-wins row per (workspace_id, match_id) so the single
            # multi-row INSERT never targets the same conflict arbiter twice
            # (Postgres rejects that within one statement).
            deduped = _dedup_newest_wins(current_price_rows)
            stmt = pg_insert(MatchCurrentPrice).values(list(deduped))
            set_ = {col: stmt.excluded[col] for col in _CURRENT_PRICE_UPDATABLE_COLUMNS}
            set_["updated_at"] = func.now()
            stmt = stmt.on_conflict_do_update(
                index_elements=["workspace_id", "match_id"],
                set_=set_,
                # READY-013-g: a late job never overwrites newer truth.
                where=_monotonic_conflict_where(stmt),
            )
            session.execute(stmt)

        if out_of_stock_rows:
            # Same upsert shape as above, narrowed update set: an existing
            # row keeps its last known price and only learns that the
            # product is now unavailable (2026-08-09, problem 4).
            deduped_oos = _dedup_newest_wins(out_of_stock_rows)
            oos_stmt = pg_insert(MatchCurrentPrice).values(list(deduped_oos))
            oos_set = {
                col: oos_stmt.excluded[col]
                for col in _CURRENT_PRICE_OUT_OF_STOCK_UPDATABLE_COLUMNS
            }
            oos_set["updated_at"] = func.now()
            oos_stmt = oos_stmt.on_conflict_do_update(
                index_elements=["workspace_id", "match_id"],
                set_=oos_set,
                # Same guard: a stale "unavailable" must not overwrite a
                # newer in-stock observation's availability either.
                where=_monotonic_conflict_where(oos_stmt),
            )
            session.execute(oos_stmt)

        # T052 + 2026-08-24 lifecycle correction: persist every attempt, but
        # terminalize only when the chain owner says this is its final
        # outcome.  A retryable/intermediate failure therefore leaves the
        # target STARTED and a later success may still complete it.  The
        # existing mark_target terminal-state guard continues to reject
        # genuinely late results after a real terminal outcome.
        for index, item in enumerate(batch):
            if item.scrape_job_id is None:
                continue
            if index in fenced_items:
                # Its target is already CANCELLED — terminal. `mark_target`
                # would refuse the transition anyway (terminal is terminal),
                # so this is belt-and-braces; what it really buys is keeping
                # the job out of `affected_job_ids` below, so a cancelled job
                # never re-triggers finalization.
                continue
            if item.success:
                target_status = ScrapeTargetStatus.COMPLETED
            elif item.next_strategy_method_id is not None:
                # Durable cross-mode handoff: persist the next candidate and
                # its optional repaired canonical URL in the same transaction
                # as this intermediate attempt, then re-enter dispatch.  The
                # worker derives HTTP/browser mode from that selected method.
                target_row = session.execute(
                    select(ScrapeJobTarget).where(
                        ScrapeJobTarget.workspace_id == item.workspace_id,
                        ScrapeJobTarget.scrape_job_id == item.scrape_job_id,
                        ScrapeJobTarget.match_id == item.match_id,
                    )
                ).scalar_one_or_none()
                if target_row is None:
                    continue
                mark_target(
                    session,
                    workspace_id=item.workspace_id,
                    scrape_job_id=item.scrape_job_id,
                    match_id=item.match_id,
                    status=ScrapeTargetStatus.DEFERRED,
                    error_code=item.error_code,
                )
                target_row.current_strategy_method_id = item.next_strategy_method_id
                target_row.strategy_attempt_ordinal = item.strategy_attempt_ordinal + 1
                target_row.chain_token = item.chain_token or new_uuid7()
                target_row.strategy_url_override = item.canonical_url
                target_row.dispatched_at = None
                write_outbox_message(
                    session,
                    workspace_id=item.workspace_id,
                    task_name=SCRAPE_DISPATCH_JOB,
                    queue="scrape_dispatch",
                    kwargs={
                        "scrape_job_id": str(item.scrape_job_id),
                        "workspace_id": str(item.workspace_id),
                    },
                    dedup_key=(
                        f"strategy-handoff:{item.scrape_job_id}:{item.match_id}:"
                        f"{item.next_strategy_method_id}:{item.strategy_attempt_ordinal + 1}"
                    ),
                )
                continue
            elif item.defer_target and consume_defer_budget(
                get_redis_client(),
                scrape_job_id=item.scrape_job_id,
                match_id=item.match_id,
                max_cycles=get_settings().SCRAPE_MAX_DEFER_CYCLES,
            ):
                # 2026-08-02: the next attempt was rate-ceiling-gated, so
                # this failure is NOT terminal -- the target is handed back
                # to `scrape_dispatch` (the spider enqueues the
                # re-dispatch). Marking DEFERRED here, in the same write
                # that records the attempt, is what keeps the intent from
                # racing a separate `mark_target` call (see
                # `ScrapeResult.defer_target`).
                #
                # 2026-08-03: bounded by `consume_defer_budget` -- once a
                # target has burned its defer cycles it falls through to
                # FAILED below, so a persistently blocked domain can no
                # longer keep its job non-terminal forever.
                target_status = ScrapeTargetStatus.DEFERRED
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
            elif item.error_code in {
                ScrapeErrorCode.NOT_LISTED,
                ScrapeErrorCode.POLICY_BLOCKED,
            }:
                # Permanent catalog/policy outcomes are not scraper
                # operational failures. They finish the target without
                # inflating the job's failure count or driving retries.
                target_status = ScrapeTargetStatus.SKIPPED
            elif not item.chain_complete:
                # Observation + request-attempt rows above are deliberately
                # retained for audit/stats.  There is simply no target
                # transition or premature finalize trigger for this item.
                continue
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

        # EPA B6 (folded-in item 2): live NEEDS_REVIEW sidecar wiring.
        # A fenced (late-after-cancel) item wrote no observation and its
        # attempt was rejected outright above -- it drives no
        # classification either. Deduped via a plain dict-as-ordered-set
        # (a batch can carry more than one attempt for the same match).
        needs_review_match_ids: dict[Any, None] = {}
        for index, item in enumerate(batch):
            if item.needs_review and index not in fenced_items:
                needs_review_match_ids[item.match_id] = None
        if needs_review_match_ids:
            _write_needs_review_classifications(
                session, list(needs_review_match_ids), effective_at=datetime.now(UTC)
            )

        # --- post-commit follow-ups, recorded IN this transaction --------
        #
        # 2026-08-15 audit risk H1. These two follow-ups used to be
        # fire-and-forget `enqueue` calls placed *after* the `with
        # workspace_txn(...)` block, on the reasoning that a broker error
        # must not roll back persisted observations. The cost of that
        # choice was silent loss: this is the highest-volume producer in
        # the system, and a Redis blip (or a spider process dying between
        # COMMIT and send_task) left committed observations whose price
        # analysis never ran and whose job never finalized — the exact
        # "valid observations, no analysis" shape the audit flags.
        #
        # Writing them to the outbox instead keeps the original property
        # (the broker is never on the persistence path — this is a plain
        # INSERT into the same transaction) while removing the loss
        # window entirely.
        #
        # One `SCRAPE_FINALIZE_JOBS` per distinct affected job, deduped by
        # job id: the task is a global no-arg sweep, so N identical
        # messages were always wasteful; the outbox's PENDING dedup index
        # collapses them to one.
        for affected_job_id in affected_job_ids:
            write_outbox_message(
                session,
                workspace_id=workspace_id,
                task_name=SCRAPE_FINALIZE_JOBS,
                queue="maintenance",
                kwargs={},
                dedup_key=f"finalize:{affected_job_id}",
            )

        # SPEC-09 US3 T029 (contracts/recompute-triggers.md trigger (a)):
        # one `PRICE_ANALYSIS_RECOMPUTE` per distinct affected
        # (workspace_id, scrape_job_id, product_variant_id). The Redis
        # `SET NX` claim is kept as a *contention reducer* only — it
        # collapses many completed matches of one variant within one job
        # into a single recompute (SC-007). It is explicitly not a
        # correctness guard (`recompute_variant` is idempotent), and it
        # is no longer the only thing standing between a committed
        # observation and its analysis: even if Redis is down and the
        # claim fails open, the message is durably recorded here.
        dedup_ttl = get_settings().PRICE_ANALYSIS_DEDUP_TTL_SECONDS
        seen_variant_jobs: set[tuple[Any, Any, Any]] = set()
        for index, item in enumerate(batch):
            if index in fenced_items:
                # No observation was persisted for this item, so there is
                # nothing for the analyzer to recompute from.
                continue
            key = (item.workspace_id, item.scrape_job_id, item.product_variant_id)
            if key in seen_variant_jobs:
                continue
            seen_variant_jobs.add(key)

            if item.scrape_job_id is not None:
                redis_key = f"analysis:enqueued:{item.scrape_job_id}:{item.product_variant_id}"
                try:
                    claimed = get_redis_client().set(redis_key, "1", nx=True, ex=dedup_ttl)
                except Exception:  # noqa: BLE001 - contention reducer only, fail open
                    claimed = True
                if not claimed:
                    continue  # another completed match of this variant already claimed it

            write_outbox_message(
                session,
                workspace_id=item.workspace_id,
                task_name=PRICE_ANALYSIS_RECOMPUTE,
                queue="price_analysis",
                kwargs={
                    "workspace_id": str(item.workspace_id),
                    "product_variant_id": str(item.product_variant_id),
                    "product_id": str(item.product_id),
                    "scrape_job_id": (
                        None if item.scrape_job_id is None else str(item.scrape_job_id)
                    ),
                },
                dedup_key=(
                    None
                    if item.scrape_job_id is None
                    else f"analysis:{item.scrape_job_id}:{item.product_variant_id}"
                ),
            )

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

    # SPEC-12 US5 (T037, contracts/stats-buffer.md "Called only from"): buffer
    # each item's attempt outcome for the domain strategy optimizer -- Redis
    # only, still inside this SAME off-reactor flush (no new reactor hop, no
    # blocking Redis on the reactor, FR-025/SC-007). Only items whose group
    # resolved a `domain_strategy_profile_id` (US2 T022) are recorded; an
    # ad-hoc/pre-SPEC-12 item with none is skipped outright. Up to two calls
    # per item -- one for its ACCESS method, one for its EXTRACTION method
    # (when one was even attempted, i.e. the fetch succeeded far enough to
    # extract) -- access and extraction are learned/promoted independently
    # (US1 AS5), so each needs its own `(method_type, method_name)` stat key.
    # `qualifying` reuses the SPEC-06/07 outcome already computed by
    # `validate_candidate`: `item.success` (passed every validation check),
    # `item.comparable` (`False` **only** on a `CURRENCY_MISMATCH` warning --
    # "currency valid when required"), `item.price is not None` ("a valid
    # numeric Decimal"), and `item.extraction_confidence` compared against
    # the promotion confidence bar -- no re-validation performed here.
    settings = get_settings()
    stats_ttl_seconds = settings.STRATEGY_STATS_KEY_TTL_SECONDS
    promotion_confidence_threshold = Decimal(str(settings.STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD))
    for item in batch:
        if item.domain_strategy_profile_id is None:
            continue
        qualifying = (
            item.success
            and item.comparable
            and item.price is not None
            and item.extraction_confidence is not None
            and item.extraction_confidence >= promotion_confidence_threshold
        )
        record_attempt(
            redis,
            workspace_id=item.workspace_id,
            profile_id=item.domain_strategy_profile_id,
            method_type=MethodType.ACCESS,
            method_name=item.access_method.value,
            success=item.success,
            response_time_ms=item.response_time_ms,
            confidence=item.extraction_confidence,
            url=item.url,
            qualifying=qualifying,
            ttl_seconds=stats_ttl_seconds,
            strategy_method_id=item.strategy_method_id,
            operational_failure=is_method_health_failure(item.error_code),
        )
        if item.extraction_method is not None:
            record_attempt(
                redis,
                workspace_id=item.workspace_id,
                profile_id=item.domain_strategy_profile_id,
                method_type=MethodType.EXTRACTION,
                method_name=item.extraction_method.value,
                success=item.success,
                response_time_ms=item.response_time_ms,
                confidence=item.extraction_confidence,
                url=item.url,
                qualifying=qualifying,
                ttl_seconds=stats_ttl_seconds,
                strategy_method_id=item.strategy_method_id,
                operational_failure=is_method_health_failure(item.error_code),
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
