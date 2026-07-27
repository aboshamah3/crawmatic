"""Unit tests for the 2026-07 scrape-escalation fixes
(`ISSUES_FULL_RUN_2026-07-17.md` Issues 3/4/5):

* Issue 4 -- the resolved access policy's ``timeout_ms`` reaches
  ``request.meta["download_timeout"]`` and Scrapy's own RetryMiddleware
  is disabled (``dont_retry``) so the access engine owns retries.
* Issue 3 -- ``dispatch_admission(reuse_lock=...)`` skips the fresh
  ``acquire_lock`` (which used to collide with the same target's own
  prior attempt and kill the DIRECT->PROXY escalation), and ``errback``
  strips the lock off the failed attempt's result + threads it into the
  retry dispatch.
* Issue 5 -- ``sticky_proxy_username`` appends the DataImpulse
  ``;sessid.<id>`` suffix for sticky assignments and leaves other
  providers untouched.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app_shared.access.engine import AttemptPlan
from app_shared.enums import AccessMethod, RobotsPolicy

import scrape_core.targets as targets_mod
from scrape_core.limiter import LockGrant, Permission
from scrape_core.targets import AdmissionContext, dispatch_admission, sticky_proxy_username

from price_monitor.spiders import generic_price_spider as gps


@pytest.fixture()
def spider() -> gps.GenericPriceSpider:
    return gps.GenericPriceSpider(
        workspace_id=str(uuid.uuid4()),
        match_ids=str(uuid.uuid4()),
    )


def _target(access_policy: Any = None) -> gps.SpiderTarget:
    return gps.SpiderTarget(
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        url="https://shop.example.com/product/1",
        profile=None,
        robots_policy=RobotsPolicy.RESPECT,
        access_policy=access_policy,
    )


# ---------------------------------------------------------------- Issue 4


def test_request_carries_policy_download_timeout(spider: gps.GenericPriceSpider) -> None:
    policy = SimpleNamespace(timeout_ms=30000)
    request = spider._request_for(_target(access_policy=policy))
    assert request.meta["download_timeout"] == 30.0


def test_request_without_policy_has_no_download_timeout(spider: gps.GenericPriceSpider) -> None:
    request = spider._request_for(_target())
    assert "download_timeout" not in request.meta


def test_request_disables_scrapy_retry_middleware(spider: gps.GenericPriceSpider) -> None:
    request = spider._request_for(_target())
    assert request.meta["dont_retry"] is True


# ---------------------------------------------------------------- Issue 5


def test_sticky_username_appends_dataimpulse_sessid() -> None:
    out = sticky_proxy_username("user__cr.sa", "http://gw.dataimpulse.com:823", "abc:def")
    assert out.startswith("user__cr.sa;sessid.")
    session_id = out.split(";sessid.")[1]
    assert session_id.isalnum() and len(session_id) == 16
    # Stable: same key -> same session id (sticky_session semantics).
    assert out == sticky_proxy_username("user__cr.sa", "http://gw.dataimpulse.com:823", "abc:def")
    # A different key rotates the id (rotate_per_request semantics).
    assert out != sticky_proxy_username("user__cr.sa", "http://gw.dataimpulse.com:823", "abc:def:2")


def test_sticky_username_untouched_without_key_or_other_provider() -> None:
    assert sticky_proxy_username("u", "http://gw.dataimpulse.com:823", None) == "u"
    assert sticky_proxy_username("u", "http://proxy.other.com:8080", "key") == "u"


def test_request_proxy_auth_uses_sticky_username(spider: gps.GenericPriceSpider) -> None:
    import base64

    provider_id = uuid.uuid4()
    spider._provider_rows = {
        provider_id: SimpleNamespace(base_url="http://gw.dataimpulse.com:823", username="user__cr.sa")
    }
    spider._provider_passwords = {provider_id: "pw"}
    assignment = SimpleNamespace(provider_id=provider_id, country="sa", sticky_key="seed:1")

    request = spider._request_for(
        _target(),
        1,
        AttemptPlan(access_method=AccessMethod.PROXY_HTTP, use_proxy=True),
        assignment,
    )

    header = request.headers[b"Proxy-Authorization"].decode()
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    assert ";sessid." in decoded.split(":", 1)[0]


# ---------------------------------------------------------------- Issue 3


def test_dispatch_admission_reuses_inherited_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """With `reuse_lock`, no fresh acquire_lock runs (it would collide with
    the caller's own still-held lock) and the inherited grant reaches
    `build_request`."""
    granted = Permission(granted=True, wait_hint_seconds=0, semaphore_key="sk", semaphore_token="st")

    async def fake_perm(ctx: Any, target: Any, method: Any) -> Permission:
        return granted

    async def exploding_acquire_lock(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("acquire_lock must not run when reuse_lock is given")

    monkeypatch.setattr(targets_mod, "acquire_fetch_permission", fake_perm)
    monkeypatch.setattr(targets_mod, "acquire_lock", exploding_acquire_lock)
    monkeypatch.setattr(targets_mod, "get_redis_client", lambda: object())

    ctx = AdmissionContext(
        workspace_id=uuid.uuid4(), scrape_job_id=None, requeue_state_by_match_id={}
    )
    target = _target()
    inherited = LockGrant(key="lk", token="lt")
    seen: dict[str, Any] = {}

    def build_request(t: Any, n: int, plan: Any, proxy: Any, perm: Any, lock: Any) -> str:
        seen["lock"] = lock
        seen["perm"] = perm
        return "REQUEST"

    result = asyncio.run(
        dispatch_admission(
            ctx,
            target,
            2,
            AttemptPlan(access_method=AccessMethod.PROXY_HTTP, use_proxy=True),
            None,
            build_request=build_request,
            reuse_lock=inherited,
        )
    )

    assert result == "REQUEST"
    assert seen["lock"] is inherited
    assert seen["perm"] is granted


def _collect(agen: Any) -> list[Any]:
    async def run() -> list[Any]:
        return [item async for item in agen]

    return asyncio.run(run())


def test_errback_strips_lock_from_failed_result_and_reuses_it(
    spider: gps.GenericPriceSpider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retrying errback must (a) NOT stamp the held lock onto the failed
    attempt's result (the pipeline would release it under the retry) and
    (b) hand that lock to `_dispatch` as `reuse_lock`."""
    import scrapy

    target = _target()
    spider._targets_by_match_id[target.match_id] = target

    request = scrapy.Request(
        url=target.url,
        meta={
            "match_id": target.match_id,
            "attempt_number": 1,
            "access_method": AccessMethod.DIRECT_HTTP,
            "match_lock_key": "lk",
            "match_lock_token": "lt",
        },
    )
    failure = SimpleNamespace(request=request, value=TimeoutError("boom"))

    decision = targets_mod._DispatchDecision(
        plan=AttemptPlan(access_method=AccessMethod.PROXY_HTTP, use_proxy=True), proxy=None
    )

    async def fake_run_in_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return decision

    dispatched: dict[str, Any] = {}

    async def fake_dispatch(
        t: Any, n: int, plan: Any, proxy: Any, reuse_lock: Any = None
    ) -> str:
        dispatched["reuse_lock"] = reuse_lock
        dispatched["attempt"] = n
        return "RETRY_REQUEST"

    monkeypatch.setattr(gps, "await_in_thread", fake_run_in_thread)
    monkeypatch.setattr(spider, "_dispatch", fake_dispatch)

    results = _collect(spider.errback(failure))

    failed = results[0]
    assert failed.match_lock_key is None
    assert failed.match_lock_token is None
    assert results[1] == "RETRY_REQUEST"
    assert dispatched["reuse_lock"] == LockGrant(key="lk", token="lt")
    assert dispatched["attempt"] == 2


def test_errback_keeps_lock_on_result_when_no_retry(
    spider: gps.GenericPriceSpider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No retry -> the failed result keeps the lock key/token so the
    persistence pipeline releases it (pre-fix behavior preserved)."""
    import scrapy

    target = _target()
    spider._targets_by_match_id[target.match_id] = target

    request = scrapy.Request(
        url=target.url,
        meta={
            "match_id": target.match_id,
            "attempt_number": 1,
            "access_method": AccessMethod.DIRECT_HTTP,
            "match_lock_key": "lk",
            "match_lock_token": "lt",
        },
    )
    failure = SimpleNamespace(request=request, value=TimeoutError("boom"))

    stop = targets_mod._DispatchDecision(plan=None, proxy=None, skip_error_code=None)

    async def fake_run_in_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return stop

    monkeypatch.setattr(gps, "await_in_thread", fake_run_in_thread)

    results = _collect(spider.errback(failure))

    assert len(results) == 1
    assert results[0].match_lock_key == "lk"
    assert results[0].match_lock_token == "lt"


def test_errback_releases_inherited_lock_on_overflow(
    spider: gps.GenericPriceSpider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry decided but `_dispatch` overflowed (returns None): the
    inherited lock's normal release path (the retry's own result row)
    never happens, so errback must release it explicitly."""
    import scrapy

    target = _target()
    spider._targets_by_match_id[target.match_id] = target

    request = scrapy.Request(
        url=target.url,
        meta={
            "match_id": target.match_id,
            "attempt_number": 1,
            "access_method": AccessMethod.DIRECT_HTTP,
            "match_lock_key": "lk",
            "match_lock_token": "lt",
        },
    )
    failure = SimpleNamespace(request=request, value=TimeoutError("boom"))

    decision = targets_mod._DispatchDecision(
        plan=AttemptPlan(access_method=AccessMethod.PROXY_HTTP, use_proxy=True), proxy=None
    )

    async def fake_run_in_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return decision

    async def fake_dispatch(*args: Any, **kwargs: Any) -> None:
        return None

    released: dict[str, Any] = {}

    async def fake_release_lock(redis: Any, *, key: str, token: str) -> None:
        released["key"] = key
        released["token"] = token

    monkeypatch.setattr(gps, "await_in_thread", fake_run_in_thread)
    monkeypatch.setattr(spider, "_dispatch", fake_dispatch)
    monkeypatch.setattr(gps, "release_lock", fake_release_lock)
    monkeypatch.setattr(gps, "get_redis_client", lambda: object())

    results = _collect(spider.errback(failure))

    assert len(results) == 1  # only the failed attempt's own result
    assert released == {"key": "lk", "token": "lt"}
