"""Structured observability log/event unit tests (SPEC-11 US4 T030,
`contracts/observability.md`, Constitution §31).

SPEC-14 T004/T006 (`contracts/shared-extraction.md`) moved the admission
machinery this file drives (`_acquire_fetch_permission`/
`_overflow_to_dispatch`/`_dispatch`'s reusable body) out of
``price_monitor.spiders.generic_price_spider`` into
``scrape_core.targets`` as free functions (so the browser spider can
share it too) -- the spider's own methods are now thin wrappers. The
actual ``acquire_permission``/``deferred_delay``/``get_redis_client``/
``acquire_lock``/``release_slot``/``run_in_thread`` calls this test
forces now execute with ``scrape_core.targets``'s own globals (where
they're defined), and the ``log_event`` calls in that code use
``scrape_core.targets``'s own module logger -- so this file monkeypatches
``scrape_core.targets`` (not the spider module) and reads events back
off the ``scrape_core.targets`` logger name. The spider-level assertions
(return values, event names/fields) are unchanged -- only *where* the
code now lives moved.

Runs entirely without infra (no Docker daemon in this build env, project
memory) -- every Redis/DB/Celery/reactor touchpoint on each exercised
code path is monkeypatched with a pure, synchronous (or already-fired
``Deferred``-returning) fake, mirroring the established conventions in
this test suite:

* Spider methods (:mod:`price_monitor.spiders.generic_price_spider`) are
  driven directly via ``asyncio.run(...)`` -- a Twisted ``Deferred``
  that has already fired (``d.callback(None)`` before ``return``, as
  ``tests/integration/test_spider_overflow.py``'s in-subprocess runner
  does) is awaitable under a plain ``asyncio`` event loop with no
  installed Twisted reactor, so no reactor/thread-pool is ever started.
* ``scrape_core.pipelines._flush_batch`` is driven directly with the
  same ``workspace_txn``/``mark_target``/``enqueue``/``get_redis_client``
  monkeypatch shape ``tests/unit/test_persistence_batching.py`` already
  established for this exact function.

Covers the six events named in `contracts/observability.md`'s
"Structured logs / counters" table -- for each, asserts the parsed JSON
log line's ``event`` name and that every documented field key is
present (not necessarily their exact values, which other unit/
integration tests already cover):

    rate_limit.hit        workspace_id, domain, access_method, wait_hint
    rate_limit.requeue     workspace_id, match_id, requeue_count, delay
    rate_limit.overflow    workspace_id, scrape_job_id, match_id
    semaphore.denied       workspace_id, domain, access_method
    dedup.skip             workspace_id, match_id
    dedup.release          workspace_id, match_id, released
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import pytest
from twisted.internet.defer import Deferred

from app_shared.access.engine import AttemptPlan
from app_shared.enums import AccessMethod, RobotsPolicy

from price_monitor.spiders import generic_price_spider as gps
from scrape_core import pipelines as pipelines_mod
from scrape_core import targets as targets_mod
from scrape_core.items import ScrapeResult
from scrape_core.limiter import Permission

# --- shared helpers ----------------------------------------------------------


def _json_events(caplog: pytest.LogCaptureFixture, logger_name: str) -> list[dict[str, Any]]:
    """Every ``caplog`` record from ``logger_name`` that parses as one JSON
    object -- i.e. every :func:`scrape_core.observability.log_event` line,
    in emission order."""
    events: list[dict[str, Any]] = []
    for record in caplog.records:
        if record.name != logger_name:
            continue
        try:
            payload = json.loads(record.getMessage())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and "event" in payload:
            events.append(payload)
    return events


def _instant_delay(seconds: float) -> Deferred:
    """Stand-in for ``scrape_core.reactor.deferred_delay`` -- an
    already-fired ``Deferred`` (mirrors
    ``test_spider_overflow.py``'s ``_instant_delay``), so the backoff
    loop never spends real wall-clock time and never needs a running
    Twisted reactor."""
    d: Deferred = Deferred()
    d.callback(None)
    return d


def _target(match_id: uuid.UUID | None = None, *, domain: str = "shop.example.com") -> gps.SpiderTarget:
    return gps.SpiderTarget(
        match_id=match_id or uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        url=f"https://{domain}/product/1",
        profile=None,
        robots_policy=RobotsPolicy.RESPECT,
        domain=domain,
        access_policy=None,
        domain_rule=None,
    )


#: SPEC-14 T006: the admission machinery (and its `log_event` calls) now
#: lives in `scrape_core.targets`, not the spider module -- see the module
#: docstring.
_SPIDER_LOGGER = "scrape_core.targets"
_PIPELINE_LOGGER = "scrape_core.pipelines"


class _FakeSettings:
    """Stand-in for `app_shared.config.Settings` -- just the SPEC-11 knobs
    (`contracts/observability.md` defaults) `_acquire_fetch_permission`/
    `resolve_limits` read, no real env/pydantic validation (mirrors
    `tests/unit/test_rate_limiter.py`'s `_FakeSettings` convention -- this
    suite never constructs a real `Settings()`, which would require every
    `DATABASE_URL`/`REDIS_URL`/... env var to be set)."""

    RATE_LIMIT_DEFAULT_PER_MINUTE = 60
    RATE_LIMIT_DEFAULT_CONCURRENCY = 4
    RATE_LIMIT_KEY_TTL_SLACK_SECONDS = 120
    SEMAPHORE_SLOT_TTL_SECONDS = 600
    MATCH_LOCK_HTTP_TTL_SECONDS = 600
    MATCH_LOCK_BROWSER_TTL_SECONDS = 1800
    REQUEUE_MAX_ATTEMPTS = 5
    REQUEUE_MAX_TOTAL_WAIT_SECONDS = 300
    RATE_LIMIT_JITTER_MIN_SECONDS = 0
    RATE_LIMIT_JITTER_MAX_SECONDS = 0.01


def _patch_get_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`acquire_fetch_permission`/`dispatch_admission` (`scrape_core.targets`,
    SPEC-14 T006 -- moved out of the spider) both do a fresh, local
    ``from app_shared.config import get_settings`` on every call -- so
    patching the real ``app_shared.config.get_settings`` name (rather
    than anything on the ``targets_mod`` module) is what those local
    imports actually resolve to. Also stubs ``targets_mod.get_redis_client``
    -- its real implementation (``app_shared.redis_client``) calls its own
    module-level ``get_settings`` reference (bound at import time, so
    patching ``app_shared.config.get_settings`` alone would not reach
    it) to build a real ``Settings()``, which needs every required env
    var set; every code path exercised here reaches it only through an
    already-monkeypatched ``acquire_permission``/``acquire_lock``/
    ``release_slot`` that never actually uses the client it's given."""
    monkeypatch.setattr("app_shared.config.get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(targets_mod, "get_redis_client", lambda: object())


# --- rate_limit.hit + rate_limit.requeue (bucket denial then grant) ---------


def test_rate_limit_hit_and_requeue_events_carry_documented_fields(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"n": 0}

    async def _fake_acquire_permission(
        redis: Any, *, workspace_id: Any, domain: Any, access_method: Any, limits: Any, settings: Any, sem_token: Any
    ) -> Permission:
        calls["n"] += 1
        if calls["n"] == 1:
            # Token bucket itself denies -- Permission.denied_by="bucket".
            return Permission(granted=False, wait_hint_seconds=5, denied_by="bucket")
        return Permission(granted=True, wait_hint_seconds=0, semaphore_key="sem-key", semaphore_token="sem-token")

    monkeypatch.setattr(targets_mod, "acquire_permission", _fake_acquire_permission)
    monkeypatch.setattr(targets_mod, "deferred_delay", _instant_delay)
    _patch_get_settings(monkeypatch)

    spider = gps.GenericPriceSpider(workspace_id=str(uuid.uuid4()), match_ids=str(uuid.uuid4()))
    target = _target()
    spider._requeue_state_by_match_id[target.match_id] = gps._RequeueState()

    caplog.set_level(logging.INFO)
    perm = asyncio.run(spider._acquire_fetch_permission(target, AccessMethod.DIRECT_HTTP))

    assert perm is not None and perm.granted
    events = _json_events(caplog, _SPIDER_LOGGER)

    hit = next(e for e in events if e["event"] == "rate_limit.hit")
    assert {"workspace_id", "domain", "access_method", "wait_hint"} <= set(hit)
    assert hit["domain"] == target.domain
    assert hit["wait_hint"] == 5

    requeue = next(e for e in events if e["event"] == "rate_limit.requeue")
    assert {"workspace_id", "match_id", "requeue_count", "delay"} <= set(requeue)
    assert requeue["requeue_count"] == 1
    assert requeue["match_id"] == str(target.match_id)

    # A granted permission never emits `semaphore.denied` -- that's a
    # distinct denial cause (see the next test).
    assert not any(e["event"] == "semaphore.denied" for e in events)


# --- semaphore.denied (bucket grants, semaphore full) -----------------------


def test_semaphore_denied_event_carries_documented_fields(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"n": 0}

    async def _fake_acquire_permission(
        redis: Any, *, workspace_id: Any, domain: Any, access_method: Any, limits: Any, settings: Any, sem_token: Any
    ) -> Permission:
        calls["n"] += 1
        if calls["n"] == 1:
            return Permission(granted=False, wait_hint_seconds=1, denied_by="semaphore")
        return Permission(granted=True, wait_hint_seconds=0, semaphore_key="sem-key", semaphore_token="sem-token")

    monkeypatch.setattr(targets_mod, "acquire_permission", _fake_acquire_permission)
    monkeypatch.setattr(targets_mod, "deferred_delay", _instant_delay)
    _patch_get_settings(monkeypatch)

    spider = gps.GenericPriceSpider(workspace_id=str(uuid.uuid4()), match_ids=str(uuid.uuid4()))
    target = _target()
    spider._requeue_state_by_match_id[target.match_id] = gps._RequeueState()

    caplog.set_level(logging.INFO)
    perm = asyncio.run(spider._acquire_fetch_permission(target, AccessMethod.DIRECT_HTTP))

    assert perm is not None and perm.granted
    events = _json_events(caplog, _SPIDER_LOGGER)

    denied = next(e for e in events if e["event"] == "semaphore.denied")
    assert {"workspace_id", "domain", "access_method"} <= set(denied)
    assert denied["domain"] == target.domain

    # A semaphore denial is never also reported as a `rate_limit.hit` --
    # the two are mutually exclusive causes for one denial.
    assert not any(e["event"] == "rate_limit.hit" for e in events)


# --- rate_limit.overflow (requeue cap exceeded) -----------------------------


def test_rate_limit_overflow_event_carries_documented_fields(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    run_in_thread_calls: list[Any] = []

    async def _fake_run_in_thread(fn: Any, *args: Any, **kwargs: Any) -> None:
        # Records the call but never actually invokes `fn` -- `fn` here
        # is `_mark_target_deferred_rate_limited`/`enqueue`, both of
        # which touch real DB/Celery in production; this test only
        # verifies the `rate_limit.overflow` log emission that follows.
        run_in_thread_calls.append((fn, args, kwargs))
        return None

    monkeypatch.setattr(targets_mod, "run_in_thread", _fake_run_in_thread)

    scrape_job_id = uuid.uuid4()
    spider = gps.GenericPriceSpider(
        workspace_id=str(uuid.uuid4()), scrape_job_id=str(scrape_job_id), match_ids=str(uuid.uuid4())
    )
    target = _target()
    # A denied Permission never carries a semaphore key/token (the
    # semaphore is never touched on a bucket denial) -- mirrors the real
    # `_acquire_fetch_permission` denial shape.
    perm = Permission(granted=False, wait_hint_seconds=5, denied_by="bucket")

    caplog.set_level(logging.INFO)
    asyncio.run(spider._overflow_to_dispatch(target, perm, redis=object()))

    assert len(run_in_thread_calls) == 2  # mark DEFERRED + re-dispatch enqueue
    events = _json_events(caplog, _SPIDER_LOGGER)

    overflow = next(e for e in events if e["event"] == "rate_limit.overflow")
    assert {"workspace_id", "scrape_job_id", "match_id"} <= set(overflow)
    assert overflow["scrape_job_id"] == str(scrape_job_id)
    assert overflow["match_id"] == str(target.match_id)


# --- dedup.skip (match lock already held) -----------------------------------


def test_dedup_skip_event_carries_documented_fields(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _fake_acquire_permission(
        redis: Any, *, workspace_id: Any, domain: Any, access_method: Any, limits: Any, settings: Any, sem_token: Any
    ) -> Permission:
        return Permission(granted=True, wait_hint_seconds=0, semaphore_key="sem-key", semaphore_token="sem-token")

    async def _fake_acquire_lock(
        redis: Any, *, workspace_id: Any, match_id: Any, mode: Any, settings: Any
    ) -> None:
        return None  # already held

    async def _fake_release_slot(redis: Any, *, key: Any, token: Any) -> None:
        return None

    monkeypatch.setattr(targets_mod, "acquire_permission", _fake_acquire_permission)
    monkeypatch.setattr(targets_mod, "acquire_lock", _fake_acquire_lock)
    monkeypatch.setattr(targets_mod, "release_slot", _fake_release_slot)
    _patch_get_settings(monkeypatch)

    spider = gps.GenericPriceSpider(workspace_id=str(uuid.uuid4()), match_ids=str(uuid.uuid4()))
    target = _target()
    spider._requeue_state_by_match_id[target.match_id] = gps._RequeueState()
    plan = AttemptPlan(access_method=AccessMethod.DIRECT_HTTP, use_proxy=False)

    caplog.set_level(logging.INFO)
    result = asyncio.run(spider._dispatch(target, 1, plan, None))

    assert isinstance(result, ScrapeResult)
    assert result.success is False
    events = _json_events(caplog, _SPIDER_LOGGER)

    skip = next(e for e in events if e["event"] == "dedup.skip")
    assert {"workspace_id", "match_id"} <= set(skip)
    assert skip["match_id"] == str(target.match_id)


# --- dedup.release (lock released post-persist) -----------------------------


class _FakeSession:
    def add_all(self, items: Any) -> None:
        pass

    def execute(self, stmt: Any) -> None:
        pass


class _FakeWorkspaceTxn:
    def __call__(self, workspace_id: Any) -> "_FakeWorkspaceTxn":
        return self

    def __enter__(self) -> _FakeSession:
        return _FakeSession()

    def __exit__(self, *exc_info: Any) -> bool:
        return False


class _FakePipelineSettings:
    """Stand-in for `Settings` -- the SPEC-09 recompute-dedup field plus
    the two SPEC-12 US5 T037 fields `_flush_batch` now also reads
    unconditionally (mirrors `tests/unit/test_persistence_batching.py`'s
    `_FakeSettings`; named differently here since this file's
    `_FakeSettings` above already covers the SPEC-11 rate-limit knobs).
    This file's seeded `ScrapeResult` carries no `domain_strategy_profile_id`,
    so `record_attempt` itself is never actually invoked here -- only the
    attribute reads need satisfying."""

    PRICE_ANALYSIS_DEDUP_TTL_SECONDS = 21600
    STRATEGY_STATS_KEY_TTL_SECONDS = 3600
    STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD = 0.85


def test_dedup_release_event_carries_documented_fields(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Mirrors `tests/unit/test_persistence_batching.py`'s fake-workspace_txn
    # convention -- no real DB/Redis/Celery anywhere in this test.
    monkeypatch.setattr(pipelines_mod, "workspace_txn", _FakeWorkspaceTxn())
    monkeypatch.setattr(pipelines_mod, "mark_target", lambda *a, **k: None)
    monkeypatch.setattr(pipelines_mod, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(pipelines_mod, "get_settings", lambda: _FakePipelineSettings())
    monkeypatch.setattr(pipelines_mod, "get_redis_client", lambda: object())

    release_calls: list[dict[str, Any]] = []

    def _fake_release_match_lock(redis: Any, *, key: Any, token: Any) -> bool:
        release_calls.append({"key": key, "token": token})
        return True

    monkeypatch.setattr(pipelines_mod, "release_match_lock", _fake_release_match_lock)

    workspace_id = uuid.uuid4()
    match_id = uuid.uuid4()
    item = ScrapeResult(
        workspace_id=workspace_id,
        match_id=match_id,
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        scrape_job_id=None,
        url="https://shop.example.com/product/1",
        access_method=AccessMethod.DIRECT_HTTP,
        success=True,
        match_lock_key="lock:scrape:ws:match",
        match_lock_token="fencing-token-abc",
    )

    caplog.set_level(logging.INFO)
    pipelines_mod._flush_batch(workspace_id, [item])

    assert len(release_calls) == 1
    events = _json_events(caplog, _PIPELINE_LOGGER)

    release = next(e for e in events if e["event"] == "dedup.release")
    assert {"workspace_id", "match_id", "released"} <= set(release)
    assert release["match_id"] == str(match_id)
    assert release["released"] is True
