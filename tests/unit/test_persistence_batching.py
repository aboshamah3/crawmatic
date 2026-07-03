"""Batched persistence pipeline tests (SPEC-07 US5 T042, contracts/persistence-pipeline.md).

``BatchedPersistencePipeline`` flushes on **either** threshold (buffer
size or elapsed time) plus a final flush at ``close_spider``, and every
flush routes through the single reactor-safe DB seam
(``scrape_core.db.run_in_thread``). ``deferToThread`` Deferreds don't
fire without a running Twisted reactor thread pool, so — mirroring the
pattern the US2 (robots middleware) tests use — these tests:

- monkeypatch the module-level ``run_in_thread``/``workspace_txn``
  names bound inside ``scrape_core.pipelines`` with pure, synchronous
  recording fakes (no real threading, no real DB, no running reactor
  loop required), and
- exercise the pipeline's pure buffering/triggering core
  (``process_item``, ``_time_based_flush``, ``close_spider``) plus the
  pure batch-building core (``_flush_batch``) directly.

Covers US5/SC-006: flush at N items, at T seconds, and a final flush at
close; N items -> far fewer than N flush transactions; the buffer is
emptied immediately on every flush (including a failing one); the DB
call is always dispatched through the mocked ``deferToThread`` seam,
never inline on the calling thread; and the FR-014 regression that a
FAILED item never triggers a ``match_current_prices`` upsert.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from twisted.internet.defer import Deferred, fail as defer_fail, succeed
from twisted.python.failure import Failure

from app_shared.enums import AccessMethod, ExtractionMethod, ScrapeErrorCode, StockStatus

from scrape_core import pipelines as pipelines_mod
from scrape_core.items import ScrapeResult
from scrape_core.pipelines import BatchedPersistencePipeline, _flush_batch

WORKSPACE_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _stub_target_terminalization(monkeypatch: Any) -> None:
    """Stub out the SPEC-08 T052 `mark_target`/`enqueue` seam here.

    This file's job is `_flush_batch`'s SPEC-07 persistence behavior
    (observations/attempts/current-price upsert) — the seeded
    `ScrapeResult`s in this file carry a real `scrape_job_id` but no real
    `scrape_job_targets` row exists behind the `_FakeSession`, so
    `mark_target`'s real `session.execute(...).scalar_one_or_none()`
    would blow up against the recording fake. The target-terminalization
    behavior itself is exercised in
    `tests/unit/test_pipeline_target_terminalization.py` (T053).
    """
    monkeypatch.setattr(pipelines_mod, "mark_target", lambda *a, **k: None)
    monkeypatch.setattr(pipelines_mod, "enqueue", lambda *a, **k: None)


def _make_result(
    *,
    success: bool = True,
    match_id: uuid.UUID | None = None,
    price: Decimal = Decimal("9.99"),
) -> ScrapeResult:
    return ScrapeResult(
        workspace_id=WORKSPACE_ID,
        match_id=match_id or uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        scrape_job_id=uuid.uuid4(),
        url="https://shop.example.com/p/1",
        access_method=AccessMethod.DIRECT_HTTP,
        success=success,
        price=price if success else None,
        currency="USD" if success else None,
        stock_status=StockStatus.IN_STOCK if success else None,
        extraction_method=ExtractionMethod.JSON_LD if success else None,
        extraction_confidence=Decimal("0.9500") if success else None,
        error_code=None if success else ScrapeErrorCode.PRICE_NOT_FOUND,
        error_message=None if success else "no price candidate found",
    )


class _RecordingRunInThread:
    """Fake ``run_in_thread`` — records the dispatched call, never runs ``fn``.

    Standing in for the real ``deferToThread`` seam: production code
    must never call the flush function directly on the calling
    (reactor) thread, so this fake asserts *what* would have been
    offloaded without actually executing it inline, and returns an
    already-fired ``Deferred`` (``succeed``/``fail`` fire synchronously
    with no reactor required) so pipeline bookkeeping (``_pending``)
    still exercises its real completion path.
    """

    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
        self._raises = raises

    def __call__(self, fn: Any, *args: Any, **kwargs: Any) -> Deferred:
        self.calls.append((fn, args, kwargs))
        if self._raises:
            try:
                raise RuntimeError("db exploded")
            except RuntimeError:
                return defer_fail(Failure())
        return succeed(None)


class _FakeSession:
    """Records ``add_all``/``execute`` calls; no real DB anywhere."""

    def __init__(self) -> None:
        self.added: list[list[Any]] = []
        self.executed: list[Any] = []

    def add_all(self, items: Any) -> None:
        self.added.append(list(items))

    def execute(self, stmt: Any) -> None:
        self.executed.append(stmt)


class _FakeWorkspaceTxn:
    """Fake ``workspace_txn`` context manager -- yields a ``_FakeSession``."""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self.workspace_id: Any = None

    def __call__(self, workspace_id: Any) -> "_FakeWorkspaceTxn":
        self.workspace_id = workspace_id
        return self

    def __enter__(self) -> _FakeSession:
        return self._session

    def __exit__(self, *exc_info: Any) -> bool:
        return False


# --- size-based flush --------------------------------------------------------


def test_flush_triggers_at_max_items(monkeypatch: Any) -> None:
    recorder = _RecordingRunInThread()
    monkeypatch.setattr(pipelines_mod, "run_in_thread", recorder)
    pipeline = BatchedPersistencePipeline(max_items=3, interval_seconds=999.0)

    pipeline.process_item(_make_result(), spider=None)
    pipeline.process_item(_make_result(), spider=None)
    assert recorder.calls == []
    assert len(pipeline._buffer) == 2

    pipeline.process_item(_make_result(), spider=None)  # 3rd item trips the size threshold

    assert len(recorder.calls) == 1
    fn, args, _kwargs = recorder.calls[0]
    assert fn is pipelines_mod._flush_batch
    assert args[0] == WORKSPACE_ID
    assert len(args[1]) == 3
    # Buffer emptied immediately -- the whole concurrency story (see
    # `_flush`'s docstring): a fresh list is swapped in before dispatch.
    assert pipeline._buffer == []


def test_non_scrape_result_items_pass_through_untouched(monkeypatch: Any) -> None:
    recorder = _RecordingRunInThread()
    monkeypatch.setattr(pipelines_mod, "run_in_thread", recorder)
    pipeline = BatchedPersistencePipeline(max_items=1, interval_seconds=999.0)

    passthrough = {"not": "a ScrapeResult"}
    result = pipeline.process_item(passthrough, spider=None)

    assert result is passthrough
    assert pipeline._buffer == []
    assert recorder.calls == []


# --- time-based flush ---------------------------------------------------------


def test_time_based_flush_flushes_partial_buffer(monkeypatch: Any) -> None:
    recorder = _RecordingRunInThread()
    monkeypatch.setattr(pipelines_mod, "run_in_thread", recorder)
    pipeline = BatchedPersistencePipeline(max_items=50, interval_seconds=2.0)

    pipeline.process_item(_make_result(), spider=None)
    pipeline.process_item(_make_result(), spider=None)
    assert recorder.calls == []  # below the size threshold -- no flush yet

    # Simulate the LoopingCall's tick directly rather than waiting on a
    # real reactor clock (no reactor loop is running in this test).
    pipeline._time_based_flush()

    assert len(recorder.calls) == 1
    assert len(recorder.calls[0][1][1]) == 2  # both buffered items in the batch
    assert pipeline._buffer == []


def test_time_based_flush_is_a_noop_on_an_empty_buffer(monkeypatch: Any) -> None:
    recorder = _RecordingRunInThread()
    monkeypatch.setattr(pipelines_mod, "run_in_thread", recorder)
    pipeline = BatchedPersistencePipeline(max_items=50, interval_seconds=2.0)

    pipeline._time_based_flush()

    assert recorder.calls == []


def test_open_spider_starts_a_looping_call_at_the_configured_interval() -> None:
    pipeline = BatchedPersistencePipeline(max_items=50, interval_seconds=2.0)
    pipeline.open_spider(spider=None)
    try:
        assert pipeline._looping_call is not None
        assert pipeline._looping_call.running
        assert pipeline._looping_call.interval == 2.0
    finally:
        pipeline._looping_call.stop()


# --- final flush at close_spider ----------------------------------------------


def test_close_spider_flushes_remaining_partial_buffer(monkeypatch: Any) -> None:
    recorder = _RecordingRunInThread()
    monkeypatch.setattr(pipelines_mod, "run_in_thread", recorder)
    pipeline = BatchedPersistencePipeline(max_items=50, interval_seconds=999.0)
    pipeline.open_spider(spider=None)

    pipeline.process_item(_make_result(), spider=None)
    pipeline.process_item(_make_result(), spider=None)
    assert recorder.calls == []

    result = pipeline.close_spider(spider=None)

    assert len(recorder.calls) == 1
    assert pipeline._buffer == []
    assert pipeline._looping_call is not None
    assert not pipeline._looping_call.running
    # `close_spider` waits for every in-flight (and this final) flush --
    # a `DeferredList` over already-fired Deferreds fires synchronously.
    assert isinstance(result, Deferred)
    fired: list[Any] = []
    result.addCallback(fired.append)
    assert fired


def test_close_spider_with_an_empty_buffer_dispatches_no_flush(monkeypatch: Any) -> None:
    recorder = _RecordingRunInThread()
    monkeypatch.setattr(pipelines_mod, "run_in_thread", recorder)
    pipeline = BatchedPersistencePipeline(max_items=50, interval_seconds=999.0)
    pipeline.open_spider(spider=None)

    pipeline.close_spider(spider=None)

    assert recorder.calls == []


# --- N items -> far fewer flushes than N (SC-006) -----------------------------


def test_n_items_flush_far_fewer_than_n_times(monkeypatch: Any) -> None:
    recorder = _RecordingRunInThread()
    monkeypatch.setattr(pipelines_mod, "run_in_thread", recorder)
    pipeline = BatchedPersistencePipeline(max_items=5, interval_seconds=999.0)

    n = 23
    for _ in range(n):
        pipeline.process_item(_make_result(), spider=None)

    # 23 items at a threshold of 5 -> 4 full-size flushes; 3 items remain
    # buffered for the eventual final flush.
    assert len(recorder.calls) == n // 5
    assert len(recorder.calls) < n  # commit count << N (SC-006)
    assert len(pipeline._buffer) == n % 5

    pipeline.close_spider(spider=None)

    assert len(recorder.calls) == n // 5 + 1  # + the final partial flush
    assert pipeline._buffer == []


# --- reactor-safety: DB always routed through the mocked deferToThread seam ---


def test_every_flush_route_dispatches_through_run_in_thread_never_inline(
    monkeypatch: Any,
) -> None:
    """All three flush triggers (size/time/close) call the *same*
    `run_in_thread` seam -- never a direct/synchronous DB call."""
    recorder = _RecordingRunInThread()
    monkeypatch.setattr(pipelines_mod, "run_in_thread", recorder)
    pipeline = BatchedPersistencePipeline(max_items=2, interval_seconds=999.0)
    pipeline.open_spider(spider=None)

    pipeline.process_item(_make_result(), spider=None)
    pipeline.process_item(_make_result(), spider=None)  # size-based flush #1

    pipeline.process_item(_make_result(), spider=None)
    pipeline._time_based_flush()  # nothing to flush yet (1 item < max_items)
    pipeline.process_item(_make_result(), spider=None)  # size-based flush #2

    pipeline.process_item(_make_result(), spider=None)
    pipeline.close_spider(spider=None)  # final flush #3

    assert len(recorder.calls) == 3
    assert all(fn is pipelines_mod._flush_batch for fn, _args, _kwargs in recorder.calls)


def test_buffer_emptied_immediately_even_when_the_flush_later_fails(
    monkeypatch: Any,
) -> None:
    """Failure semantics (documented in `_flush`/`_on_flush_failure`):
    the buffer is swapped out for a fresh list *before* dispatch, so it
    is emptied regardless of whether the offloaded flush eventually
    succeeds or fails -- a failed flush loses only its own batch (no
    retry queue in this MVP slice) and never wedges subsequent
    flushes or crashes the reactor/spider run."""
    recorder = _RecordingRunInThread(raises=True)
    monkeypatch.setattr(pipelines_mod, "run_in_thread", recorder)
    pipeline = BatchedPersistencePipeline(max_items=2, interval_seconds=999.0)

    pipeline.process_item(_make_result(), spider=None)
    pipeline.process_item(_make_result(), spider=None)  # flush dispatched, then fails

    assert len(recorder.calls) == 1
    assert pipeline._buffer == []
    # The errback + `_forget_pending` ran synchronously (the fake fires
    # immediately) -- the failed Deferred is no longer tracked as pending.
    assert pipeline._pending == []

    # The pipeline keeps working after a failed flush -- not wedged.
    pipeline.process_item(_make_result(), spider=None)
    pipeline.process_item(_make_result(), spider=None)
    assert len(recorder.calls) == 2


# --- `_flush_batch` pure core: bulk insert + upsert-only-for-success ----------


def test_flush_batch_bulk_inserts_observations_and_attempts(monkeypatch: Any) -> None:
    session = _FakeSession()
    monkeypatch.setattr(pipelines_mod, "workspace_txn", _FakeWorkspaceTxn(session))

    batch = [_make_result(success=True)]
    _flush_batch(WORKSPACE_ID, batch)

    assert len(session.added) == 2  # one add_all for observations, one for attempts
    assert len(session.added[0]) == 1
    assert len(session.added[1]) == 1
    assert len(session.executed) == 1  # the match_current_prices upsert


def test_flush_batch_never_upserts_current_price_for_a_failed_item(
    monkeypatch: Any,
) -> None:
    """Regression (FR-014): a FAILED `ScrapeResult` must never touch
    `match_current_prices` -- it is structurally excluded from the
    upsert's input rows, so `ON CONFLICT` can never fire for it and
    overwrite a good current price with a bad/missing one."""
    session = _FakeSession()
    monkeypatch.setattr(pipelines_mod, "workspace_txn", _FakeWorkspaceTxn(session))

    batch = [_make_result(success=False)]
    _flush_batch(WORKSPACE_ID, batch)

    assert len(session.added) == 2  # the observation + attempt are still recorded
    assert session.executed == []  # but NO upsert statement executes at all


def test_flush_batch_mixed_batch_only_upserts_the_successful_rows(
    monkeypatch: Any,
) -> None:
    session = _FakeSession()
    monkeypatch.setattr(pipelines_mod, "workspace_txn", _FakeWorkspaceTxn(session))

    captured: dict[str, Any] = {}
    original_dedup = pipelines_mod.dedup_last_wins

    def _spy_dedup(rows: Any, *, key_fn: Any) -> Any:
        captured["rows"] = list(rows)
        return original_dedup(rows, key_fn=key_fn)

    monkeypatch.setattr(pipelines_mod, "dedup_last_wins", _spy_dedup)

    success_match_id = uuid.uuid4()
    failed_match_id = uuid.uuid4()
    batch = [
        _make_result(success=True, match_id=success_match_id),
        _make_result(success=False, match_id=failed_match_id),
    ]
    _flush_batch(WORKSPACE_ID, batch)

    assert len(session.added[0]) == 2  # both observations recorded regardless
    assert len(session.executed) == 1  # exactly one upsert statement for the batch
    # Only the successful item's row ever reached the upsert's input.
    assert len(captured["rows"]) == 1
    assert captured["rows"][0]["match_id"] == success_match_id


def test_flush_batch_all_failed_items_records_attempts_but_no_upsert(
    monkeypatch: Any,
) -> None:
    session = _FakeSession()
    monkeypatch.setattr(pipelines_mod, "workspace_txn", _FakeWorkspaceTxn(session))

    batch = [_make_result(success=False) for _ in range(3)]
    _flush_batch(WORKSPACE_ID, batch)

    assert len(session.added[0]) == 3
    assert len(session.added[1]) == 3
    assert session.executed == []
