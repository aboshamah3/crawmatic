"""Fleet admission at the PHYSICAL request boundary (EPA B5, F10).

`tests/unit/test_fleet_admission.py` covers the Redis primitives. This
file covers the seam: that the fleet lease is actually taken where the
outbound request is about to happen, released on BOTH the success and the
error path, and that a refusal defers the target through the existing
``_mark_target_deferred_rate_limited`` path with the new
``FLEET_LIMITED`` verdict rather than failing it or inventing a new one.

The boundary is ``scrape_core.limiter.acquire_permission`` (the
reactor-safe seam) called from ``scrape_core.targets``'s shared admission
machinery — NOT a Scrapy downloader middleware. The plan text names
`libs/scrape-core/scrape_core/middlewares/limiter.py`; no such module
exists (and creating one would fork admission away from the browser
spider, which shares `scrape_core.targets`). `targets.acquire_fetch_permission`
is the one place every transport passes through before a request is
built, so that is where the lease belongs, and this file drives it there.

Runs with no infra, following this suite's established conventions
(`tests/unit/test_observability_logs.py`): coroutines are driven directly
via ``asyncio.run``, ``await_in_thread`` is replaced by a synchronous
pass-through, and every Redis/DB/Celery touchpoint is a pure fake.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from twisted.internet.defer import Deferred

from app_shared.enums import AccessMethod, RobotsPolicy, ScrapeErrorCode
from app_shared.limiter.fleet import FleetLimits, FleetTransport, fleet_semaphore_key

from scrape_core import limiter as limiter_mod
from scrape_core import targets as targets_mod
from scrape_core.limiter import Permission

DOMAIN = "amazon.sa"


# --- doubles -----------------------------------------------------------------


class _FakeSettings:
    RATE_LIMIT_DEFAULT_PER_MINUTE = 60
    RATE_LIMIT_DEFAULT_CONCURRENCY = 4
    RATE_LIMIT_KEY_TTL_SLACK_SECONDS = 120
    SEMAPHORE_SLOT_TTL_SECONDS = 600
    MATCH_LOCK_HTTP_TTL_SECONDS = 600
    MATCH_LOCK_BROWSER_TTL_SECONDS = 1800
    # Zero requeue budget: the FIRST denial overflows, so the deferral
    # path is exercised deterministically instead of after five backoffs.
    REQUEUE_MAX_ATTEMPTS = 0
    REQUEUE_MAX_TOTAL_WAIT_SECONDS = 300
    RATE_LIMIT_JITTER_MIN_SECONDS = 0
    RATE_LIMIT_JITTER_MAX_SECONDS = 0
    SCRAPE_MAX_DEFER_CYCLES = 3
    FLEET_HOST_CONCURRENCY_DEFAULT = 6
    FLEET_HOST_RATE_PER_MINUTE_DEFAULT = 90
    FLEET_LEASE_TTL_SECONDS = 120


def _instant_delay(seconds: float) -> Deferred:
    d: Deferred = Deferred()
    d.callback(None)
    return d


def _target(domain: str = DOMAIN) -> targets_mod.SpiderTarget:
    return targets_mod.SpiderTarget(
        match_id=uuid.uuid4(),
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


def _ctx(target: targets_mod.SpiderTarget) -> targets_mod.AdmissionContext:
    return targets_mod.AdmissionContext(
        workspace_id=uuid.uuid4(),
        scrape_job_id=uuid.uuid4(),
        requeue_state_by_match_id={target.match_id: targets_mod._RequeueState()},
    )


async def _passthrough_in_thread(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Stand-in for ``scrape_core.db.await_in_thread``: runs ``fn`` inline
    rather than on Twisted's thread pool, so no reactor is needed."""
    return fn(*args, **kwargs)


@pytest.fixture
def marks(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every DB/Celery side effect the deferral path performs."""
    recorded: list[dict[str, Any]] = []

    def _mark(workspace_id: Any, scrape_job_id: Any, match_id: Any, error_code: Any = None) -> None:
        recorded.append(
            {"call": "mark_deferred", "match_id": match_id, "error_code": error_code}
        )

    def _enqueue(*args: Any, **kwargs: Any) -> None:
        recorded.append({"call": "enqueue"})

    monkeypatch.setattr(targets_mod, "_mark_target_deferred_rate_limited", _mark)
    monkeypatch.setattr(targets_mod, "enqueue", _enqueue)
    monkeypatch.setattr(targets_mod, "consume_defer_budget", lambda *a, **k: True)
    monkeypatch.setattr(targets_mod, "await_in_thread", _passthrough_in_thread)
    monkeypatch.setattr(targets_mod, "deferred_delay", _instant_delay)
    monkeypatch.setattr(targets_mod, "get_redis_client", lambda: object())
    monkeypatch.setattr("app_shared.config.get_settings", lambda: _FakeSettings())
    return recorded


# --- the seam: acquire_permission takes the fleet lease last -----------------


class _RecordingRedis:
    """Records the acquire/release calls `acquire_permission` makes, so the
    ORDER and the release-on-refusal behaviour are both observable."""

    def __init__(self, *, fleet_granted: bool = True) -> None:
        self.fleet_granted = fleet_granted
        self.calls: list[tuple[str, str]] = []


@pytest.fixture
def stubbed_primitives(monkeypatch: pytest.MonkeyPatch) -> _RecordingRedis:
    redis = _RecordingRedis()

    class _Result:
        granted = True
        wait_hint_seconds = 0

    def _acquire_token(_client: Any, *, key: str, **_kw: Any) -> Any:
        redis.calls.append(("acquire_token", key))
        return _Result()

    def _acquire_slot(_client: Any, *, key: str, **_kw: Any) -> bool:
        redis.calls.append(("acquire_slot", key))
        return True

    def _release_slot(_client: Any, *, key: str, **_kw: Any) -> None:
        redis.calls.append(("release_slot", key))

    def _admit_fleet(_client: Any, *, domain: str, transport: Any, **_kw: Any) -> Any:
        redis.calls.append(("admit_fleet", domain))
        if not redis.fleet_granted:
            return None
        from app_shared.limiter.fleet import FleetLease

        return FleetLease(
            semaphore_key=fleet_semaphore_key(domain, transport),
            token="fleet-token",
            domain=domain,
            transport=transport,
            lease_ttl=120,
        )

    monkeypatch.setattr(limiter_mod, "await_in_thread", _passthrough_in_thread)
    monkeypatch.setattr(limiter_mod._bucket, "acquire_token", _acquire_token)
    monkeypatch.setattr(limiter_mod._bucket, "acquire_slot", _acquire_slot)
    monkeypatch.setattr(limiter_mod._bucket, "release_slot", _release_slot)
    monkeypatch.setattr(limiter_mod._fleet, "admit_fleet", _admit_fleet)
    return redis


def _acquire(redis: _RecordingRedis, **overrides: Any) -> Permission:
    kwargs: dict[str, Any] = dict(
        workspace_id=uuid.uuid4(),
        domain=DOMAIN,
        access_method=AccessMethod.DIRECT_HTTP,
        limits=type("L", (), {"per_minute": 60, "concurrency": 4})(),
        settings=_FakeSettings(),
        sem_token="tenant-token",
        fleet_limits=FleetLimits(concurrency=6, rate_per_minute=90),
    )
    kwargs.update(overrides)
    return asyncio.run(limiter_mod.acquire_permission(redis, **kwargs))


def test_a_granted_permission_carries_both_the_tenant_slot_and_the_fleet_lease(
    stubbed_primitives: _RecordingRedis,
) -> None:
    perm = _acquire(stubbed_primitives)

    assert perm.granted
    assert perm.semaphore_key and perm.semaphore_token == "tenant-token"
    assert perm.fleet_key == fleet_semaphore_key(DOMAIN, FleetTransport.HTTP)
    assert perm.fleet_token == "fleet-token"
    # Tenant gates first, fleet last -- the cheap per-workspace fairness
    # checks must not queue behind a ceiling every workspace shares.
    assert [name for name, _ in stubbed_primitives.calls] == [
        "acquire_token",
        "acquire_slot",
        "admit_fleet",
    ]


def test_a_fleet_refusal_hands_the_tenant_slot_straight_back(
    stubbed_primitives: _RecordingRedis,
) -> None:
    """Otherwise a request that will never be sent parks a tenant slot for
    SEMAPHORE_SLOT_TTL_SECONDS."""
    stubbed_primitives.fleet_granted = False

    perm = _acquire(stubbed_primitives)

    assert perm.granted is False
    assert perm.denied_by == "fleet"
    assert perm.wait_hint_seconds > 0
    # A denied permission carries no lease to release (the fleet gate
    # returned before it was granted one).
    assert perm.fleet_key is None and perm.fleet_token is None
    released = [key for name, key in stubbed_primitives.calls if name == "release_slot"]
    assert len(released) == 1, "exactly the tenant slot, released exactly once"
    assert released[0].startswith("semaphore:"), "the TENANT semaphore, not a fleet key"


def test_omitting_fleet_limits_reproduces_the_pre_b5_behaviour(
    stubbed_primitives: _RecordingRedis,
) -> None:
    """Every caller that has not been taught about fleet admission (and
    every older test) must be byte-for-byte unaffected."""
    perm = _acquire(stubbed_primitives, fleet_limits=None)

    assert perm.granted
    assert perm.fleet_key is None and perm.fleet_token is None
    assert "admit_fleet" not in [name for name, _ in stubbed_primitives.calls]


def test_a_browser_access_method_leases_the_browser_fleet_key(
    stubbed_primitives: _RecordingRedis,
) -> None:
    perm = _acquire(stubbed_primitives, access_method=AccessMethod.PLAYWRIGHT_PROXY)
    assert perm.fleet_key == fleet_semaphore_key(DOMAIN, FleetTransport.BROWSER)


# --- refusal defers through the EXISTING path, recorded as FLEET_LIMITED ----


def test_a_refused_fleet_lease_defers_the_target_as_fleet_limited(
    monkeypatch: pytest.MonkeyPatch, marks: list[dict[str, Any]]
) -> None:
    target = _target()
    ctx = _ctx(target)

    async def _denied(*_a: Any, **_kw: Any) -> Permission:
        return Permission(granted=False, wait_hint_seconds=2.0, denied_by="fleet")

    monkeypatch.setattr(targets_mod, "acquire_permission", _denied)

    perm = asyncio.run(
        targets_mod.acquire_fetch_permission(ctx, target, AccessMethod.DIRECT_HTTP)
    )

    # No request is dispatched for this attempt...
    assert perm is None
    # ...and the target went DEFERRED through the SAME
    # `_mark_target_deferred_rate_limited` writer, plus the same
    # `scrape_dispatch` re-enqueue, that a tenant rate-limit overflow uses.
    assert [m["call"] for m in marks] == ["mark_deferred", "enqueue"]
    # The verdict, however, is the new one: admission pressure, recorded
    # separately from host blocking so C1/D5 can tell them apart.
    assert marks[0]["error_code"] is ScrapeErrorCode.FLEET_LIMITED


def test_a_tenant_rate_limit_overflow_still_records_rate_limited(
    monkeypatch: pytest.MonkeyPatch, marks: list[dict[str, Any]]
) -> None:
    """The regression guard for the line above: FLEET_LIMITED must not
    swallow the pre-existing verdict."""
    target = _target()
    ctx = _ctx(target)

    async def _denied(*_a: Any, **_kw: Any) -> Permission:
        return Permission(granted=False, wait_hint_seconds=5, denied_by="bucket")

    monkeypatch.setattr(targets_mod, "acquire_permission", _denied)

    assert (
        asyncio.run(targets_mod.acquire_fetch_permission(ctx, target, AccessMethod.DIRECT_HTTP))
        is None
    )
    assert marks[0]["error_code"] is ScrapeErrorCode.RATE_LIMITED


def test_the_fleet_denial_emits_its_own_event(
    monkeypatch: pytest.MonkeyPatch, marks: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    import json
    import logging

    target = _target()
    ctx = _ctx(target)

    async def _denied(*_a: Any, **_kw: Any) -> Permission:
        return Permission(granted=False, wait_hint_seconds=2.0, denied_by="fleet")

    monkeypatch.setattr(targets_mod, "acquire_permission", _denied)
    caplog.set_level(logging.INFO)

    asyncio.run(targets_mod.acquire_fetch_permission(ctx, target, AccessMethod.DIRECT_HTTP))

    events = []
    for record in caplog.records:
        if record.name != "scrape_core.targets":
            continue
        try:
            payload = json.loads(record.getMessage())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            events.append(payload)

    denied = next(e for e in events if e.get("event") == "fleet.denied")
    assert denied["domain"] == DOMAIN
    assert denied["fleet_concurrency"] == _FakeSettings.FLEET_HOST_CONCURRENCY_DEFAULT
    assert denied["fleet_rate_per_minute"] == _FakeSettings.FLEET_HOST_RATE_PER_MINUTE_DEFAULT
    # `rate_limit.hit` asserts THIS TENANT hit its own ceiling -- a fleet
    # refusal must never be reported as that.
    assert not any(e.get("event") == "rate_limit.hit" for e in events)


# --- release on BOTH the success and the error path -------------------------


@pytest.mark.parametrize(
    "spider_module",
    [
        "price_monitor.spiders.generic_price_spider",
        "price_monitor_browser.spiders.generic_browser_price_spider",
    ],
)
def test_both_spiders_thread_and_release_the_lease_on_success_and_error(
    spider_module: str,
) -> None:
    """Structural check over the real spider source: each transport must
    stamp the lease onto ``meta`` at request build time and release it in
    BOTH ``parse`` (success) and ``errback`` (error) -- a lease released
    on only one path leaks the fleet's whole ceiling to whichever
    outcome was forgotten.

    The browser path stamps its lease before the request is yielded, i.e.
    before scrapy-playwright opens the page and calls ``page.goto``, and
    releases it once the page has closed and the response reached
    ``parse``; child resources ride that one lease by construction (they
    are fetched inside the navigation, never as separate Scrapy requests).
    """
    import importlib
    import inspect

    module = importlib.import_module(spider_module)
    source = inspect.getsource(module)

    assert 'meta["fleet_key"] = permission.fleet_key' in source
    assert 'meta["fleet_token"] = permission.fleet_token' in source
    # Success path (`parse` reads `response.meta`) and error path
    # (`errback` reads `failure.request.meta`) -- one release each.
    assert source.count('.meta.get("fleet_key")') == 2
    assert source.count("await release_fleet_lease(get_redis_client()") == 2


def test_dispatch_admission_releases_the_lease_when_the_match_lock_is_held(
    monkeypatch: pytest.MonkeyPatch, marks: list[dict[str, Any]]
) -> None:
    """No fetch will happen on this path, so the fleet slot must go back
    immediately rather than waiting out FLEET_LEASE_TTL_SECONDS and
    throttling every OTHER workspace on this domain."""
    target = _target()
    ctx = _ctx(target)
    released: list[tuple[str, str]] = []

    granted = Permission(
        granted=True,
        wait_hint_seconds=0,
        semaphore_key="semaphore:ws:amazon.sa:DIRECT_HTTP",
        semaphore_token="tenant-token",
        fleet_key=fleet_semaphore_key(DOMAIN, FleetTransport.HTTP),
        fleet_token="fleet-token",
    )

    async def _permission(*_a: Any, **_kw: Any) -> Permission:
        return granted

    async def _no_lock(*_a: Any, **_kw: Any) -> None:
        return None

    async def _release_slot(_redis: Any, *, key: str, token: str) -> None:
        released.append(("slot", key))

    async def _release_fleet(_redis: Any, *, key: str, token: str) -> None:
        released.append(("fleet", key))

    monkeypatch.setattr(targets_mod, "acquire_fetch_permission", _permission)
    monkeypatch.setattr(targets_mod, "acquire_lock", _no_lock)
    monkeypatch.setattr(targets_mod, "release_slot", _release_slot)
    monkeypatch.setattr(targets_mod, "release_fleet_lease", _release_fleet)

    result = asyncio.run(
        targets_mod.dispatch_admission(
            ctx,
            target,
            1,
            type("Plan", (), {"access_method": AccessMethod.DIRECT_HTTP})(),
            None,
            build_request=lambda *a, **k: pytest.fail("no request may be built"),
        )
    )

    assert result.error_code is ScrapeErrorCode.LOCKED_ALREADY_RUNNING
    assert [kind for kind, _ in released] == ["slot", "fleet"]
    assert released[1][1] == granted.fleet_key
