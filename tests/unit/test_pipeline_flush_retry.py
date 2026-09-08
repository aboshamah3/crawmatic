"""Durable flush: bounded retry, quarantine, and admission backpressure (F05, plan task B1).

The two behaviours the plan names, plus the two that make them safe:

- ``test_flush_failure_defers_and_replays_once`` — a flush that fails is
  not lost. Its rows stay in the spool, the failure is recorded against
  them, a ``callLater`` replays the SAME batch, and only the successful
  replay empties the spool.
- ``test_admission_pauses_when_pending_batches_exceed_cap`` — with the
  in-flight cap reached, ``process_item`` returns an unfired ``Deferred``
  instead of accepting more work.
- quarantine after ``SCRAPE_FLUSH_QUARANTINE_AFTER`` failures (the rows
  leave the retry loop but stay on disk, and the counter increments), and
- ``open_spider`` draining what a previous container left behind.

Test mechanism (deliberate, documented deviation from the plan's sketch)
-----------------------------------------------------------------------
The plan sketches these with a real running reactor and a
``run_reactor_until`` helper. This file instead uses the two seams the
rest of the suite already uses for reactor-side code — a synchronous fake
for ``run_in_thread`` (``deferToThread`` Deferreds never fire without a
running reactor thread pool; see ``test_persistence_batching.py``) and a
``twisted.internet.task.Clock`` injected as the pipeline's clock — so the
assertions are identical (``calls == [2, 2]``, ``pending_count() == 0``,
a third item returning an unfired ``Deferred``) while the test stays a
unit test: deterministic, no reactor loop, no wall-clock sleep for a
600-second backoff.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from twisted.internet.defer import Deferred, fail as defer_fail, succeed
from twisted.internet.task import Clock
from twisted.python.failure import Failure

from app_shared.enums import AccessMethod, ExtractionMethod, StockStatus

from scrape_core import pipelines as pipelines_mod
from scrape_core.items import ScrapeResult
from scrape_core.pipelines import BatchedPersistencePipeline

WORKSPACE_ID = uuid.uuid4()


def _make_result() -> ScrapeResult:
    return ScrapeResult(
        workspace_id=WORKSPACE_ID,
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        scrape_job_id=uuid.uuid4(),
        url="https://shop.example.com/p/1",
        access_method=AccessMethod.DIRECT_HTTP,
        success=True,
        price=Decimal("9.99"),
        currency="USD",
        stock_status=StockStatus.IN_STOCK,
        extraction_method=ExtractionMethod.JSON_LD,
        extraction_confidence=Decimal("0.9500"),
    )


class _SyncRunInThread:
    """Run ``fn`` inline and return an already-fired ``Deferred``.

    The production seam is ``deferToThread``, which needs a running
    reactor thread pool; this stands in for it so the pipeline's real
    callback/errback bookkeeping is exercised synchronously. It is the
    inverse of ``test_persistence_batching.py``'s recorder (which never
    calls ``fn``): here the monkeypatched ``_flush_batch`` IS the thing
    under test.
    """

    def __call__(self, fn: Any, *args: Any, **kwargs: Any) -> Deferred:
        try:
            return succeed(fn(*args, **kwargs))
        except Exception:  # noqa: BLE001 - mirrors deferToThread's error path
            return defer_fail(Failure())


@pytest.fixture()
def clock() -> Clock:
    return Clock()


def _pipeline(tmp_path: Any, clock: Clock, **kwargs: Any) -> BatchedPersistencePipeline:
    defaults: dict[str, Any] = dict(
        max_items=2,
        interval_seconds=60.0,
        spool_path=tmp_path / "spool.sqlite",
        max_pending_batches=8,
        retry_backoff_seconds=(1.0, 5.0, 30.0, 120.0, 600.0),
        quarantine_after=5,
        clock=clock,
    )
    defaults.update(kwargs)
    return BatchedPersistencePipeline(**defaults)


# --- the plan's two named tests ----------------------------------------------


def test_flush_failure_defers_and_replays_once(
    tmp_path: Any, monkeypatch: Any, clock: Clock
) -> None:
    calls: list[int] = []

    def flaky_flush(workspace_id: Any, batch: list[ScrapeResult], spool_ids: Any) -> None:
        calls.append(len(batch))
        if len(calls) == 1:
            raise OperationalError("conn reset", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(pipelines_mod, "run_in_thread", _SyncRunInThread())
    monkeypatch.setattr(pipelines_mod, "_flush_batch", flaky_flush)

    pipeline = _pipeline(tmp_path, clock)
    pipeline.process_item(_make_result(), spider=None)
    pipeline.process_item(_make_result(), spider=None)  # size threshold -> flush #1 fails

    # Nothing lost: both rows are still spooled, with the failure recorded.
    assert calls == [2]
    assert pipeline.spool.pending_count() == 2

    # The retry is scheduled, not run inline -- first backoff entry.
    clock.advance(1.0)

    assert calls == [2, 2]  # one failure, one successful replay
    assert pipeline.spool.pending_count() == 0


def test_admission_pauses_when_pending_batches_exceed_cap(
    tmp_path: Any, monkeypatch: Any, clock: Clock
) -> None:
    """The third item waits: two flushes are already in flight."""
    never_fires: list[Deferred] = []

    def _stalled_run_in_thread(fn: Any, *args: Any, **kwargs: Any) -> Deferred:
        # A flush that has been dispatched and has not come back --
        # exactly what `time.sleep(0.5)` stands for in the plan's sketch,
        # without a sleep.
        deferred: Deferred = Deferred()
        never_fires.append(deferred)
        return deferred

    monkeypatch.setattr(pipelines_mod, "run_in_thread", _stalled_run_in_thread)

    pipeline = _pipeline(tmp_path, clock, max_items=1, max_pending_batches=2)
    returned = [pipeline.process_item(_make_result(), spider=None) for _ in range(3)]

    assert len(never_fires) == 3
    assert isinstance(returned[0], ScrapeResult)  # under the cap -- item passes through
    assert isinstance(returned[1], Deferred) and not returned[1].called
    assert isinstance(returned[2], Deferred) and not returned[2].called  # third item waits

    # A completing flush releases the waiters -- admission resumes.
    never_fires[0].callback(None)
    assert returned[1].called
    assert returned[2].called


# --- quarantine ---------------------------------------------------------------


def test_batch_is_quarantined_after_the_configured_number_of_failures(
    tmp_path: Any, monkeypatch: Any, clock: Clock
) -> None:
    def always_fails(workspace_id: Any, batch: list[ScrapeResult], spool_ids: Any) -> None:
        raise OperationalError("still down", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(pipelines_mod, "run_in_thread", _SyncRunInThread())
    monkeypatch.setattr(pipelines_mod, "_flush_batch", always_fails)
    before = pipelines_mod.crawmatic_persistence_quarantined_batches.value

    pipeline = _pipeline(tmp_path, clock, quarantine_after=3, retry_backoff_seconds=(1.0,))
    pipeline.process_item(_make_result(), spider=None)
    pipeline.process_item(_make_result(), spider=None)  # failure #1

    for _ in range(2):  # failures #2 and #3
        clock.advance(1.0)

    # Out of the retry loop, still on disk -- never deleted, because the
    # fetch behind it was already paid for.
    assert pipeline.spool.pending_count() == 0
    assert pipeline.spool.quarantined_count() == 2
    assert pipelines_mod.crawmatic_persistence_quarantined_batches.value == before + 1

    # No further retry is scheduled once the batch is quarantined.
    clock.advance(600.0)
    assert pipeline.spool.quarantined_count() == 2


# --- replay on open_spider ----------------------------------------------------


def test_open_spider_drains_what_a_previous_container_left_behind(
    tmp_path: Any, monkeypatch: Any, clock: Clock
) -> None:
    flushed: list[list[ScrapeResult]] = []

    def record(workspace_id: Any, batch: list[ScrapeResult], spool_ids: Any) -> None:
        flushed.append(list(batch))

    monkeypatch.setattr(pipelines_mod, "run_in_thread", _SyncRunInThread())
    monkeypatch.setattr(pipelines_mod, "_flush_batch", record)
    spool_path = tmp_path / "leftovers.sqlite"

    # A previous run that never got to flush: rows on disk, no pipeline.
    dead = _pipeline(tmp_path, clock, spool_path=spool_path, max_items=50)
    dead.process_item(_make_result(), spider=None)
    dead.process_item(_make_result(), spider=None)
    dead.spool.close()
    assert flushed == []

    reborn = _pipeline(tmp_path, clock, spool_path=spool_path, max_items=50)
    try:
        reborn.open_spider(spider=None)
        assert [len(batch) for batch in flushed] == [2]
        assert reborn.spool.pending_count() == 0
    finally:
        if reborn._looping_call is not None and reborn._looping_call.running:
            reborn._looping_call.stop()


def test_close_spider_gives_up_after_the_grace_period(
    tmp_path: Any, monkeypatch: Any, clock: Clock
) -> None:
    """An unreachable database must not hold the container open forever."""

    def _stalled_run_in_thread(fn: Any, *args: Any, **kwargs: Any) -> Deferred:
        return Deferred()

    monkeypatch.setattr(pipelines_mod, "run_in_thread", _stalled_run_in_thread)

    pipeline = _pipeline(tmp_path, clock, max_items=1)
    pipeline.process_item(_make_result(), spider=None)
    closing = pipeline.close_spider(spider=None)

    assert not closing.called
    clock.advance(pipelines_mod._CLOSE_FLUSH_GRACE_SECONDS)
    assert closing.called
    # The unflushed result is still spooled -- promptness was given up,
    # never the result.
    assert pipeline.spool.pending_count() == 1


# --- idempotent inserts -------------------------------------------------------
#
# The replay above is only SAFE because re-persisting a committed batch
# writes nothing. That property lives in the statement `_flush_batch`
# builds, so it is asserted on the compiled SQL rather than inferred.


def test_observation_and_attempt_inserts_carry_on_conflict_do_nothing() -> None:
    from sqlalchemy.dialects import postgresql

    from app_shared.models.observations import PriceObservation, RequestAttempt

    class _CapturingSession:
        def __init__(self) -> None:
            self.statements: list[Any] = []

        def execute(self, stmt: Any) -> None:
            self.statements.append(stmt)

    result = _make_result()
    result.attempt_id = uuid.uuid4()
    result.scraped_at = datetime(2026, 9, 7, tzinfo=UTC)

    session = _CapturingSession()
    pipelines_mod._insert_ignoring_replays(
        session,
        PriceObservation,
        [
            PriceObservation(
                id=uuid.uuid4(),
                workspace_id=result.workspace_id,
                scraped_at=result.scraped_at,
                attempt_uuid=result.attempt_id,
                match_id=result.match_id,
                product_id=result.product_id,
                product_variant_id=result.product_variant_id,
                success=True,
                comparable=True,
            )
        ],
        ("workspace_id", "attempt_uuid", "scraped_at"),
    )
    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "INSERT INTO price_observations" in sql
    assert "ON CONFLICT (workspace_id, attempt_uuid, scraped_at) DO NOTHING" in sql

    session = _CapturingSession()
    attempt = RequestAttempt(
        workspace_id=result.workspace_id,
        created_at=result.scraped_at,
        attempt_uuid=result.attempt_id,
        match_id=result.match_id,
        url=result.url,
        access_method=result.access_method,
        success=True,
    )
    pipelines_mod._insert_ignoring_replays(
        session, RequestAttempt, [attempt], ("workspace_id", "attempt_uuid", "created_at")
    )
    statement = session.statements[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "INSERT INTO request_attempts" in sql
    assert "ON CONFLICT (workspace_id, attempt_uuid, created_at) DO NOTHING" in sql
    # The Python-side column defaults ORM `add_all` used to apply are
    # resolved by `_row_values`, not silently written as NULL.
    values = pipelines_mod._row_values(attempt)
    assert isinstance(values["id"], uuid.UUID)
    assert values["attempt_number"] == 1
    assert values["terminal_for_target"] is True


def test_the_observation_and_the_attempt_share_one_attempt_identity(
    monkeypatch: Any,
) -> None:
    """One fetch, one identity -- on both rows, or the replay dedups neither."""
    captured: list[tuple[Any, list[Any]]] = []

    class _Session:
        def execute(self, stmt: Any) -> Any:
            return _NoRows()

    class _NoRows:
        def scalars(self) -> "_NoRows":
            return self

        def all(self) -> list[Any]:
            return []

    class _Txn:
        def __call__(self, workspace_id: Any) -> "_Txn":
            return self

        def __enter__(self) -> Any:
            return _Session()

        def __exit__(self, *exc_info: Any) -> bool:
            return False

    monkeypatch.setattr(pipelines_mod, "workspace_txn", _Txn())
    monkeypatch.setattr(pipelines_mod, "mark_target", lambda *a, **k: None)
    monkeypatch.setattr(pipelines_mod, "write_outbox_message", lambda *a, **k: None)
    monkeypatch.setattr(pipelines_mod, "get_redis_client", lambda: _FakeRedis())
    monkeypatch.setattr(pipelines_mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(pipelines_mod, "cancelled_scrape_job_ids", lambda *a, **k: set())
    monkeypatch.setattr(
        pipelines_mod,
        "_insert_ignoring_replays",
        lambda session, model, instances, index_elements: captured.append(
            (model, list(instances))
        ),
    )

    item = _make_result()
    item.attempt_id = uuid.uuid4()
    pipelines_mod._flush_batch(WORKSPACE_ID, [item], [1])

    (_observation_model, observations), (_attempt_model, attempts) = captured
    assert observations[0].attempt_uuid == item.attempt_id
    assert attempts[0].attempt_uuid == item.attempt_id


class _FakeRedis:
    """`SET NX`-honouring stand-in (mirrors `test_persistence_batching.py`)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True


class _FakeSettings:
    """The three fields `_flush_batch` reads unconditionally."""

    PRICE_ANALYSIS_DEDUP_TTL_SECONDS = 21600
    STRATEGY_STATS_KEY_TTL_SECONDS = 3600
    STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD = 0.85
