"""Live SPEC-12 US2 consumption-seam integration test (T023,
`contracts/consumption.md`, FR-013/FR-016, SC-001) — ⏸ DEFERRED.

Mirrors `test_spider_access.py`'s lighter-weight pattern (T032/T035):
rather than a full `run_generic_price_spider_subprocess` crawl, this
exercises `generic_price_spider.load_targets`/`_prepare_dispatch`
**directly** against a live Postgres (for the profile get-or-create +
access-policy resolution) and Redis (for the enqueue-dedup counter and
the access ceiling/cooldown gates) — still skip-clean on the same
`live_stack_reachable` probe the sibling `test_spider_*_live.py` files
use.

Three scenarios (US2 Independent Test):

1. An `ACTIVE` profile (`preferred_access_method=PROXY_HTTP`,
   `preferred_extraction_method=CSS`) pre-seeded for a match's
   `(workspace, competitor, domain, url_pattern)` key -> `load_targets`
   threads `strategy_start`/`domain_strategy_profile_id` onto the
   target, and the group's first `_prepare_dispatch` decision (attempt
   1) uses the learned `PROXY_HTTP` even though the resolved
   `AccessPolicy` is `DIRECT_ONLY` (which would otherwise never proxy,
   SC-001).
2. An unseen key -> `target.strategy_start is None` (default ladder
   unchanged), a fresh `DISCOVERY_REQUIRED` `domain_strategy_profiles`
   row is created stamped at the current `URL_PATTERN_ALGORITHM_VERSION`,
   and exactly one `STRATEGY_DISCOVERY_RUN` message lands on the
   `strategy_discovery` Redis queue -- a second `load_targets` call for
   the very same key does not enqueue a second time (get-or-create is
   idempotent).
3. A `DISABLED` profile (with a stale `preferred_access_method=PROXY_HTTP`
   still stored) -> `target.strategy_start is None` and the first
   attempt uses the plain default-ladder method, never the stale
   preference (FR-014).

Needs a reachable Postgres (`DATABASE_URL`) with the SPEC-12 migration
applied AND a reachable Redis (`REDIS_URL`) -- reuses
`_scrapyd_spider_live_support.live_stack_reachable`. Not runnable in the
no-Docker-daemon build environment used to author this feature -- SKIPS
cleanly whenever either isn't usable or the required tables don't exist.

Author now; leave unchecked (DEFERRED -- needs a Postgres+Redis host
with the SPEC-12 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

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
    "request_attempts",
    "domain_strategy_profiles",
    "strategy_discovery_runs",
)

pytestmark = pytest.mark.skipif(
    not live_stack_reachable(_REQUIRED_TABLES),
    reason="No reachable Postgres (with the SPEC-12 migration applied) + Redis in this environment",
)


# --- seeding / cleanup helpers -----------------------------------------------


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

    unique = uuid.uuid4().hex[:8]
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


def _seed_strategy_profile(
    seeded: SeededWorkspace,
    competitor_id: uuid.UUID,
    *,
    domain: str,
    url_pattern: str,
    status: str,
    preferred_access_method: str | None = None,
    preferred_extraction_method: str | None = None,
) -> uuid.UUID:
    from app_shared.database import get_session
    from app_shared.models.strategy import DomainStrategyProfile
    from app_shared.url_pattern import URL_PATTERN_ALGORITHM_VERSION

    with get_session() as session:
        profile = DomainStrategyProfile(
            workspace_id=seeded.workspace_id,
            competitor_id=competitor_id,
            domain=domain,
            url_pattern=url_pattern,
            url_pattern_version=URL_PATTERN_ALGORITHM_VERSION,
            status=status,
            preferred_access_method=preferred_access_method,
            preferred_extraction_method=preferred_extraction_method,
            access_confidence=Decimal("0.9000") if preferred_access_method else None,
            extraction_confidence=Decimal("0.9000") if preferred_extraction_method else None,
        )
        session.add(profile)
        session.commit()
        session.refresh(profile)
        return profile.id


def _fetch_strategy_profile(workspace_id: uuid.UUID, competitor_id: uuid.UUID, domain: str, url_pattern: str):
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        return session.execute(
            text(
                "SELECT id, status, url_pattern_version FROM domain_strategy_profiles "
                "WHERE workspace_id = :ws AND competitor_id = :cid AND domain = :domain "
                "AND url_pattern = :url_pattern"
            ),
            {"ws": workspace_id, "cid": competitor_id, "domain": domain, "url_pattern": url_pattern},
        ).mappings().one_or_none()


def _cleanup_access_rows(workspace_id: uuid.UUID) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        session.execute(text("DELETE FROM access_policies WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM proxy_providers WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.commit()


def _cleanup_strategy_rows(workspace_id: uuid.UUID) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        session.execute(
            text("DELETE FROM strategy_discovery_runs WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.execute(
            text("DELETE FROM domain_strategy_profiles WHERE workspace_id = :ws"), {"ws": workspace_id}
        )
        session.commit()


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    ws = seed_workspace_with_variant("consumption-seam")
    yield ws
    _cleanup_strategy_rows(ws.workspace_id)
    _cleanup_access_rows(ws.workspace_id)
    cleanup_seeded_workspace(ws)


# --- US2 AS1: an ACTIVE profile seeds the first attempt's method ------------


def test_active_profile_seeds_first_attempt_with_preferred_methods(seeded: SeededWorkspace) -> None:
    from price_monitor.spiders.generic_price_spider import _prepare_dispatch, load_targets

    from app_shared.enums import AccessMethod, AccessStrategy, ExtractionMethod

    provider_id = _create_proxy_provider(seeded.workspace_id, type="DATACENTER", country_code="US")
    # DIRECT_ONLY would never proxy on its own -- proves the override, not
    # a coincidence of the policy's own default ladder (SC-001).
    _create_access_policy(
        seeded.workspace_id,
        strategy=AccessStrategy.DIRECT_ONLY,
        provider_id=provider_id,
        max_retries=1,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=False,
        allow_browser_fallback=False,
    )
    competitor_id = seed_competitor(seeded, "consumption-active")
    unique = uuid.uuid4().hex[:8]
    url = f"https://consumption-active-{unique}.invalid/products/red-shoe-123"
    match_id = seed_match(seeded, competitor_id, url)

    from app_shared.database import get_session
    from app_shared.models.competitors_matches import Competitor

    with get_session() as session:
        domain = session.get(Competitor, competitor_id).domain

    # STRATEGY_PROFILE_SCOPE defaults to "domain" (2026-07-11 discovery-gate
    # fix) -- `load_targets`'s lookup key is the bare domain, not a derived
    # v1 pattern, so the pre-seeded profile must be keyed the same way.
    url_pattern = domain
    profile_id = _seed_strategy_profile(
        seeded,
        competitor_id,
        domain=domain,
        url_pattern=url_pattern,
        status="ACTIVE",
        preferred_access_method=AccessMethod.PROXY_HTTP.value,
        preferred_extraction_method=ExtractionMethod.CSS.value,
    )

    loaded = load_targets(seeded.workspace_id, [match_id])
    assert len(loaded.targets) == 1
    target = loaded.targets[0]
    assert target.domain_strategy_profile_id == profile_id
    assert target.strategy_start is not None
    assert target.strategy_start.access_method == AccessMethod.PROXY_HTTP
    assert target.strategy_start.extraction_method == ExtractionMethod.CSS

    decision = _prepare_dispatch(target, 1, loaded.visible_providers, loaded.provider_rows)
    assert decision.plan is not None
    assert decision.plan.access_method == AccessMethod.PROXY_HTTP
    assert decision.plan.use_proxy is True
    assert decision.proxy is not None
    assert decision.proxy.provider_id == provider_id


# --- US2 AS2: an unseen key falls back + auto-enqueues discovery once ------


def test_unseen_key_falls_back_and_seeds_discovery_required_profile_once(
    seeded: SeededWorkspace,
) -> None:
    from price_monitor.spiders.generic_price_spider import load_targets

    from app_shared.enums import AccessStrategy
    from app_shared.redis_client import get_redis_client
    from app_shared.url_pattern import URL_PATTERN_ALGORITHM_VERSION

    _create_access_policy(seeded.workspace_id, strategy=AccessStrategy.DIRECT_ONLY, max_retries=0)
    competitor_id = seed_competitor(seeded, "consumption-unseen")
    unique = uuid.uuid4().hex[:8]
    url = f"https://consumption-unseen-{unique}.invalid/products/blue-shoe-999"
    match_id = seed_match(seeded, competitor_id, url)

    from app_shared.database import get_session
    from app_shared.models.competitors_matches import Competitor

    with get_session() as session:
        domain = session.get(Competitor, competitor_id).domain
    # STRATEGY_PROFILE_SCOPE defaults to "domain" -- the lookup key is the
    # bare domain (2026-07-11 discovery-gate fix).
    url_pattern = domain

    redis_client = get_redis_client()
    before = redis_client.llen("strategy_discovery")

    loaded = load_targets(seeded.workspace_id, [match_id])
    target = loaded.targets[0]
    assert target.strategy_start is None
    assert target.domain_strategy_profile_id is not None

    row = _fetch_strategy_profile(seeded.workspace_id, competitor_id, domain, url_pattern)
    assert row is not None
    assert row["status"] == "DISCOVERY_REQUIRED"
    assert row["url_pattern_version"] == URL_PATTERN_ALGORITHM_VERSION

    after = redis_client.llen("strategy_discovery")
    assert after == before + 1

    # A second resolution for the exact same (now-existing) key must not
    # enqueue discovery again -- get-or-create is idempotent.
    load_targets(seeded.workspace_id, [match_id])
    after_second = redis_client.llen("strategy_discovery")
    assert after_second == after


# --- US2 AS3: a DISABLED profile never applies its stale preference --------


def test_disabled_profile_falls_back_to_default_ladder(seeded: SeededWorkspace) -> None:
    from price_monitor.spiders.generic_price_spider import _prepare_dispatch, load_targets

    from app_shared.enums import AccessMethod, AccessStrategy, ExtractionMethod

    _create_access_policy(
        seeded.workspace_id,
        strategy=AccessStrategy.DIRECT_ONLY,
        max_retries=1,
        use_proxy_on_first_attempt=False,
        use_proxy_on_retry=False,
        allow_browser_fallback=False,
    )
    competitor_id = seed_competitor(seeded, "consumption-disabled")
    unique = uuid.uuid4().hex[:8]
    url = f"https://consumption-disabled-{unique}.invalid/products/green-shoe-321"
    match_id = seed_match(seeded, competitor_id, url)

    from app_shared.database import get_session
    from app_shared.models.competitors_matches import Competitor

    with get_session() as session:
        domain = session.get(Competitor, competitor_id).domain
    # STRATEGY_PROFILE_SCOPE defaults to "domain" -- the lookup key is the
    # bare domain (2026-07-11 discovery-gate fix).
    url_pattern = domain

    _seed_strategy_profile(
        seeded,
        competitor_id,
        domain=domain,
        url_pattern=url_pattern,
        status="DISABLED",
        preferred_access_method=AccessMethod.PROXY_HTTP.value,
        preferred_extraction_method=ExtractionMethod.CSS.value,
    )

    loaded = load_targets(seeded.workspace_id, [match_id])
    target = loaded.targets[0]
    assert target.strategy_start is None

    decision = _prepare_dispatch(target, 1, loaded.visible_providers, loaded.provider_rows)
    assert decision.plan is not None
    assert decision.plan.access_method == AccessMethod.DIRECT_HTTP
    assert decision.plan.use_proxy is False


# --- Domain-scope discovery-gate fix (2026-07-11, PLAN_DOMAIN_STRATEGY_PROFILES.md) --


def test_distinct_url_patterns_same_domain_share_one_profile(seeded: SeededWorkspace) -> None:
    """The bug this fix closes: per-product-slug URLs each derive their own
    n=1 v1 pattern, so under the old url_pattern-keyed lookup, discovery
    never gathered >= STRATEGY_DISCOVERY_MIN_SAMPLE and never ran. Under
    the "domain" scope default, two matches whose URLs differ (and are
    seeded with their own literal `url_pattern`, `seed_match`'s
    convention) still resolve to the SAME `domain_strategy_profiles` row
    keyed by the shared competitor domain -- one profile, one discovery
    enqueue, not one per slug."""
    from price_monitor.spiders.generic_price_spider import load_targets

    from app_shared.enums import AccessStrategy
    from app_shared.redis_client import get_redis_client

    _create_access_policy(seeded.workspace_id, strategy=AccessStrategy.DIRECT_ONLY, max_retries=0)
    competitor_id = seed_competitor(seeded, "consumption-domain-scope")
    unique = uuid.uuid4().hex[:8]
    url_a = f"https://consumption-domain-scope-{unique}.invalid/products/red-shoe-111"
    url_b = f"https://consumption-domain-scope-{unique}.invalid/products/blue-shoe-222"
    match_id_a = seed_match(seeded, competitor_id, url_a)
    match_id_b = seed_match(seeded, competitor_id, url_b)

    redis_client = get_redis_client()
    before = redis_client.llen("strategy_discovery")

    loaded = load_targets(seeded.workspace_id, [match_id_a, match_id_b])
    assert len(loaded.targets) == 2
    profile_ids = {target.domain_strategy_profile_id for target in loaded.targets}
    assert len(profile_ids) == 1, "both slug-unique matches must resolve to one shared profile"

    after = redis_client.llen("strategy_discovery")
    assert after == before + 1, "one profile -> one discovery enqueue, not one per match"


def test_url_pattern_scope_rollback_keeps_per_pattern_profiles(
    seeded: SeededWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`STRATEGY_PROFILE_SCOPE=url_pattern` is a config-only rollback to the
    exact legacy per-pattern behavior -- two matches with distinct stored
    `url_pattern` values get two distinct profiles, same as before this
    fix."""
    import app_shared.config as config_module
    from price_monitor.spiders.generic_price_spider import load_targets

    from app_shared.enums import AccessStrategy

    monkeypatch.setenv("STRATEGY_PROFILE_SCOPE", "url_pattern")
    config_module.get_settings.cache_clear()
    try:
        _create_access_policy(seeded.workspace_id, strategy=AccessStrategy.DIRECT_ONLY, max_retries=0)
        competitor_id = seed_competitor(seeded, "consumption-legacy-scope")
        unique = uuid.uuid4().hex[:8]
        url_a = f"https://consumption-legacy-scope-{unique}.invalid/products/red-shoe-111"
        url_b = f"https://consumption-legacy-scope-{unique}.invalid/products/blue-shoe-222"
        match_id_a = seed_match(seeded, competitor_id, url_a)
        match_id_b = seed_match(seeded, competitor_id, url_b)

        loaded = load_targets(seeded.workspace_id, [match_id_a, match_id_b])
        assert len(loaded.targets) == 2
        profile_ids = {target.domain_strategy_profile_id for target in loaded.targets}
        assert len(profile_ids) == 2, "legacy scope keeps one profile per distinct url_pattern"
    finally:
        config_module.get_settings.cache_clear()
