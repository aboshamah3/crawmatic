"""Live SPEC-10 US2/US3 spider access-policy + attempt-logging integration
test (T032, T035, `contracts/spider-integration.md` Acceptance) —
DEFERRED.

Per the task brief: reproducing the full
``run_generic_price_spider_subprocess`` machinery (a real Scrapy crawl
against a loopback fixture server) for every one of these scenarios
would be heavy for this pass, so this file exercises
``generic_price_spider.load_targets``/``_prepare_dispatch``/
``_request_for``/``_build_result``/``_attempt_kwargs_from_meta``
**directly** against a live Postgres (for the access resolution +
provider/decrypt bounded loads, and to persist/query real
``request_attempts`` rows) and Redis (for the ceiling/cooldown/budget
gates) — still skip-clean on the same ``live_stack_reachable`` probe the
sibling ``test_spider_*_live.py`` files use, still asserting the genuine
acceptance behaviors:

1. ``DIRECT_THEN_PROXY`` + ``max_retries>=1``: attempt 1 is plain
   direct, attempt 2 (simulating a failed attempt 1) is ``PROXY_HTTP``
   with the provider/country set (US2-1).
2. ``DIRECT_ONLY`` never proxies any attempt (US2-2, SC-001).
3. A disabled/missing referenced provider degrades (``PROXY_FAILED``),
   never raises (Edge Case).
4. A policy whose per-minute ceiling is exceeded defers/skips with
   ``RATE_LIMITED`` — no dispatch (FR-011, US2-4).
5. An exhausted proxy budget reroutes (``DIRECT_THEN_PROXY``) or records
   ``LIMIT_REACHED`` (a strategy with no non-proxy fallback) (US2-5).
6. The decrypted proxy password never appears in captured logs.
7. (SPEC-10 US3, T035) A direct attempt 1 + a proxied retry (attempt 2)
   each persist their **own** ``RequestAttempt`` row (attempt_number 1
   and 2, the retry carrying the proxy provider/country) via the
   unchanged ``BatchedPersistencePipeline`` core (``_flush_batch``) —
   US3-1/SC-002.
8. A never-dispatched, budget/proxy-gated skip still persists its own
   ``PROXY_FAILED`` row (the pure classification coverage for
   ``BLOCKED``/``TIMEOUT``/``PROXY_FAILED`` itself lives in
   ``tests/unit/test_errors.py``, needing no live stack) — US3-2.
9. Exactly one row per attempt, each in the correct monthly partition
   (``request_attempts_<created_at YYYY_MM>``) — US3-3, SC-002.
10. A workspace-scoped query sees only its own persisted attempt rows;
    a no-``app.workspace_id``-context query sees zero — US3-4, SC-005.

Needs a reachable Postgres (``DATABASE_URL``) with the SPEC-10 migration
applied AND a reachable Redis (``REDIS_URL``) — reuses
``_scrapyd_spider_live_support.live_stack_reachable``. Not runnable in
the no-Docker-daemon build environment used to author this feature —
SKIPS cleanly whenever either isn't usable or the required tables don't
exist.

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis host with
the SPEC-10 migration applied).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ._scrapyd_spider_live_support import (
    SeededWorkspace,
    cleanup_seeded_workspace,
    live_stack_reachable,
    seed_competitor,
    seed_match,
    seed_workspace_with_variant,
)

_REQUIRED_TABLES = (
    "competitor_product_matches",
    "competitors",
    "proxy_providers",
    "access_policies",
    "domain_access_rules",
    "request_attempts",
)

pytestmark = pytest.mark.skipif(
    not live_stack_reachable(_REQUIRED_TABLES),
    reason="No reachable Postgres (with the SPEC-10 migration applied) + Redis in this environment",
)


def _create_access_policy(workspace_id: uuid.UUID, *, name: str = "default", **kwargs: object):
    from app_shared.database import get_session
    from app_shared.models.access import AccessPolicy

    with get_session() as session:
        policy = AccessPolicy(workspace_id=workspace_id, name=name, **kwargs)
        session.add(policy)
        session.commit()
        session.refresh(policy)
        return policy.id


def _create_proxy_provider(workspace_id: uuid.UUID, *, name: str | None = None, **kwargs: object):
    from app_shared.database import get_session
    from app_shared.models.access import ProxyProvider
    from app_shared.security.encryption import encrypt_secret

    unique = uuid.uuid4().hex[:8]
    plaintext_password = kwargs.pop("_password", None)
    if plaintext_password is not None:
        encrypted = encrypt_secret(str(plaintext_password))
        kwargs["password_encrypted"] = encrypted.ciphertext
        kwargs["password_key_version"] = encrypted.key_version

    with get_session() as session:
        provider = ProxyProvider(
            workspace_id=workspace_id,
            name=name or f"provider-{unique}",
            base_url=kwargs.pop("base_url", "http://proxy.example.invalid:8080"),
            **kwargs,
        )
        session.add(provider)
        session.commit()
        session.refresh(provider)
        return provider.id


def _cleanup_access_rows(workspace_id: uuid.UUID) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        session.execute(text("DELETE FROM domain_access_rules WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM access_policies WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM proxy_providers WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.commit()


def _cleanup_request_attempts(workspace_id: uuid.UUID) -> None:
    """SPEC-10 US3 (T035) teardown for the real ``request_attempts`` rows
    the attempt-logging tests persist via ``_flush_batch`` (the unchanged
    ``BatchedPersistencePipeline`` core)."""
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        session.execute(text("DELETE FROM request_attempts WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.commit()


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    ws = seed_workspace_with_variant("spider-access")
    yield ws
    _cleanup_request_attempts(ws.workspace_id)
    _cleanup_access_rows(ws.workspace_id)
    cleanup_seeded_workspace(ws)


def _seed_target(seeded: SeededWorkspace, *, url: str | None = None) -> uuid.UUID:
    competitor_id = seed_competitor(seeded, "access-target")
    unique = uuid.uuid4().hex[:8]
    return seed_match(seeded, competitor_id, url or f"https://access-target.invalid/p/{unique}")


def _fetch_request_attempts(workspace_id: uuid.UUID, match_id: uuid.UUID) -> list[dict[str, object]]:
    """Query the real, unchanged ``request_attempts`` table for `match_id`'s
    persisted rows (SPEC-10 US3, T035), ordered by ``attempt_number``.

    ``tableoid::regclass::text`` resolves to the concrete monthly
    partition a row physically landed in (e.g. ``request_attempts_2026_07``)
    -- the table itself is only the partitioned parent, never queried by
    name directly by Postgres for storage.
    """
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        rows = session.execute(
            text(
                "SELECT attempt_number, access_method, proxy_provider_id, proxy_country, "
                "status_code, response_time_ms, success, error_code, created_at, "
                "tableoid::regclass::text AS partition_name "
                "FROM request_attempts WHERE workspace_id = :ws AND match_id = :match_id "
                "ORDER BY attempt_number"
            ),
            {"ws": workspace_id, "match_id": match_id},
        ).mappings().all()
        return [dict(row) for row in rows]


# --- US2-1: DIRECT_THEN_PROXY retries via PROXY_HTTP ------------------------


def test_direct_then_proxy_retries_via_proxy_http_with_country(seeded: SeededWorkspace) -> None:
    from price_monitor.spiders.generic_price_spider import _prepare_dispatch, load_targets

    from app_shared.enums import AccessMethod, AccessStrategy

    provider_id = _create_proxy_provider(seeded.workspace_id, type="DATACENTER", country_code="US")
    _create_access_policy(
        seeded.workspace_id,
        strategy=AccessStrategy.DIRECT_THEN_PROXY,
        provider_id=provider_id,
        max_retries=1,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
    )
    match_id = _seed_target(seeded)

    loaded = load_targets(seeded.workspace_id, [match_id])
    assert len(loaded.targets) == 1
    target = loaded.targets[0]
    assert target.access_policy is not None
    assert target.access_policy.strategy == AccessStrategy.DIRECT_THEN_PROXY

    first = _prepare_dispatch(target, 1, loaded.visible_providers, loaded.provider_rows)
    assert first.plan is not None
    assert first.plan.access_method == AccessMethod.DIRECT_HTTP
    assert first.plan.use_proxy is False
    assert first.proxy is None

    second = _prepare_dispatch(target, 2, loaded.visible_providers, loaded.provider_rows)
    assert second.plan is not None
    assert second.plan.access_method == AccessMethod.PROXY_HTTP
    assert second.plan.use_proxy is True
    assert second.proxy is not None
    assert second.proxy.provider_id == provider_id
    assert second.proxy.country == "US"


# --- US2-2 / SC-001: DIRECT_ONLY never proxies ------------------------------


def test_direct_only_never_proxies_any_attempt(seeded: SeededWorkspace) -> None:
    from price_monitor.spiders.generic_price_spider import _prepare_dispatch, load_targets

    from app_shared.enums import AccessStrategy

    _create_proxy_provider(seeded.workspace_id, type="DATACENTER")
    _create_access_policy(
        seeded.workspace_id,
        strategy=AccessStrategy.DIRECT_ONLY,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=False,
        allow_browser_fallback=False,
    )
    match_id = _seed_target(seeded)

    loaded = load_targets(seeded.workspace_id, [match_id])
    target = loaded.targets[0]

    for attempt_number in (1, 2, 3, 4):
        decision = _prepare_dispatch(attempt_number=attempt_number, target=target, visible_providers=loaded.visible_providers, provider_rows=loaded.provider_rows)
        if decision.plan is not None:
            assert decision.plan.use_proxy is False
        assert decision.proxy is None


# --- Edge Case: disabled/missing provider degrades, never crashes ----------


def test_disabled_provider_degrades_to_proxy_failed(seeded: SeededWorkspace) -> None:
    from price_monitor.spiders.generic_price_spider import _prepare_dispatch, load_targets

    from app_shared.enums import AccessStrategy

    disabled_provider_id = _create_proxy_provider(seeded.workspace_id, type="DATACENTER", status="DISABLED")
    _create_access_policy(
        seeded.workspace_id,
        strategy=AccessStrategy.PROXY_FIRST,
        provider_id=disabled_provider_id,
        max_retries=0,
        use_proxy_on_first_attempt=True,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
    )
    match_id = _seed_target(seeded)

    loaded = load_targets(seeded.workspace_id, [match_id])
    target = loaded.targets[0]

    decision = _prepare_dispatch(target, 1, loaded.visible_providers, loaded.provider_rows)

    assert decision.plan is None
    from app_shared.enums import ScrapeErrorCode

    assert decision.skip_error_code == ScrapeErrorCode.PROXY_FAILED


def test_missing_provider_id_degrades_without_crashing(seeded: SeededWorkspace) -> None:
    from price_monitor.spiders.generic_price_spider import _prepare_dispatch, load_targets

    from app_shared.enums import AccessStrategy, ScrapeErrorCode

    dangling_provider_id = uuid.uuid4()  # never created -- absent from visible_providers
    _create_access_policy(
        seeded.workspace_id,
        strategy=AccessStrategy.PROXY_FIRST,
        provider_id=dangling_provider_id,
        max_retries=0,
        use_proxy_on_first_attempt=True,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
    )
    match_id = _seed_target(seeded)

    loaded = load_targets(seeded.workspace_id, [match_id])
    target = loaded.targets[0]

    decision = _prepare_dispatch(target, 1, loaded.visible_providers, loaded.provider_rows)

    assert decision.plan is None
    assert decision.skip_error_code == ScrapeErrorCode.PROXY_FAILED


# --- FR-011 / US2-4: exceeded per-minute ceiling defers, no dispatch -------


def test_exceeded_rate_ceiling_skips_with_rate_limited(seeded: SeededWorkspace) -> None:
    from price_monitor.spiders.generic_price_spider import _prepare_dispatch, load_targets

    from app_shared.enums import AccessStrategy, ScrapeErrorCode

    _create_access_policy(
        seeded.workspace_id,
        strategy=AccessStrategy.DIRECT_ONLY,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=False,
        allow_browser_fallback=False,
        max_requests_per_minute=1,
    )
    match_id = _seed_target(seeded)

    loaded = load_targets(seeded.workspace_id, [match_id])
    target = loaded.targets[0]

    first = _prepare_dispatch(target, 1, loaded.visible_providers, loaded.provider_rows)
    assert first.plan is not None  # first request within the ceiling

    second = _prepare_dispatch(target, 1, loaded.visible_providers, loaded.provider_rows)
    assert second.plan is None
    assert second.skip_error_code == ScrapeErrorCode.RATE_LIMITED


# --- US2-5: exhausted proxy budget reroutes or records LIMIT_REACHED ------


def test_exhausted_proxy_budget_reroutes_direct_then_proxy_to_direct(seeded: SeededWorkspace) -> None:
    from price_monitor.spiders.generic_price_spider import _prepare_dispatch, load_targets

    from app_shared.enums import AccessMethod, AccessStrategy

    provider_id = _create_proxy_provider(seeded.workspace_id, type="DATACENTER", monthly_budget_limit=1)
    _create_access_policy(
        seeded.workspace_id,
        strategy=AccessStrategy.DIRECT_THEN_PROXY,
        provider_id=provider_id,
        max_retries=2,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
    )
    match_id = _seed_target(seeded)

    loaded = load_targets(seeded.workspace_id, [match_id])
    target = loaded.targets[0]

    first_retry = _prepare_dispatch(target, 2, loaded.visible_providers, loaded.provider_rows)
    assert first_retry.plan is not None
    assert first_retry.plan.access_method == AccessMethod.PROXY_HTTP  # consumes the budget=1

    second_retry = _prepare_dispatch(target, 3, loaded.visible_providers, loaded.provider_rows)
    assert second_retry.plan is not None
    assert second_retry.plan.access_method == AccessMethod.DIRECT_HTTP_RETRY
    assert second_retry.plan.use_proxy is False
    assert second_retry.proxy is None


def test_exhausted_proxy_budget_records_limit_reached_with_no_fallback(seeded: SeededWorkspace) -> None:
    from price_monitor.spiders.generic_price_spider import _prepare_dispatch, load_targets

    from app_shared.enums import AccessStrategy, ScrapeErrorCode

    provider_id = _create_proxy_provider(seeded.workspace_id, type="DATACENTER", monthly_budget_limit=1)
    _create_access_policy(
        seeded.workspace_id,
        strategy=AccessStrategy.PROXY_FIRST,
        provider_id=provider_id,
        max_retries=2,
        use_proxy_on_first_attempt=True,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
    )
    match_id = _seed_target(seeded)

    loaded = load_targets(seeded.workspace_id, [match_id])
    target = loaded.targets[0]

    first = _prepare_dispatch(target, 1, loaded.visible_providers, loaded.provider_rows)
    assert first.plan is not None  # consumes the budget=1

    second = _prepare_dispatch(target, 2, loaded.visible_providers, loaded.provider_rows)
    assert second.plan is None
    assert second.skip_error_code == ScrapeErrorCode.LIMIT_REACHED


# --- decrypted proxy password never appears in captured logs ---------------


def test_decrypted_password_never_appears_in_logs(seeded: SeededWorkspace, caplog: pytest.LogCaptureFixture) -> None:
    from price_monitor.spiders.generic_price_spider import GenericPriceSpider, _prepare_dispatch, load_targets

    from app_shared.enums import AccessStrategy

    plaintext_password = f"s3cr3t-{uuid.uuid4().hex}"
    provider_id = _create_proxy_provider(
        seeded.workspace_id,
        type="DATACENTER",
        username="proxyuser",
        _password=plaintext_password,
    )
    _create_access_policy(
        seeded.workspace_id,
        strategy=AccessStrategy.PROXY_FIRST,
        provider_id=provider_id,
        max_retries=0,
        use_proxy_on_first_attempt=True,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
    )
    match_id = _seed_target(seeded)

    with caplog.at_level(logging.DEBUG):
        loaded = load_targets(seeded.workspace_id, [match_id])
        target = loaded.targets[0]
        decision = _prepare_dispatch(target, 1, loaded.visible_providers, loaded.provider_rows)
        assert decision.plan is not None
        assert decision.proxy is not None

        spider = GenericPriceSpider(workspace_id=str(seeded.workspace_id), match_ids=str(match_id))
        spider._provider_rows = loaded.provider_rows
        spider._provider_passwords = loaded.provider_passwords
        request = spider._request_for(target, 1, decision.plan, decision.proxy)

    auth_header = request.headers.get("Proxy-Authorization")
    assert auth_header is not None
    assert plaintext_password not in caplog.text
    assert plaintext_password.encode("ascii") not in auth_header  # base64-encoded, not raw


# =============================================================================
# SPEC-10 US3 (T035): attempt-logging -- `contracts/spider-integration.md`
# Acceptance + quickstart.md §5.
#
# These exercise the same plain, synchronous spider helpers as the US2 tests
# above (`_prepare_dispatch`/`_request_for`) plus the two US3 result-side
# additions (`_attempt_kwargs_from_meta`/`_build_result`) directly -- never
# `start()`/`errback()` themselves, which are `async def` and offload via
# `scrape_core.db.run_in_thread` (`deferToThread`); that seam needs a live
# Twisted reactor thread pool to ever fire, which a bare `asyncio.run()` in
# a pytest test does not provide (SPEC-07/08's reactor-safety unit/live
# tests already cover that offload seam structurally -- not this file's
# job). Calling the same plain functions `errback`/`parse` call internally
# exercises the identical production code path/contract without that
# dependency, matching this file's own established convention (see the
# module docstring).
#
# Persistence goes through `scrape_core.pipelines._flush_batch` -- the
# same, unchanged pure core the real `BatchedPersistencePipeline` calls
# from inside `run_in_thread` for every flush (size/time/close-triggered);
# no pipeline/model/migration change (T034/T035 are a wiring finish only).
# =============================================================================


# --- US3-1 / SC-002: direct attempt 1 + proxied retry persist 2 rows -------


def test_direct_attempt_then_proxied_retry_persists_two_request_attempt_rows(
    seeded: SeededWorkspace,
) -> None:
    from scrape_core.errors import classify_exception
    from scrape_core.items import ScrapeResult
    from scrape_core.pipelines import _flush_batch

    from price_monitor.spiders.generic_price_spider import (
        GenericPriceSpider,
        _attempt_kwargs_from_meta,
        _prepare_dispatch,
        load_targets,
    )

    from app_shared.enums import AccessMethod, AccessStrategy, ScrapeErrorCode

    provider_id = _create_proxy_provider(seeded.workspace_id, type="DATACENTER", country_code="US")
    _create_access_policy(
        seeded.workspace_id,
        strategy=AccessStrategy.DIRECT_THEN_PROXY,
        provider_id=provider_id,
        max_retries=1,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
    )
    match_id = _seed_target(seeded)

    loaded = load_targets(seeded.workspace_id, [match_id])
    target = loaded.targets[0]
    spider = GenericPriceSpider(workspace_id=str(seeded.workspace_id), match_ids=str(match_id))
    spider._provider_rows = loaded.provider_rows
    spider._provider_passwords = loaded.provider_passwords

    # Attempt 1: plain direct -- fails with a connection-level error (what
    # `errback` would receive as `failure.value`).
    first_decision = _prepare_dispatch(target, 1, loaded.visible_providers, loaded.provider_rows)
    assert first_decision.plan is not None
    assert first_decision.plan.access_method == AccessMethod.DIRECT_HTTP
    request1 = spider._request_for(target, 1, first_decision.plan, first_decision.proxy)
    now1 = datetime.now(UTC)
    result1 = spider._build_result(
        target,
        request1.url,
        now1,
        status_code=None,
        success=False,
        error_code=classify_exception(ConnectionRefusedError("connection refused")),
        error_message="connection refused",
        **_attempt_kwargs_from_meta(request1.meta),
    )
    assert isinstance(result1, ScrapeResult)
    assert result1.attempt_number == 1
    assert result1.access_method == AccessMethod.DIRECT_HTTP
    assert result1.proxy_provider_id is None
    assert result1.error_code == ScrapeErrorCode.UNKNOWN_ERROR  # a bare ConnectionRefusedError

    # Attempt 2 (the retry `errback` would dispatch): proxied per the
    # policy, terminal here (max_retries=1) -- times out.
    second_decision = _prepare_dispatch(target, 2, loaded.visible_providers, loaded.provider_rows)
    assert second_decision.plan is not None
    assert second_decision.plan.access_method == AccessMethod.PROXY_HTTP
    assert second_decision.proxy is not None
    request2 = spider._request_for(target, 2, second_decision.plan, second_decision.proxy)
    now2 = datetime.now(UTC)

    class _TimeoutErrorLike(Exception):
        pass

    result2 = spider._build_result(
        target,
        request2.url,
        now2,
        status_code=None,
        success=False,
        error_code=classify_exception(_TimeoutErrorLike("upstream timed out")),
        error_message="upstream timed out",
        **_attempt_kwargs_from_meta(request2.meta),
    )
    assert result2.attempt_number == 2
    assert result2.access_method == AccessMethod.PROXY_HTTP
    assert result2.proxy_provider_id == provider_id
    assert result2.proxy_country == "US"
    assert result2.error_code == ScrapeErrorCode.TIMEOUT

    # Persist both attempts through the unchanged pipeline's pure core --
    # one ScrapeResult per attempt (including the retry), so exactly two
    # `RequestAttempt` rows result (FR-012/013/015).
    _flush_batch(seeded.workspace_id, [result1, result2])

    rows = _fetch_request_attempts(seeded.workspace_id, match_id)
    assert len(rows) == 2  # exactly one row per attempt (SC-002)
    row1, row2 = rows  # already ordered by attempt_number
    assert row1["attempt_number"] == 1
    assert row1["access_method"] == "DIRECT_HTTP"
    assert row1["proxy_provider_id"] is None
    assert row1["proxy_country"] is None
    assert row1["success"] is False

    assert row2["attempt_number"] == 2
    assert row2["access_method"] == "PROXY_HTTP"
    assert row2["proxy_provider_id"] == provider_id
    assert row2["proxy_country"] == "US"
    assert row2["error_code"] == "TIMEOUT"

    # Each row lands in its own attempt's correct monthly partition
    # (US3-3) -- both attempts happen within the same test run, so the
    # same current-month partition for both.
    for row in rows:
        expected_partition = f"request_attempts_{row['created_at']:%Y_%m}"
        assert row["partition_name"] == expected_partition


# US3-2 (blocked/timeout/proxy-connect -> the matching `error_code`) is
# pure classification coverage with no DB/Redis dependency at all --
# unit-tested in `tests/unit/test_errors.py` (runs everywhere, not gated
# behind this file's live-stack skip) rather than duplicated here. The
# PROXY_FAILED case specifically is also exercised end-to-end through a
# real persisted row below (never-dispatched skip -> `RequestAttempt`).


# --- Edge Case + US3-2: a never-dispatched PROXY_FAILED skip still logs ----


def test_disabled_provider_skip_persists_one_row_with_proxy_failed(
    seeded: SeededWorkspace,
) -> None:
    """The `start()` skip branch (`decision.skip_error_code` set, no
    request ever dispatched) still needs its own `RequestAttempt` row --
    `_DispatchDecision.attempted_method` (T034) supplies the access_method
    `_build_result` would otherwise have no way to know (no request.meta
    exists for an attempt that was never dispatched)."""
    from scrape_core.pipelines import _flush_batch

    from price_monitor.spiders.generic_price_spider import (
        GenericPriceSpider,
        _prepare_dispatch,
        load_targets,
    )

    from app_shared.enums import AccessMethod, AccessStrategy, ScrapeErrorCode

    disabled_provider_id = _create_proxy_provider(seeded.workspace_id, type="DATACENTER", status="DISABLED")
    _create_access_policy(
        seeded.workspace_id,
        strategy=AccessStrategy.PROXY_FIRST,
        provider_id=disabled_provider_id,
        max_retries=0,
        use_proxy_on_first_attempt=True,
        use_proxy_on_retry=True,
        allow_browser_fallback=False,
    )
    match_id = _seed_target(seeded)

    loaded = load_targets(seeded.workspace_id, [match_id])
    target = loaded.targets[0]
    spider = GenericPriceSpider(workspace_id=str(seeded.workspace_id), match_ids=str(match_id))

    decision = _prepare_dispatch(target, 1, loaded.visible_providers, loaded.provider_rows)
    assert decision.plan is None
    assert decision.skip_error_code == ScrapeErrorCode.PROXY_FAILED

    # Mirrors `start()`'s skip branch exactly.
    result = spider._build_result(
        target,
        target.url,
        datetime.now(UTC),
        status_code=None,
        success=False,
        error_code=decision.skip_error_code,
        error_message="attempt 1 not dispatched",
        access_method=decision.attempted_method,
        attempt_number=1,
        proxy_provider_id=(decision.attempted_proxy.provider_id if decision.attempted_proxy else None),
        proxy_country=(decision.attempted_proxy.country if decision.attempted_proxy else None),
    )
    assert result.access_method == AccessMethod.PROXY_HTTP  # the intended (never-dispatched) method
    assert result.proxy_provider_id is None  # no provider was ever assigned
    assert result.attempt_number == 1

    _flush_batch(seeded.workspace_id, [result])

    rows = _fetch_request_attempts(seeded.workspace_id, match_id)
    assert len(rows) == 1  # exactly one row for this one (never-dispatched) attempt
    assert rows[0]["error_code"] == "PROXY_FAILED"
    assert rows[0]["access_method"] == "PROXY_HTTP"
    assert rows[0]["status_code"] is None
    assert rows[0]["response_time_ms"] is None  # never dispatched -- nothing to time


# --- US3-4 / SC-005: workspace-scoped query sees only its own attempt rows -


def test_workspace_scoped_query_sees_only_its_own_rows_no_context_zero(
    seeded: SeededWorkspace,
) -> None:
    from sqlalchemy import create_engine, text

    from scrape_core.pipelines import _flush_batch

    from price_monitor.spiders.generic_price_spider import (
        GenericPriceSpider,
        _attempt_kwargs_from_meta,
        _prepare_dispatch,
        load_targets,
    )

    from app_shared.config import get_settings
    from app_shared.enums import AccessStrategy

    _create_access_policy(
        seeded.workspace_id,
        strategy=AccessStrategy.DIRECT_ONLY,
        max_retries=0,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=False,
        allow_browser_fallback=False,
    )
    match_id = _seed_target(seeded)

    loaded = load_targets(seeded.workspace_id, [match_id])
    target = loaded.targets[0]
    spider = GenericPriceSpider(workspace_id=str(seeded.workspace_id), match_ids=str(match_id))

    decision = _prepare_dispatch(target, 1, loaded.visible_providers, loaded.provider_rows)
    assert decision.plan is not None
    request1 = spider._request_for(target, 1, decision.plan, decision.proxy)
    result = spider._build_result(
        target,
        request1.url,
        datetime.now(UTC),
        status_code=200,
        success=True,
        **_attempt_kwargs_from_meta(request1.meta),
    )
    _flush_batch(seeded.workspace_id, [result])

    from app_shared.database import get_session, set_workspace_context

    with get_session() as session:
        set_workspace_context(session, seeded.workspace_id)
        own_match_ids = {
            row[0]
            for row in session.execute(
                text("SELECT match_id FROM request_attempts WHERE workspace_id = :ws"),
                {"ws": seeded.workspace_id},
            ).fetchall()
        }
    assert match_id in own_match_ids

    engine = create_engine(get_settings().DATABASE_URL)
    try:
        with engine.begin() as conn:
            # Deliberately no `set_config('app.workspace_id', ...)` at all
            # -- RLS is the only thing standing between this query and the
            # row this test just inserted (fail-closed, US3-4/SC-005).
            no_context_rows = conn.execute(
                text("SELECT match_id FROM request_attempts WHERE match_id = :match_id"),
                {"match_id": match_id},
            ).fetchall()
    finally:
        engine.dispose()
    assert no_context_rows == []
