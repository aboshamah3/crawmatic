"""Pipeline result-path target terminalization (SPEC-08 T053, T052).

`scrape_core.pipelines._flush_batch` terminalizes each item's
`scrape_job_targets` row (via `app_shared.jobs.targets.mark_target`) in
the SAME transaction as the observation/attempt writes, then enqueues
`SCRAPE_FINALIZE_JOBS` (`app_shared.messaging.enqueue`, queue
`maintenance`) once per distinct affected `scrape_job_id` — per
`contracts/lifecycle-counters.md` (FR-017/018/019, SC-007).

Exercised entirely against fakes: a recording `mark_target`/`enqueue`
plus the same `_FakeSession`/`_FakeWorkspaceTxn` pattern
`tests/unit/test_persistence_batching.py` (SPEC-07) already uses for
`_flush_batch` — no real DB, no real Celery/Redis, no running Twisted
reactor.

SPEC-09 US3 T029 also wires a `PRICE_ANALYSIS_RECOMPUTE` enqueue into the
same post-commit continuation (`contracts/recompute-triggers.md` trigger
(a); see `test_recompute_triggers_pipeline.py` for its dedicated
coverage) -- `_install_fakes` here additionally stubs `get_settings`/
`get_redis_client` so every `_flush_batch` call in this file (which now
unconditionally reads `Settings.PRICE_ANALYSIS_DEDUP_TTL_SECONDS`) stays
fully fake, and the few assertions below that inspect `enqueue.calls`
are narrowed to `SCRAPE_FINALIZE_JOBS` so they remain about this file's
own concern (target terminalization), not the new trigger.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app_shared.enums import (
    AccessMethod,
    ExtractionMethod,
    ScrapeErrorCode,
    ScrapeTargetStatus,
    StockStatus,
)
from app_shared.task_names import SCRAPE_FINALIZE_JOBS

from scrape_core import pipelines as pipelines_mod
from scrape_core.items import ScrapeResult
from scrape_core.pipelines import _flush_batch

WORKSPACE_ID = uuid.uuid4()

# Sentinel distinguishing "caller didn't pass scrape_job_id" (-> default to a
# fresh UUID) from "caller explicitly passed scrape_job_id=None" (-> keep it
# None, exercising the "nothing to terminalize" path).
_UNSET = object()


def _make_result(
    *,
    success: bool = True,
    match_id: uuid.UUID | None = None,
    scrape_job_id: uuid.UUID | None | object = _UNSET,
    error_code: ScrapeErrorCode | None = None,
) -> ScrapeResult:
    resolved_job_id = uuid.uuid4() if scrape_job_id is _UNSET else scrape_job_id
    return ScrapeResult(
        workspace_id=WORKSPACE_ID,
        match_id=match_id or uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        scrape_job_id=resolved_job_id,
        url="https://shop.example.com/p/1",
        access_method=AccessMethod.DIRECT_HTTP,
        success=success,
        price=Decimal("9.99") if success else None,
        currency="USD" if success else None,
        stock_status=StockStatus.IN_STOCK if success else None,
        extraction_method=ExtractionMethod.JSON_LD if success else None,
        extraction_confidence=Decimal("0.9500") if success else None,
        error_code=None if success else (error_code or ScrapeErrorCode.PRICE_NOT_FOUND),
        error_message=None if success else "no price candidate found",
    )


class _FakeSession:
    """Records `add_all`/`execute` calls; no real DB anywhere."""

    def __init__(self) -> None:
        self.added: list[list[Any]] = []
        self.executed: list[Any] = []

    def add_all(self, items: Any) -> None:
        self.added.append(list(items))

    def execute(self, stmt: Any) -> None:
        self.executed.append(stmt)


class _FakeWorkspaceTxn:
    """Fake `workspace_txn` context manager -- yields a `_FakeSession`."""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self.workspace_id: Any = None
        self.entered = 0

    def __call__(self, workspace_id: Any) -> "_FakeWorkspaceTxn":
        self.workspace_id = workspace_id
        return self

    def __enter__(self) -> _FakeSession:
        self.entered += 1
        return self._session

    def __exit__(self, *exc_info: Any) -> bool:
        return False


class _RecordingMarkTarget:
    """Stand-in for `app_shared.jobs.targets.mark_target`."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        session: Any,
        *,
        workspace_id: Any,
        scrape_job_id: Any,
        match_id: Any,
        status: ScrapeTargetStatus,
        error_code: ScrapeErrorCode | None = None,
    ) -> None:
        self.calls.append(
            {
                "session": session,
                "workspace_id": workspace_id,
                "scrape_job_id": scrape_job_id,
                "match_id": match_id,
                "status": status,
                "error_code": error_code,
            }
        )


class _RecordingEnqueue:
    """Stand-in for `app_shared.messaging.enqueue`."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, name: str, *, queue: str, kwargs: dict[str, Any] | None = None) -> None:
        self.calls.append({"name": name, "queue": queue, "kwargs": kwargs})


class _FakeSettings:
    """Stand-in for `app_shared.config.Settings` -- the SPEC-09 T029 field
    plus the two SPEC-12 US5 T037 fields `_flush_batch` now also reads
    unconditionally (the stats-buffer recording gate), no real
    env/pydantic validation. None of this file's seeded `ScrapeResult`s
    carry a `domain_strategy_profile_id`, so `record_attempt` itself is
    never actually invoked here -- only the attribute reads need
    satisfying."""

    PRICE_ANALYSIS_DEDUP_TTL_SECONDS = 21600
    STRATEGY_STATS_KEY_TTL_SECONDS = 3600
    STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD = 0.85


class _FakeRedis:
    """Minimal `redis.Redis`-shaped fake honoring `SET NX` (mirrors the
    `FakeRedis` pattern in `test_jobs_dispatch_task.py`/`test_jobs_stall_recovery.py`)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True


def _install_fakes(
    monkeypatch: Any,
) -> tuple[_FakeSession, _FakeWorkspaceTxn, _RecordingMarkTarget, _RecordingEnqueue, _FakeRedis]:
    session = _FakeSession()
    txn = _FakeWorkspaceTxn(session)
    mark_target = _RecordingMarkTarget()
    enqueue = _RecordingEnqueue()
    fake_redis = _FakeRedis()
    monkeypatch.setattr(pipelines_mod, "workspace_txn", txn)
    monkeypatch.setattr(pipelines_mod, "mark_target", mark_target)
    monkeypatch.setattr(pipelines_mod, "enqueue", enqueue)
    monkeypatch.setattr(pipelines_mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(pipelines_mod, "get_redis_client", lambda: fake_redis)
    return session, txn, mark_target, enqueue, fake_redis


# --- success -> COMPLETED; failure -> FAILED w/ error_code -------------------


def test_successful_item_marks_its_target_completed(monkeypatch: Any) -> None:
    session, _txn, mark_target, _enqueue, _redis = _install_fakes(monkeypatch)
    job_id = uuid.uuid4()
    match_id = uuid.uuid4()
    item = _make_result(success=True, match_id=match_id, scrape_job_id=job_id)

    _flush_batch(WORKSPACE_ID, [item])

    assert len(mark_target.calls) == 1
    call = mark_target.calls[0]
    assert call["workspace_id"] == WORKSPACE_ID
    assert call["scrape_job_id"] == job_id
    assert call["match_id"] == match_id
    assert call["status"] == ScrapeTargetStatus.COMPLETED
    assert call["error_code"] is None
    # Shares the SAME session `workspace_txn` yielded for the observation/
    # attempt writes -- no second run_in_thread/session.
    assert call["session"] is session


def test_failed_item_marks_its_target_failed_with_error_code(monkeypatch: Any) -> None:
    _session, _txn, mark_target, _enqueue, _redis = _install_fakes(monkeypatch)
    job_id = uuid.uuid4()
    match_id = uuid.uuid4()
    item = _make_result(
        success=False,
        match_id=match_id,
        scrape_job_id=job_id,
        error_code=ScrapeErrorCode.HTTP_403,
    )

    _flush_batch(WORKSPACE_ID, [item])

    assert len(mark_target.calls) == 1
    call = mark_target.calls[0]
    assert call["scrape_job_id"] == job_id
    assert call["match_id"] == match_id
    assert call["status"] == ScrapeTargetStatus.FAILED
    assert call["error_code"] == ScrapeErrorCode.HTTP_403


def test_item_with_null_scrape_job_id_marks_nothing(monkeypatch: Any) -> None:
    _session, _txn, mark_target, enqueue, _redis = _install_fakes(monkeypatch)
    item = _make_result(success=True, scrape_job_id=None)

    _flush_batch(WORKSPACE_ID, [item])

    assert mark_target.calls == []
    # No scrape_job_id -> no SCRAPE_FINALIZE_JOBS (this file's own concern).
    # SPEC-09 T029 still enqueues PRICE_ANALYSIS_RECOMPUTE directly for the
    # ad-hoc item (no job -> no dedup key) -- see
    # test_recompute_triggers_pipeline.py for that trigger's own coverage.
    assert [c for c in enqueue.calls if c["name"] == SCRAPE_FINALIZE_JOBS] == []


def test_mixed_batch_marks_only_items_with_a_scrape_job_id(monkeypatch: Any) -> None:
    _session, _txn, mark_target, _enqueue, _redis = _install_fakes(monkeypatch)
    job_id = uuid.uuid4()
    with_job = _make_result(success=True, scrape_job_id=job_id)
    without_job = _make_result(success=True, scrape_job_id=None)

    _flush_batch(WORKSPACE_ID, [with_job, without_job])

    assert len(mark_target.calls) == 1
    assert mark_target.calls[0]["scrape_job_id"] == job_id


# --- shares the batch transaction -- no second run_in_thread -----------------


def test_target_marking_happens_inside_the_single_workspace_txn(monkeypatch: Any) -> None:
    _session, txn, mark_target, _enqueue, _redis = _install_fakes(monkeypatch)
    item = _make_result(success=True)

    _flush_batch(WORKSPACE_ID, [item])

    # workspace_txn entered exactly once for the whole flush (observations +
    # attempts + upsert + target terminalization all share it).
    assert txn.entered == 1
    assert len(mark_target.calls) == 1


# --- exactly one SCRAPE_FINALIZE_JOBS enqueue per distinct scrape_job_id -----


def test_one_finalize_enqueue_per_distinct_scrape_job_id(monkeypatch: Any) -> None:
    _session, _txn, _mark_target, enqueue, _redis = _install_fakes(monkeypatch)
    job_a = uuid.uuid4()
    job_b = uuid.uuid4()
    batch = [
        _make_result(success=True, scrape_job_id=job_a),
        _make_result(success=False, scrape_job_id=job_a),  # same job -- still 1 enqueue
        _make_result(success=True, scrape_job_id=job_b),
    ]

    _flush_batch(WORKSPACE_ID, batch)

    finalize_calls = [c for c in enqueue.calls if c["name"] == SCRAPE_FINALIZE_JOBS]
    assert len(finalize_calls) == 2
    for call in finalize_calls:
        assert call["queue"] == "maintenance"


def test_no_finalize_enqueue_when_no_item_carries_a_scrape_job_id(monkeypatch: Any) -> None:
    _session, _txn, _mark_target, enqueue, _redis = _install_fakes(monkeypatch)
    batch = [_make_result(success=True, scrape_job_id=None) for _ in range(3)]

    _flush_batch(WORKSPACE_ID, batch)

    # No scrape_job_id anywhere in the batch -> no SCRAPE_FINALIZE_JOBS
    # (this file's own concern; SPEC-09 T029's ad-hoc PRICE_ANALYSIS_RECOMPUTE
    # enqueues are covered separately by test_recompute_triggers_pipeline.py).
    assert [c for c in enqueue.calls if c["name"] == SCRAPE_FINALIZE_JOBS] == []
