"""Audited job cancellation + reconciliation (EPA A2).

`app_shared.jobs.cancellation.cancel_and_reconcile_job` is the ONLY
sanctioned way to close a stranded job. What has to be true:

1. **No invented successes.** Only non-terminal targets
   (`PENDING`/`STARTED`/`DEFERRED`) are terminalized, and they become
   `CANCELLED` — never `COMPLETED`. A `COMPLETED`/`FAILED`/`SKIPPED`
   target is never touched again.
2. **The fence is written FIRST.** The same transaction bumps
   `scrape_jobs.cancellation_generation` and sets the job's status to
   `CANCELLED` *before* anything outside the database is touched, so a
   crash after the commit can never leave "DB says CANCELLED" contradicted
   by a late result.
3. **Late results are rejected, not persisted.** A scraper result that
   arrives after the fence lands in an audited `late_after_cancel`
   outcome — the `request_attempts` row records the rejection, and **zero**
   `price_observations` rows are written for it.
4. **Idempotent.** A second call cancels nothing, reports
   `idempotent_replay=True`, and emits no second event.
5. **Exactly one durable event** per job cancellation, deduped on
   `job-cancel:{scrape_job_id}`.
6. **`mark_target` stays the single transition writer** — cancellation
   never updates a `scrape_job_targets` row directly.

Exercised against the `FakeOrmSession` evaluator
(`tests/unit/_jobs_fake_session.py`, the SPEC-08 jobs-test convention)
extended with `Insert`-statement recording for the outbox write, plus the
`_flush_batch` fake-`workspace_txn` pattern from
`tests/unit/test_pipeline_target_terminalization.py` for the late-result
path — no real Postgres, no Redis, no broker.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Insert

from app_shared.enums import (
    AccessMethod,
    ExtractionMethod,
    ScrapeJobSource,
    ScrapeJobStatus,
    ScrapeJobType,
    ScrapeScope,
    ScrapeTargetStatus,
    StockStatus,
)
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
from app_shared.task_names import CREATE_WEBHOOK_EVENT

from unit._jobs_fake_session import FakeOrmSession

WORKSPACE_ID = uuid.uuid4()
ACTOR = "ops@crawmatic.test"
REASON = "stranded canary closed at Gate B"
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _CancellationSession(FakeOrmSession):
    """`FakeOrmSession` + `Insert` recording + a real `in_transaction()`.

    `cancel_and_reconcile_job` writes its event through
    `write_outbox_message`, which issues a `postgresql.insert(...)` —
    a statement shape `FakeOrmSession.execute` (a `Select` evaluator)
    knows nothing about. Recording it here is what lets the outbox
    assertions below read the real compiled parameters rather than a
    stubbed-out call recorder.

    `in_transaction()` returns True: this fake stands in for the
    already-workspace-scoped request/worker session, which is the posture
    `cancel_and_reconcile_job` must accept without opening a nested
    `workspace_context` (`SET LOCAL app.workspace_id` is transaction-local
    — see `app_shared.maintenance.scoping.WorkspaceContextError`).

    `events` is an ordered log of the things whose *relative order* the
    module's ordering protocol constrains — `"commit"` is appended here,
    and the out-of-band steps append their own names once a test stubs
    them. It exists so "the fence is committed BEFORE steps 2-4" can be
    asserted as a sequence rather than inferred from side effects.
    """

    def __init__(self) -> None:
        super().__init__()
        self.inserts: list[Any] = []
        self.events: list[str] = []

    def in_transaction(self) -> bool:
        return True

    def commit(self) -> None:
        self.events.append("commit")
        super().commit()

    def execute(self, stmt: Any) -> Any:
        if isinstance(stmt, Insert):
            self.inserts.append(stmt)
            return None
        return super().execute(stmt)

    def outbox_params(self) -> list[dict[str, Any]]:
        return [
            dict(stmt.compile(dialect=postgresql.dialect()).params) for stmt in self.inserts
        ]


def _job(status: ScrapeJobStatus = ScrapeJobStatus.RUNNING, generation: int = 0) -> ScrapeJob:
    return ScrapeJob(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        type=ScrapeJobType.MANUAL,
        scope=ScrapeScope.VARIANT,
        status=status,
        source=ScrapeJobSource.API,
        total_targets=0,
        success_count=0,
        failure_count=0,
        skipped_count=0,
        cancellation_generation=generation,
        created_at=NOW,
    )


def _target(job: ScrapeJob, status: ScrapeTargetStatus) -> ScrapeJobTarget:
    return ScrapeJobTarget(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        scrape_job_id=job.id,
        match_id=uuid.uuid4(),
        status=status,
        strategy_attempt_ordinal=0,
        created_at=NOW,
    )


def _seeded(
    *statuses: ScrapeTargetStatus,
    job_status: ScrapeJobStatus = ScrapeJobStatus.RUNNING,
    generation: int = 0,
) -> tuple[_CancellationSession, ScrapeJob, list[ScrapeJobTarget]]:
    session = _CancellationSession()
    job = _job(status=job_status, generation=generation)
    targets = [_target(job, status) for status in statuses]
    session.seed(job, *targets)
    return session, job, targets


# --------------------------------------------------------------------------
# 1. Only non-terminal targets are terminalized — never as a success
# --------------------------------------------------------------------------


def test_cancel_terminalizes_only_pending_targets() -> None:
    from app_shared.jobs.cancellation import cancel_and_reconcile_job

    session, job, targets = _seeded(
        ScrapeTargetStatus.PENDING,
        ScrapeTargetStatus.PENDING,
        ScrapeTargetStatus.PENDING,
        ScrapeTargetStatus.COMPLETED,
        ScrapeTargetStatus.COMPLETED,
        ScrapeTargetStatus.FAILED,
    )

    report = cancel_and_reconcile_job(
        session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON
    )

    assert report.job_id == str(job.id)
    assert report.targets_cancelled == 3
    assert report.targets_already_terminal == 3
    assert report.idempotent_replay is False

    by_status = [target.status for target in targets]
    assert by_status.count(ScrapeTargetStatus.CANCELLED) == 3
    # The pre-existing terminal outcomes are untouched — cancellation
    # never rewrites a real result, in either direction.
    assert by_status.count(ScrapeTargetStatus.COMPLETED) == 2
    assert by_status.count(ScrapeTargetStatus.FAILED) == 1

    # The audit trail lands on the rows that actually moved.
    for target in targets:
        if target.status is ScrapeTargetStatus.CANCELLED:
            assert target.cancelled_by == ACTOR
            assert target.cancelled_reason == REASON
            assert target.cancelled_at is not None
        else:
            assert target.cancelled_at is None


def test_cancel_never_marks_a_target_completed() -> None:
    """The whole point of the audited path: no invented successes."""
    from app_shared.jobs.cancellation import cancel_and_reconcile_job

    session, job, targets = _seeded(
        ScrapeTargetStatus.PENDING,
        ScrapeTargetStatus.STARTED,
        ScrapeTargetStatus.DEFERRED,
    )

    cancel_and_reconcile_job(session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON)

    assert {target.status for target in targets} == {ScrapeTargetStatus.CANCELLED}
    assert job.success_count == 0


# --------------------------------------------------------------------------
# 2. The fence
# --------------------------------------------------------------------------


def test_cancel_bumps_fence_generation() -> None:
    from app_shared.jobs.cancellation import cancel_and_reconcile_job

    session, job, _targets = _seeded(ScrapeTargetStatus.PENDING, generation=4)

    cancel_and_reconcile_job(session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON)

    assert job.cancellation_generation == 5
    assert job.status is ScrapeJobStatus.CANCELLED


def test_cancel_fences_before_it_terminalizes_any_target(monkeypatch: Any) -> None:
    """Ordering protocol step 1: fence FIRST, inside the same transaction.

    A `mark_target` that runs before the generation bump would leave a
    window in which a target is already closed while dispatch still reads
    a stale generation as current.
    """
    import app_shared.jobs.cancellation as cancellation_mod

    session, job, _targets = _seeded(
        ScrapeTargetStatus.PENDING, ScrapeTargetStatus.PENDING
    )
    observed: list[tuple[int, ScrapeJobStatus]] = []
    real_mark_target = cancellation_mod.mark_target

    def _spy(*args: Any, **kwargs: Any) -> None:
        observed.append((job.cancellation_generation, job.status))
        return real_mark_target(*args, **kwargs)

    monkeypatch.setattr(cancellation_mod, "mark_target", _spy)
    cancellation_mod.cancel_and_reconcile_job(
        session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON
    )

    assert observed, "mark_target was never called"
    assert all(
        generation == 1 and status is ScrapeJobStatus.CANCELLED
        for generation, status in observed
    )


# --------------------------------------------------------------------------
# 3. Late results after the fence
# --------------------------------------------------------------------------


def test_late_result_after_cancel_is_rejected_not_persisted(monkeypatch: Any) -> None:
    """A result arriving after the fence writes NO observation.

    It is not silently dropped either: the `request_attempts` row records
    the rejection with `late_after_cancel`, so the outcome is auditable.
    """
    from app_shared.models.observations import PriceObservation, RequestAttempt

    from scrape_core import pipelines as pipelines_mod
    from scrape_core.items import ScrapeResult

    cancelled_job_id = uuid.uuid4()

    class _FakeSession:
        def __init__(self) -> None:
            self.added: list[Any] = []

        def add_all(self, items: Any) -> None:
            self.added.extend(items)

        def execute(self, stmt: Any) -> Any:
            return None

    # EPA F05 (plan task B1): the observation/attempt inserts are now
    # `ON CONFLICT DO NOTHING` Core statements rather than ORM `add_all`,
    # so the instances are routed back through `add_all` here -- this
    # test is about WHICH rows a fenced flush writes, not about how they
    # reach the driver.
    monkeypatch.setattr(
        pipelines_mod,
        "_insert_ignoring_replays",
        lambda session, model, instances, index_elements: session.add_all(instances),
    )

    class _FakeTxn:
        def __init__(self, session: _FakeSession) -> None:
            self._session = session

        def __call__(self, workspace_id: Any) -> "_FakeTxn":
            return self

        def __enter__(self) -> _FakeSession:
            return self._session

        def __exit__(self, *exc_info: Any) -> bool:
            return False

    class _FakeSettings:
        PRICE_ANALYSIS_DEDUP_TTL_SECONDS = 21600
        STRATEGY_STATS_KEY_TTL_SECONDS = 3600
        STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD = 0.85
        SCRAPE_MAX_DEFER_CYCLES = 3

    class _FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        def set(
            self, name: str, value: str, *, nx: bool = False, ex: int | None = None
        ) -> bool | None:
            if nx and name in self.store:
                return None
            self.store[name] = value
            return True

    session = _FakeSession()
    marked: list[Any] = []
    monkeypatch.setattr(pipelines_mod, "workspace_txn", _FakeTxn(session))
    monkeypatch.setattr(pipelines_mod, "mark_target", lambda *a, **k: marked.append(k))
    monkeypatch.setattr(pipelines_mod, "write_outbox_message", lambda *a, **k: None)
    monkeypatch.setattr(pipelines_mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(pipelines_mod, "get_redis_client", lambda: _FakeRedis())
    # The fence: this job is CANCELLED in the database.
    monkeypatch.setattr(
        pipelines_mod,
        "cancelled_scrape_job_ids",
        lambda _session, job_ids: {job_id for job_id in job_ids if job_id == cancelled_job_id},
    )

    item = ScrapeResult(
        workspace_id=WORKSPACE_ID,
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        scrape_job_id=cancelled_job_id,
        url="https://shop.example.com/p/1",
        access_method=AccessMethod.DIRECT_HTTP,
        success=True,
        price=Decimal("9.99"),
        currency="USD",
        stock_status=StockStatus.IN_STOCK,
        extraction_method=ExtractionMethod.JSON_LD,
        extraction_confidence=Decimal("0.9500"),
        chain_complete=True,
    )

    pipelines_mod._flush_batch(WORKSPACE_ID, [item])

    observations = [row for row in session.added if isinstance(row, PriceObservation)]
    attempts = [row for row in session.added if isinstance(row, RequestAttempt)]

    assert observations == [], "a late result after cancellation must persist NO observation"
    assert len(attempts) == 1, "the rejected attempt is still audited"
    rejected_reason = attempts[0].error_message
    assert rejected_reason == "late_after_cancel"
    assert attempts[0].success is False
    # Provenance, not outcome: the cancellation closed the target, this
    # attempt did not (EPA Phase A review F-6). Asserted on a fetch that
    # genuinely succeeded, which is the case that used to be mislabelled.
    assert attempts[0].terminal_for_target is False
    # A cancelled job's target is already terminal — nothing to transition.
    assert marked == []


def test_uncancelled_job_results_still_persist(monkeypatch: Any) -> None:
    """The fence must not become a blanket drop — control case."""
    from app_shared.models.observations import PriceObservation

    from scrape_core import pipelines as pipelines_mod
    from scrape_core.items import ScrapeResult

    class _FakeSession:
        def __init__(self) -> None:
            self.added: list[Any] = []

        def add_all(self, items: Any) -> None:
            self.added.extend(items)

        def execute(self, stmt: Any) -> Any:
            return None

    # EPA F05 (plan task B1): the observation/attempt inserts are now
    # `ON CONFLICT DO NOTHING` Core statements rather than ORM `add_all`,
    # so the instances are routed back through `add_all` here -- this
    # test is about WHICH rows a fenced flush writes, not about how they
    # reach the driver.
    monkeypatch.setattr(
        pipelines_mod,
        "_insert_ignoring_replays",
        lambda session, model, instances, index_elements: session.add_all(instances),
    )

    class _FakeTxn:
        def __init__(self, session: _FakeSession) -> None:
            self._session = session

        def __call__(self, workspace_id: Any) -> "_FakeTxn":
            return self

        def __enter__(self) -> _FakeSession:
            return self._session

        def __exit__(self, *exc_info: Any) -> bool:
            return False

    class _FakeSettings:
        PRICE_ANALYSIS_DEDUP_TTL_SECONDS = 21600
        STRATEGY_STATS_KEY_TTL_SECONDS = 3600
        STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD = 0.85
        SCRAPE_MAX_DEFER_CYCLES = 3

    class _FakeRedis:
        def set(self, *a: Any, **k: Any) -> bool:
            return True

    session = _FakeSession()
    monkeypatch.setattr(pipelines_mod, "workspace_txn", _FakeTxn(session))
    monkeypatch.setattr(pipelines_mod, "mark_target", lambda *a, **k: None)
    monkeypatch.setattr(pipelines_mod, "write_outbox_message", lambda *a, **k: None)
    monkeypatch.setattr(pipelines_mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(pipelines_mod, "get_redis_client", lambda: _FakeRedis())
    monkeypatch.setattr(pipelines_mod, "cancelled_scrape_job_ids", lambda _s, _j: set())

    item = ScrapeResult(
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
        chain_complete=True,
    )
    pipelines_mod._flush_batch(WORKSPACE_ID, [item])

    assert len([row for row in session.added if isinstance(row, PriceObservation)]) == 1


# --------------------------------------------------------------------------
# 4. Idempotence
# --------------------------------------------------------------------------


def test_cancel_is_idempotent() -> None:
    from app_shared.jobs.cancellation import cancel_and_reconcile_job

    session, job, _targets = _seeded(
        ScrapeTargetStatus.PENDING,
        ScrapeTargetStatus.PENDING,
        ScrapeTargetStatus.COMPLETED,
    )

    first = cancel_and_reconcile_job(
        session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON
    )
    second = cancel_and_reconcile_job(
        session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON
    )

    assert first.targets_cancelled == 2
    assert first.idempotent_replay is False

    assert second.targets_cancelled == 0
    assert second.idempotent_replay is True
    assert second.targets_already_terminal == 3
    # The fence is bumped once, not once per replay.
    assert job.cancellation_generation == 1


# --------------------------------------------------------------------------
# 5. Exactly one durable event
# --------------------------------------------------------------------------


def test_cancel_enqueues_one_outbox_message() -> None:
    from app_shared.jobs.cancellation import cancel_and_reconcile_job

    session, job, _targets = _seeded(ScrapeTargetStatus.PENDING)

    first = cancel_and_reconcile_job(
        session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON
    )
    second = cancel_and_reconcile_job(
        session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON
    )

    params = session.outbox_params()
    assert len(params) == 1, f"expected exactly one outbox message, got {len(params)}"
    assert params[0]["dedup_key"] == f"job-cancel:{job.id}"
    assert params[0]["task_name"] == CREATE_WEBHOOK_EVENT
    assert params[0]["queue"] == "webhook_events"

    assert first.outbox_message_id is not None
    assert second.outbox_message_id is None


def test_cancel_event_carries_actor_and_reason() -> None:
    from app_shared.jobs.cancellation import cancel_and_reconcile_job

    session, job, _targets = _seeded(ScrapeTargetStatus.PENDING)

    cancel_and_reconcile_job(session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON)

    payload = session.outbox_params()[0]["payload"]["payload"]
    assert payload["scrape_job_id"] == str(job.id)
    assert payload["cancelled_by"] == ACTOR
    assert payload["reason"] == REASON
    assert payload["targets_cancelled"] == 1


# --------------------------------------------------------------------------
# 6. `mark_target` remains the single transition writer
# --------------------------------------------------------------------------


def test_cancel_goes_through_mark_target_single_writer(monkeypatch: Any) -> None:
    import app_shared.jobs.cancellation as cancellation_mod

    session, job, targets = _seeded(
        ScrapeTargetStatus.PENDING,
        ScrapeTargetStatus.STARTED,
        ScrapeTargetStatus.DEFERRED,
        ScrapeTargetStatus.COMPLETED,
        ScrapeTargetStatus.SKIPPED,
    )
    non_terminal = 3

    calls: list[dict[str, Any]] = []
    real_mark_target = cancellation_mod.mark_target

    def _spy(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs)
        return real_mark_target(*args, **kwargs)

    monkeypatch.setattr(cancellation_mod, "mark_target", _spy)
    cancellation_mod.cancel_and_reconcile_job(
        session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON
    )

    assert len(calls) == non_terminal
    assert all(call["status"] is ScrapeTargetStatus.CANCELLED for call in calls)
    assert {call["match_id"] for call in calls} == {
        target.match_id
        for target in targets
        if target.status is ScrapeTargetStatus.CANCELLED
    }


# --------------------------------------------------------------------------
# Ordering protocol steps 2-4
# --------------------------------------------------------------------------


def _log_out_of_band_steps(
    monkeypatch: Any, cancellation_mod: Any, session: _CancellationSession
) -> None:
    """Replace steps 2-4 with recorders on `session.events`.

    Stubbing all three (rather than just Redis) is what makes the
    assertion below about *ordering* instead of about reachability: each
    step announces itself into the same log the session's `commit()`
    writes to, so the protocol's sequence is directly observable.
    """
    monkeypatch.setattr(
        cancellation_mod,
        "_best_effort_scrapyd_cancel",
        lambda *_a, **_k: session.events.append("scrapyd"),
    )
    monkeypatch.setattr(
        cancellation_mod,
        "_delete_dispatch_guards",
        lambda *_a, **_k: session.events.append("guards"),
    )

    def _release(*_a: Any, **_k: Any) -> int:
        session.events.append("reservations")
        return 0

    monkeypatch.setattr(cancellation_mod, "release_reservations_for_job", _release)


def test_fence_is_committed_before_any_out_of_band_step(monkeypatch: Any) -> None:
    """Steps 2-4 may only run against a COMMITTED fence (EPA review F-1).

    The module's own protocol justifies deleting the `dispatched:{job}:*`
    guards with "safe only *because* step 1 already committed". On a
    session handed over already inside a transaction — the API request
    posture, where the caller commits only after the handler returns —
    that was not true: the cleanup ran first. So the commit is now the
    function's own responsibility, and this pins it.

    Fails if the explicit `session.commit()` is removed: `"commit"` then
    never reaches the log at all.
    """
    import app_shared.jobs.cancellation as cancellation_mod

    session, job, _targets = _seeded(ScrapeTargetStatus.PENDING)
    _log_out_of_band_steps(monkeypatch, cancellation_mod, session)

    cancellation_mod.cancel_and_reconcile_job(
        session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON
    )

    assert session.events == ["commit", "scrapyd", "guards", "reservations"]
    assert session.committed is True


def test_replay_also_commits_before_cleanup(monkeypatch: Any) -> None:
    """Idempotence does not get to skip the fence commit.

    A replay cancels nothing and emits no event, but it still re-runs the
    cleanup — so it must still leave the transaction closed before that
    cleanup touches Redis, or a crash mid-replay would reintroduce
    exactly the window this ordering exists to close.
    """
    import app_shared.jobs.cancellation as cancellation_mod

    session, job, _targets = _seeded(
        ScrapeTargetStatus.PENDING, job_status=ScrapeJobStatus.CANCELLED, generation=1
    )
    _log_out_of_band_steps(monkeypatch, cancellation_mod, session)

    report = cancellation_mod.cancel_and_reconcile_job(
        session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON
    )

    assert report.idempotent_replay is True
    assert session.events.index("commit") < session.events.index("guards")


def test_scrapyd_job_ids_interface_is_empty_until_b1() -> None:
    """`dispatch_intents` is B1's table; the seam exists and returns []."""
    from app_shared.jobs.cancellation import iter_known_scrapyd_job_ids

    assert list(iter_known_scrapyd_job_ids(_CancellationSession(), uuid.uuid4())) == []


def test_reservation_release_returns_zero_when_the_job_holds_none() -> None:
    """Step 4 is REAL as of EPA C3 — and still returns 0 with nothing to release.

    Replaces the pre-C3 `..._is_a_named_no_op_until_c3` assertion. The
    function now iterates the job's `RESERVED` `cost_reservations` rows
    and compare-and-sets each to `RELEASED`; a job holding none (this
    fake session seeds no reservations) releases none. The zero is now a
    *result*, not a stub — which is also the idempotence property: a
    second cancellation of the same job finds every row already terminal
    and likewise reports 0.
    """
    from app_shared.jobs.cancellation import release_reservations_for_job

    assert release_reservations_for_job(_CancellationSession(), uuid.uuid4()) == 0


def test_cancel_deletes_surviving_dispatch_guards(monkeypatch: Any) -> None:
    """Step 3: the `dispatched:{job}:*` sentinels go, AFTER the fence.

    Safe precisely because the fence is already committed — a re-dispatch
    that races the delete is rejected on the stale generation.
    """
    import app_shared.jobs.cancellation as cancellation_mod

    class _FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}
            self.deleted: list[str] = []

        def scan_iter(self, match: str = "*", count: int | None = None) -> Any:
            prefix = match.rstrip("*")
            return iter([key for key in list(self.store) if key.startswith(prefix)])

        def delete(self, *names: str) -> int:
            removed = 0
            for name in names:
                if self.store.pop(name, None) is not None:
                    self.deleted.append(name)
                    removed += 1
            return removed

    session, job, _targets = _seeded(ScrapeTargetStatus.PENDING)
    redis = _FakeRedis()
    redis.store[f"dispatched:{job.id}:0"] = "1"
    redis.store[f"dispatched:{job.id}:1"] = "1"
    redis.store[f"dispatched:{uuid.uuid4()}:0"] = "1"

    monkeypatch.setattr(cancellation_mod, "get_redis_client", lambda: redis)
    cancellation_mod.cancel_and_reconcile_job(
        session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON
    )

    assert sorted(redis.deleted) == [f"dispatched:{job.id}:0", f"dispatched:{job.id}:1"]
    assert len(redis.store) == 1, "another job's guard must survive"


def test_guard_deletion_failure_never_fails_the_cancellation(monkeypatch: Any) -> None:
    """Redis is not on the correctness path — the fence is already durable."""
    import app_shared.jobs.cancellation as cancellation_mod

    class _BrokenRedis:
        def scan_iter(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("redis is down")

        def delete(self, *names: str) -> int:  # pragma: no cover - never reached
            raise RuntimeError("redis is down")

    session, job, targets = _seeded(ScrapeTargetStatus.PENDING)
    monkeypatch.setattr(cancellation_mod, "get_redis_client", lambda: _BrokenRedis())

    report = cancellation_mod.cancel_and_reconcile_job(
        session, scrape_job_id=str(job.id), actor=ACTOR, reason=REASON
    )

    assert report.targets_cancelled == 1
    assert targets[0].status is ScrapeTargetStatus.CANCELLED


def test_cancel_unknown_job_raises_lookup_error() -> None:
    from app_shared.jobs.cancellation import cancel_and_reconcile_job

    session = _CancellationSession()
    try:
        cancel_and_reconcile_job(
            session, scrape_job_id=str(uuid.uuid4()), actor=ACTOR, reason=REASON
        )
    except LookupError:
        return
    raise AssertionError("cancelling an unresolvable job must raise LookupError")
