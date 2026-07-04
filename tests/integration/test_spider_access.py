"""Live SPEC-10 US2 spider access-policy integration test (T032,
`contracts/spider-integration.md` Acceptance, request-side) — DEFERRED.

Per the task brief: reproducing the full
``run_generic_price_spider_subprocess`` machinery (a real Scrapy crawl
against a loopback fixture server) for every one of these scenarios
would be heavy for this pass, so this file exercises
``generic_price_spider.load_targets``/``_prepare_dispatch``/
``_request_for`` **directly** against a live Postgres (for the access
resolution + provider/decrypt bounded loads) and Redis (for the
ceiling/cooldown/budget gates) — still skip-clean on the same
``live_stack_reachable`` probe the sibling ``test_spider_*_live.py``
files use, still asserting the genuine acceptance behaviors:

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


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    ws = seed_workspace_with_variant("spider-access")
    yield ws
    _cleanup_access_rows(ws.workspace_id)
    cleanup_seeded_workspace(ws)


def _seed_target(seeded: SeededWorkspace, *, url: str | None = None) -> uuid.UUID:
    competitor_id = seed_competitor(seeded, "access-target")
    unique = uuid.uuid4().hex[:8]
    return seed_match(seeded, competitor_id, url or f"https://access-target.invalid/p/{unique}")


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
