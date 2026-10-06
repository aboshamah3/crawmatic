"""`_prepare_dispatch` fail-closed-for-paid-work tests (audit 2026-08-15, H3).

This is the test that matters most for H3, because the failure mode this
change guards against has two ways to get it wrong and only one right
answer:

* deny too much -> a Redis outage stops ALL scraping, including the free
  direct traffic that is the majority of the catalog;
* deny too little -> the cost hole stays open and a Redis incident plus a
  retry/rediscovery loop spends money with no ceiling.

So every test below asserts BOTH halves of the split against the same
simulated Redis outage: a proxied plan must be denied, a direct plan must
proceed untouched.

`plan.use_proxy` is the discriminator under test -- it is what decides
whether `assign_proxy` runs, whether the monthly budget counter is
incremented, and whether the request is routed through DataImpulse.
"""

from __future__ import annotations

import uuid

import pytest

from app_shared.enums import AccessMethod, AccessStrategy, RobotsPolicy, ScrapeErrorCode
from app_shared.models.access import AccessPolicy

from scrape_core import targets as targets_mod
from scrape_core.targets import SpiderTarget, _prepare_dispatch


class _BrokenRedis:
    """Every command raises — a total Redis outage."""

    def incr(self, *_a: object, **_kw: object) -> int:
        raise ConnectionError("redis unavailable")

    def expire(self, *_a: object, **_kw: object) -> None:
        raise ConnectionError("redis unavailable")

    def ttl(self, *_a: object, **_kw: object) -> int:
        raise ConnectionError("redis unavailable")

    def set(self, *_a: object, **_kw: object) -> bool:
        raise ConnectionError("redis unavailable")


class _HealthyRedis:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key: str, seconds: int) -> None:
        return None

    def ttl(self, key: str) -> int:
        return -2

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        return True


def _policy(strategy: AccessStrategy, **overrides: object) -> AccessPolicy:
    """A minimally-populated AccessPolicy (never persisted)."""
    policy = AccessPolicy(
        id=uuid.uuid4(),
        name="test-policy",
        strategy=strategy,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
        max_requests_per_minute=60,
        max_requests_per_hour=None,
        max_requests_per_day=None,
        rotate_per_request=False,
        sticky_session=False,
        provider_id=None,
        country_code=None,
    )
    for key, value in overrides.items():
        setattr(policy, key, value)
    return policy


def _target(policy: AccessPolicy) -> SpiderTarget:
    return SpiderTarget(
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        url="https://shop.example.com/p/1",
        profile=None,
        robots_policy=RobotsPolicy.RESPECT,
        domain="shop.example.com",
        access_policy=policy,
    )


@pytest.fixture
def outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate Redis being completely down, breaker enabled and closed."""
    monkeypatch.setattr(targets_mod, "get_redis_client", lambda: _BrokenRedis())
    monkeypatch.setattr(targets_mod, "_breaker_allows_paid_work", lambda: (True, None))


@pytest.fixture
def healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(targets_mod, "get_redis_client", lambda: _HealthyRedis())
    monkeypatch.setattr(targets_mod, "_breaker_allows_paid_work", lambda: (True, None))


def _set_fail_open(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    class _S:
        PROXY_LEDGER_FAIL_OPEN = value

    monkeypatch.setattr(targets_mod, "_settings", lambda: _S())


# --- Redis down: DIRECT work continues --------------------------------------


def test_redis_outage_lets_a_direct_attempt_proceed(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DIRECT_ONLY never proxies, so an outage must not stop it at all."""
    _set_fail_open(monkeypatch, False)
    target = _target(_policy(AccessStrategy.DIRECT_ONLY))

    decision = _prepare_dispatch(target, 1, {}, {})

    assert decision.skip_error_code is None
    assert decision.plan is not None
    assert decision.plan.use_proxy is False
    assert decision.proxy is None


def test_redis_outage_lets_the_direct_step_of_a_mixed_strategy_proceed(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DIRECT_THEN_PROXY attempt 1 is direct — unaffected by the outage."""
    _set_fail_open(monkeypatch, False)
    target = _target(_policy(AccessStrategy.DIRECT_THEN_PROXY))

    decision = _prepare_dispatch(target, 1, {}, {})

    assert decision.skip_error_code is None
    assert decision.plan is not None
    assert decision.plan.use_proxy is False


# --- Redis down: PROXIED work is denied -------------------------------------


def test_redis_outage_degrades_a_proxied_retry_to_direct(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DIRECT_THEN_PROXY retry wants a proxy; with no ledger it must not
    get one — but the strategy has a direct step, so scraping continues
    unpaid rather than stopping."""
    _set_fail_open(monkeypatch, False)
    target = _target(_policy(AccessStrategy.DIRECT_THEN_PROXY))

    decision = _prepare_dispatch(target, 2, {}, {})

    assert decision.plan is not None
    assert decision.plan.use_proxy is False, "paid retry must not be authorised"
    assert decision.plan.access_method is AccessMethod.DIRECT_HTTP_RETRY
    assert decision.proxy is None


def test_redis_outage_stops_a_proxy_only_strategy_with_limit_reached(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PROXY_FIRST has no direct step to fall back to, so the target is
    skipped cleanly (a terminal, finalizable outcome) instead of being
    fetched through a paid proxy with no accounting."""
    _set_fail_open(monkeypatch, False)
    target = _target(_policy(AccessStrategy.PROXY_FIRST))

    decision = _prepare_dispatch(target, 1, {}, {})

    assert decision.plan is None
    assert decision.skip_error_code is ScrapeErrorCode.LIMIT_REACHED
    assert decision.attempted_method is AccessMethod.PROXY_HTTP


def test_redis_outage_stops_residential_only(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_fail_open(monkeypatch, False)
    target = _target(_policy(AccessStrategy.RESIDENTIAL_ONLY))

    decision = _prepare_dispatch(target, 1, {}, {})

    assert decision.skip_error_code is ScrapeErrorCode.LIMIT_REACHED


def test_paid_denial_never_assigns_a_proxy(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`assign_proxy` must not even be reached — no upstream session is
    opened for a request we have refused to pay for."""
    called: list[object] = []
    monkeypatch.setattr(
        targets_mod, "assign_proxy", lambda **kw: called.append(kw) or None
    )
    _set_fail_open(monkeypatch, False)

    _prepare_dispatch(_target(_policy(AccessStrategy.PROXY_FIRST)), 1, {}, {})

    assert called == []


# --- emergency override ------------------------------------------------------


def test_emergency_override_restores_fail_open_for_paid_work(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PROXY_LEDGER_FAIL_OPEN=true is the incident escape hatch."""
    _set_fail_open(monkeypatch, True)
    target = _target(_policy(AccessStrategy.PROXY_FIRST))

    decision = _prepare_dispatch(target, 1, {}, {})

    # The plan stays proxied; it only stops later for want of an eligible
    # provider (none configured in this fixture), not for want of a ledger.
    assert decision.skip_error_code is ScrapeErrorCode.PROXY_FAILED


# --- circuit breaker gate ----------------------------------------------------


def test_open_breaker_denies_paid_work_even_with_healthy_redis(
    healthy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The breaker is independent of Redis: a healthy ledger that says
    'allowed' does not override a tripped durable breaker."""
    _set_fail_open(monkeypatch, False)
    monkeypatch.setattr(
        targets_mod,
        "_breaker_allows_paid_work",
        lambda: (False, "circuit breaker OPEN (REQUESTS_PER_URL): 100.00/url"),
    )

    decision = _prepare_dispatch(_target(_policy(AccessStrategy.PROXY_FIRST)), 1, {}, {})

    # 2026-10-05: BREAKER_OPEN, not LIMIT_REACHED. The breaker clears on
    # its own, so the spiders defer on this code instead of failing the
    # target (`TRANSIENT_DISPATCH_SKIP_CODES`).
    assert decision.skip_error_code is ScrapeErrorCode.BREAKER_OPEN
    assert decision.skip_error_code in targets_mod.TRANSIENT_DISPATCH_SKIP_CODES


def test_a_degraded_ledger_still_reports_limit_reached(
    outage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the breaker's denial changed code; a fail-closed ledger outage
    keeps its pre-existing outcome."""
    _set_fail_open(monkeypatch, False)

    decision = _prepare_dispatch(_target(_policy(AccessStrategy.PROXY_FIRST)), 1, {}, {})

    assert decision.skip_error_code is ScrapeErrorCode.LIMIT_REACHED
    assert decision.skip_error_code not in targets_mod.TRANSIENT_DISPATCH_SKIP_CODES


def test_attempt_one_defers_a_breaker_denied_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spider hands a breaker-denied target back DEFERRED with the
    breaker's code at once -- no in-line wait, no terminal row."""
    import asyncio

    from scrape_core.targets import AdmissionContext, _DispatchDecision

    deferred: list[tuple[str, ScrapeErrorCode]] = []

    async def _fake_await_in_thread(fn: object, *args: object, **kwargs: object) -> object:
        return _DispatchDecision(
            plan=None, proxy=None, skip_error_code=ScrapeErrorCode.BREAKER_OPEN
        )

    async def _fake_defer(
        ctx: object, target: object, *, event: str, error_code: ScrapeErrorCode
    ) -> None:
        deferred.append((event, error_code))

    monkeypatch.setattr(targets_mod, "await_in_thread", _fake_await_in_thread)
    monkeypatch.setattr(targets_mod, "defer_rate_limited_target", _fake_defer)
    monkeypatch.setattr("app_shared.config.get_settings", lambda: object())
    ctx = AdmissionContext(
        workspace_id=uuid.uuid4(), scrape_job_id=uuid.uuid4(), requeue_state_by_match_id={}
    )

    decision = asyncio.run(
        targets_mod.prepare_dispatch_with_backoff(
            ctx, _target(_policy(AccessStrategy.PROXY_FIRST)), 1, {}, {}
        )
    )

    assert decision is None
    assert deferred == [("proxy_breaker.defer", ScrapeErrorCode.BREAKER_OPEN)]


def test_open_breaker_does_not_stop_direct_work(
    healthy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tripped spend breaker stops PAID work only — free direct
    scraping keeps the catalog fresh while an operator investigates."""
    _set_fail_open(monkeypatch, False)
    monkeypatch.setattr(
        targets_mod, "_breaker_allows_paid_work", lambda: (False, "circuit breaker OPEN")
    )

    decision = _prepare_dispatch(_target(_policy(AccessStrategy.DIRECT_ONLY)), 1, {}, {})

    assert decision.skip_error_code is None
    assert decision.plan is not None
    assert decision.plan.use_proxy is False


def test_breaker_is_not_consulted_for_a_direct_plan(
    healthy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No database round trip on the free path."""
    calls: list[int] = []

    def _counting() -> tuple[bool, str | None]:
        calls.append(1)
        return True, None

    _set_fail_open(monkeypatch, False)
    monkeypatch.setattr(targets_mod, "_breaker_allows_paid_work", _counting)

    _prepare_dispatch(_target(_policy(AccessStrategy.DIRECT_ONLY)), 1, {}, {})

    assert calls == []


# --- 2026-10-06 (P5 + A9): a breaker trip fans in, not out ----------------
#
# Every BREAKER_OPEN defer used to `enqueue` its own `dispatch_job` (no
# dedup): a trip with ~300 granted targets spawned ~300 dispatchers for the
# same job. Each defer also burned one of the target's
# SCRAPE_MAX_DEFER_CYCLES, so a trip outlasting a few dispatch cycles
# failed targets that had never been allowed to try. BREAKER_OPEN defers
# now go through the outbox under one per-job dedup key with a debounce
# (mirroring the pipeline's `strategy-handoff` path), spend no defer
# cycle, and drop the process's cached breaker verdict.


class _DeferHarness:
    """Records what one or more defers wrote, with the outbox's
    pending-row dedup (`ON CONFLICT (workspace_id, dedup_key) DO NOTHING`
    on PENDING rows) applied to what it records."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from contextlib import contextmanager
        from types import SimpleNamespace

        self.marked: list[tuple[uuid.UUID, object, object]] = []
        self.outbox: dict[tuple[object, str], dict[str, object]] = {}
        self.outbox_writes = 0
        self.enqueued: list[object] = []
        self.redis = _HealthyRedis()
        self.sessions: list[object] = []

        async def _sync_await_in_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        @contextmanager
        def _workspace_txn(_workspace_id):
            session = object()
            self.sessions.append(session)
            yield session

        def _mark_target(session, *, match_id, status, error_code, **_kw):
            self.marked.append((match_id, status, error_code))

        def _write_outbox_message(session, *, workspace_id, task_name, queue, kwargs,
                                  dedup_key=None, available_after_seconds=0, **_kw):
            self.outbox_writes += 1
            assert session is self.sessions[-1], "outbox row outside the mark's txn"
            self.outbox.setdefault(
                (workspace_id, dedup_key),
                {
                    "task_name": task_name,
                    "queue": queue,
                    "kwargs": kwargs,
                    "delay": available_after_seconds,
                },
            )

        monkeypatch.setattr(targets_mod, "await_in_thread", _sync_await_in_thread)
        monkeypatch.setattr(targets_mod, "workspace_txn", _workspace_txn)
        monkeypatch.setattr(targets_mod, "mark_target", _mark_target)
        monkeypatch.setattr(targets_mod, "write_outbox_message", _write_outbox_message)
        monkeypatch.setattr(targets_mod, "enqueue", lambda *a, **kw: self.enqueued.append(kw))
        monkeypatch.setattr(targets_mod, "get_redis_client", lambda: self.redis)
        monkeypatch.setattr(
            "app_shared.config.get_settings",
            lambda: SimpleNamespace(
                SCRAPE_MAX_DEFER_CYCLES=3,
                SCRAPE_MAX_BREAKER_DEFER_CYCLES=60,
                SCRAPE_BREAKER_DEFER_DISPATCH_DEBOUNCE_SECONDS=60,
            ),
        )

    def counters(self, prefix: str) -> list[int]:
        return [v for k, v in self.redis.counters.items() if k.startswith(prefix + ":")]

    def defer(
        self,
        ctx: object,
        error_code: ScrapeErrorCode,
        n: int,
        match_ids: list[uuid.UUID] | None = None,
    ) -> list[uuid.UUID]:
        import asyncio
        from types import SimpleNamespace

        match_ids = match_ids or [uuid.uuid4() for _ in range(n)]

        async def _run() -> None:
            for match_id in match_ids:
                await targets_mod.defer_rate_limited_target(
                    ctx,
                    SimpleNamespace(match_id=match_id),
                    event="proxy_breaker.defer",
                    error_code=error_code,
                )

        asyncio.run(_run())
        return match_ids


def _ctx() -> object:
    from scrape_core.targets import AdmissionContext

    return AdmissionContext(
        workspace_id=uuid.uuid4(), scrape_job_id=uuid.uuid4(), requeue_state_by_match_id={}
    )


def test_300_breaker_deferred_targets_schedule_one_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app_shared.enums import ScrapeTargetStatus
    from app_shared.task_names import SCRAPE_DISPATCH_JOB

    harness = _DeferHarness(monkeypatch)
    ctx = _ctx()

    match_ids = harness.defer(ctx, ScrapeErrorCode.BREAKER_OPEN, 300)

    assert harness.enqueued == []
    assert list(harness.outbox) == [(ctx.workspace_id, f"breaker-defer:{ctx.scrape_job_id}")]
    (row,) = harness.outbox.values()
    assert row == {
        "task_name": SCRAPE_DISPATCH_JOB,
        "queue": "scrape_dispatch",
        "kwargs": {
            "scrape_job_id": str(ctx.scrape_job_id),
            "workspace_id": str(ctx.workspace_id),
        },
        "delay": 60,
    }
    # Every target is still handed back DEFERRED with the breaker's code.
    assert harness.marked == [
        (m, ScrapeTargetStatus.DEFERRED, ScrapeErrorCode.BREAKER_OPEN) for m in match_ids
    ]


def test_a_breaker_defer_spends_no_rate_limit_defer_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It counts on its own, larger budget instead (next test)."""
    harness = _DeferHarness(monkeypatch)
    target = uuid.uuid4()
    harness.defer(_ctx(), ScrapeErrorCode.BREAKER_OPEN, 5, match_ids=[target] * 5)
    assert harness.counters("defercycles") == []
    assert harness.counters("breakerdefercycles") == [5]


def test_a_breaker_defer_loop_is_bounded_by_its_own_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix round 2026-10-06: nothing else bounds it. A target whose cursor
    is persisted is never re-charged by the attempt ladder, and its
    deadline anchor (`started_at`) restarts on every claim, so without
    this a DIRECT-first target escalating to proxy re-deferred every
    ~60 s until the 12 h job deadline. The 61st defer fails it with
    BREAKER_OPEN and schedules nothing."""
    from app_shared.enums import ScrapeTargetStatus

    harness = _DeferHarness(monkeypatch)
    ctx = _ctx()
    target = uuid.uuid4()
    harness.defer(ctx, ScrapeErrorCode.BREAKER_OPEN, 61, match_ids=[target] * 61)

    statuses = [status for _m, status, _c in harness.marked]
    assert statuses == [ScrapeTargetStatus.DEFERRED] * 60 + [ScrapeTargetStatus.FAILED]
    assert harness.marked[-1][2] is ScrapeErrorCode.BREAKER_OPEN
    assert harness.outbox_writes == 60


def test_a_breaker_defer_drops_the_cached_breaker_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next gate read goes to the durable row, so a breaker that has
    since closed is seen at once rather than up to 30 s later."""
    from app_shared.access import breaker

    harness = _DeferHarness(monkeypatch)
    breaker._gate_cache = breaker._GateCache(allowed=False, reason="OPEN", fetched_at=0.0)
    try:
        harness.defer(_ctx(), ScrapeErrorCode.BREAKER_OPEN, 1)
        assert breaker._gate_cache is None
    finally:
        breaker.reset_gate_cache()


def test_a_rate_limit_defer_keeps_its_budget_and_direct_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only BREAKER_OPEN changed: a rate-limit defer still spends a cycle
    and still re-enqueues directly."""
    harness = _DeferHarness(monkeypatch)
    harness.defer(_ctx(), ScrapeErrorCode.RATE_LIMITED, 2)
    assert harness.counters("defercycles") == [1, 1]
    assert len(harness.enqueued) == 2
    assert harness.outbox == {}


def test_the_retry_path_breaker_redispatch_also_fans_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The errback path marks DEFERRED through the pipeline and then calls
    `redispatch_job`; for BREAKER_OPEN that re-dispatch is the same one
    debounced outbox row per job, not one enqueue per target (P5)."""
    import asyncio
    from types import SimpleNamespace

    harness = _DeferHarness(monkeypatch)
    ctx = _ctx()

    async def _run() -> None:
        for _ in range(300):
            await targets_mod.redispatch_job(
                ctx,
                SimpleNamespace(match_id=uuid.uuid4()),
                error_code=ScrapeErrorCode.BREAKER_OPEN,
            )

    asyncio.run(_run())

    assert harness.enqueued == []
    assert list(harness.outbox) == [(ctx.workspace_id, f"breaker-defer:{ctx.scrape_job_id}")]
    assert harness.marked == []  # the pipeline owns the mark on this path


def test_the_spider_tells_redispatch_why() -> None:
    import inspect
    from pathlib import Path

    source = Path(
        inspect.getfile(targets_mod)
    ).parents[3].joinpath(
        "apps/scrapers/price_monitor/spiders/generic_price_spider.py"
    ).read_text()
    assert "target, error_code=decision.skip_error_code" in source
