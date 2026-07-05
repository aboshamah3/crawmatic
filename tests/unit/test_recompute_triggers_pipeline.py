"""Recompute trigger (a): scrape completion (SPEC-09 US3 T029/T032,
contracts/recompute-triggers.md trigger (a), FR-012/FR-015, SC-007).

`scrape_core.pipelines._flush_batch` enqueues one `PRICE_ANALYSIS_RECOMPUTE`
per distinct affected `(workspace_id, scrape_job_id, product_variant_id)` in
the batch, **after** the persistence transaction commits (the same
off-reactor continuation that already enqueues `SCRAPE_FINALIZE_JOBS`,
SPEC-08 T053). Items that carry a non-null `scrape_job_id` first claim a
Redis `SET NX` dedup key (`analysis:enqueued:{scrape_job_id}:{product_variant_id}`)
so many completed matches of one variant within one job collapse to a
single recompute; ad-hoc items with no `scrape_job_id` enqueue directly,
no dedup key.

Exercised entirely against fakes: the same `_FakeSession`/
`_FakeWorkspaceTxn`/recording-`enqueue` pattern
`test_pipeline_target_terminalization.py` (SPEC-08) uses for
`_flush_batch`, plus a `FakeRedis` honoring `SET NX` (mirrors
`test_jobs_dispatch_task.py`) — no real DB, no real Celery/Redis, no
running Twisted reactor.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app_shared.enums import AccessMethod, ExtractionMethod, ScrapeErrorCode, StockStatus
from app_shared.task_names import PRICE_ANALYSIS_RECOMPUTE

from scrape_core import pipelines as pipelines_mod
from scrape_core.items import ScrapeResult
from scrape_core.pipelines import _flush_batch

WORKSPACE_ID = uuid.uuid4()


def _make_result(
    *,
    success: bool = True,
    match_id: uuid.UUID | None = None,
    product_variant_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    scrape_job_id: uuid.UUID | None = None,
) -> ScrapeResult:
    return ScrapeResult(
        workspace_id=WORKSPACE_ID,
        match_id=match_id or uuid.uuid4(),
        product_id=product_id or uuid.uuid4(),
        product_variant_id=product_variant_id or uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        scrape_job_id=scrape_job_id,
        url="https://shop.example.com/p/1",
        access_method=AccessMethod.DIRECT_HTTP,
        success=success,
        price=Decimal("9.99") if success else None,
        currency="USD" if success else None,
        stock_status=StockStatus.IN_STOCK if success else None,
        extraction_method=ExtractionMethod.JSON_LD if success else None,
        extraction_confidence=Decimal("0.9500") if success else None,
        error_code=None if success else ScrapeErrorCode.PRICE_NOT_FOUND,
        error_message=None if success else "no price candidate found",
    )


class _FakeSession:
    def add_all(self, items: Any) -> None:
        pass

    def execute(self, stmt: Any) -> None:
        pass


class _FakeWorkspaceTxn:
    """Fake `workspace_txn` -- also records whether `enqueue` was ever
    called before this context manager exited (commit boundary)."""

    def __init__(self, session: _FakeSession, order: list[str]) -> None:
        self._session = session
        self._order = order

    def __call__(self, workspace_id: Any) -> "_FakeWorkspaceTxn":
        return self

    def __enter__(self) -> _FakeSession:
        return self._session

    def __exit__(self, *exc_info: Any) -> bool:
        self._order.append("txn_exit")
        return False


class _RecordingEnqueue:
    def __init__(self, order: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._order = order

    def __call__(self, name: str, *, queue: str, kwargs: dict[str, Any] | None = None) -> None:
        self.calls.append({"name": name, "queue": queue, "kwargs": kwargs})
        if self._order is not None:
            self._order.append("enqueue")


class _FakeSettings:
    """SPEC-09 T029 field plus the two SPEC-12 US5 T037 fields
    `_flush_batch` now also reads unconditionally. None of this file's
    seeded `ScrapeResult`s carry a `domain_strategy_profile_id`, so
    `record_attempt` itself is never actually invoked here -- only the
    attribute reads need satisfying."""

    PRICE_ANALYSIS_DEDUP_TTL_SECONDS = 21600
    STRATEGY_STATS_KEY_TTL_SECONDS = 3600
    STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD = 0.85


class _FakeRedis:
    """Minimal `redis.Redis`-shaped fake honoring `SET NX`."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True


def _install_fakes(
    monkeypatch: Any,
) -> tuple[_FakeWorkspaceTxn, _RecordingEnqueue, _FakeRedis, list[str]]:
    order: list[str] = []
    session = _FakeSession()
    txn = _FakeWorkspaceTxn(session, order)
    enqueue = _RecordingEnqueue(order)
    fake_redis = _FakeRedis()
    monkeypatch.setattr(pipelines_mod, "workspace_txn", txn)
    monkeypatch.setattr(pipelines_mod, "mark_target", lambda *a, **k: None)
    monkeypatch.setattr(pipelines_mod, "enqueue", enqueue)
    monkeypatch.setattr(pipelines_mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(pipelines_mod, "get_redis_client", lambda: fake_redis)
    return txn, enqueue, fake_redis, order


def _recompute_calls(enqueue: _RecordingEnqueue) -> list[dict[str, Any]]:
    return [c for c in enqueue.calls if c["name"] == PRICE_ANALYSIS_RECOMPUTE]


# --- SC-007: N completions of one variant in one job -> exactly one enqueue --


def test_n_matches_of_one_variant_in_one_job_enqueue_exactly_once(monkeypatch: Any) -> None:
    _txn, enqueue, fake_redis, _order = _install_fakes(monkeypatch)
    job_id = uuid.uuid4()
    variant_id = uuid.uuid4()
    product_id = uuid.uuid4()
    batch = [
        _make_result(
            match_id=uuid.uuid4(),
            product_variant_id=variant_id,
            product_id=product_id,
            scrape_job_id=job_id,
        )
        for _ in range(5)
    ]

    _flush_batch(WORKSPACE_ID, batch)

    calls = _recompute_calls(enqueue)
    assert len(calls) == 1
    call = calls[0]
    assert call["queue"] == "price_analysis"
    assert call["kwargs"] == {
        "workspace_id": str(WORKSPACE_ID),
        "product_variant_id": str(variant_id),
        "product_id": str(product_id),
        "scrape_job_id": str(job_id),
    }
    # The dedup key was claimed exactly once in the fake redis store.
    assert fake_redis.store == {f"analysis:enqueued:{job_id}:{variant_id}": "1"}


def test_distinct_variants_each_enqueue_once(monkeypatch: Any) -> None:
    _txn, enqueue, _redis, _order = _install_fakes(monkeypatch)
    job_id = uuid.uuid4()
    variant_a = uuid.uuid4()
    variant_b = uuid.uuid4()
    batch = [
        _make_result(product_variant_id=variant_a, scrape_job_id=job_id),
        _make_result(product_variant_id=variant_a, scrape_job_id=job_id),
        _make_result(product_variant_id=variant_b, scrape_job_id=job_id),
    ]

    _flush_batch(WORKSPACE_ID, batch)

    calls = _recompute_calls(enqueue)
    assert len(calls) == 2
    enqueued_variants = {c["kwargs"]["product_variant_id"] for c in calls}
    assert enqueued_variants == {str(variant_a), str(variant_b)}


def test_a_fresh_job_for_the_same_variant_enqueues_again(monkeypatch: Any) -> None:
    """A different `scrape_job_id` is a different dedup key -- no
    cross-job suppression (only within one job is collapsed)."""
    _txn, enqueue, _redis, _order = _install_fakes(monkeypatch)
    variant_id = uuid.uuid4()
    job_a = uuid.uuid4()
    job_b = uuid.uuid4()

    _flush_batch(WORKSPACE_ID, [_make_result(product_variant_id=variant_id, scrape_job_id=job_a)])
    _flush_batch(WORKSPACE_ID, [_make_result(product_variant_id=variant_id, scrape_job_id=job_b)])

    calls = _recompute_calls(enqueue)
    assert len(calls) == 2
    assert {c["kwargs"]["scrape_job_id"] for c in calls} == {str(job_a), str(job_b)}


def test_redis_loss_skips_the_enqueue(monkeypatch: Any) -> None:
    """A losing `SET NX` (another flush already claimed the key) must
    skip the enqueue entirely -- exercised by pre-seeding the fake
    redis store with the exact key `_flush_batch` will claim."""
    _txn, enqueue, fake_redis, _order = _install_fakes(monkeypatch)
    job_id = uuid.uuid4()
    variant_id = uuid.uuid4()
    fake_redis.store[f"analysis:enqueued:{job_id}:{variant_id}"] = "1"

    _flush_batch(
        WORKSPACE_ID,
        [_make_result(product_variant_id=variant_id, scrape_job_id=job_id)],
    )

    assert _recompute_calls(enqueue) == []


# --- ad-hoc items (no scrape_job_id): enqueue directly, no dedup key --------


def test_item_with_no_scrape_job_id_enqueues_directly_no_dedup_key(monkeypatch: Any) -> None:
    _txn, enqueue, fake_redis, _order = _install_fakes(monkeypatch)
    variant_id = uuid.uuid4()
    product_id = uuid.uuid4()

    _flush_batch(
        WORKSPACE_ID,
        [_make_result(product_variant_id=variant_id, product_id=product_id, scrape_job_id=None)],
    )

    calls = _recompute_calls(enqueue)
    assert len(calls) == 1
    assert calls[0]["kwargs"] == {
        "workspace_id": str(WORKSPACE_ID),
        "product_variant_id": str(variant_id),
        "product_id": str(product_id),
        "scrape_job_id": None,
    }
    # No redis interaction at all for an ad-hoc item.
    assert fake_redis.store == {}


def test_mixed_batch_job_and_ad_hoc_each_enqueue(monkeypatch: Any) -> None:
    _txn, enqueue, _redis, _order = _install_fakes(monkeypatch)
    job_id = uuid.uuid4()
    with_job = _make_result(scrape_job_id=job_id)
    ad_hoc = _make_result(scrape_job_id=None)

    _flush_batch(WORKSPACE_ID, [with_job, ad_hoc])

    calls = _recompute_calls(enqueue)
    assert len(calls) == 2
    assert {c["kwargs"]["scrape_job_id"] for c in calls} == {str(job_id), None}


# --- emission happens after commit ------------------------------------------


def test_recompute_enqueue_happens_after_the_transaction_commits(monkeypatch: Any) -> None:
    _txn, enqueue, _redis, order = _install_fakes(monkeypatch)
    item = _make_result(scrape_job_id=uuid.uuid4())

    _flush_batch(WORKSPACE_ID, [item])

    assert "txn_exit" in order
    assert "enqueue" in order
    # The transaction's __exit__ (commit boundary) always precedes every
    # enqueue call in this flush -- never emitted mid-transaction.
    txn_exit_index = order.index("txn_exit")
    assert all(
        idx > txn_exit_index for idx, event in enumerate(order) if event == "enqueue"
    )
