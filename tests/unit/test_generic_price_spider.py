"""``generic_price_spider`` unit tests (SPEC-07 tasks.md T054, FR-006).

Exercises `GenericPriceSpider._request_for` -- the pure request-building
step `start()` delegates to -- directly, with a hand-built `SpiderTarget`.
No DB/Redis/reactor: `load_targets` (the DB-touching half of T054, which
now also reads `Competitor.robots_policy`) is intentionally not exercised
here (would need Postgres); this covers the half that's unit-testable
off-reactor -- that whatever `robots_policy` a target carries reaches
`request.meta["robots_policy"]`, which is what
`scrape_core.robots.RobotsPolicyMiddleware.process_request` actually
reads (defaulting to `RobotsPolicy.RESPECT` only when the key is
missing).
"""

from __future__ import annotations

import uuid

import pytest

from app_shared.enums import RobotsPolicy

from price_monitor.spiders import generic_price_spider as gps


@pytest.fixture()
def spider() -> gps.GenericPriceSpider:
    return gps.GenericPriceSpider(
        workspace_id=str(uuid.uuid4()),
        match_ids=str(uuid.uuid4()),
    )


def _target(robots_policy: RobotsPolicy) -> gps.SpiderTarget:
    return gps.SpiderTarget(
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        url="https://shop.example.com/product/1",
        profile=None,
        robots_policy=robots_policy,
    )


@pytest.mark.parametrize(
    "policy",
    [
        RobotsPolicy.RESPECT,
        RobotsPolicy.IGNORE_AFTER_APPROVAL,
        RobotsPolicy.REVIEW_REQUIRED,
    ],
)
def test_request_carries_resolved_robots_policy(
    spider: gps.GenericPriceSpider, policy: RobotsPolicy
) -> None:
    target = _target(policy)

    request = spider._request_for(target)

    assert request.meta["robots_policy"] == policy


def test_request_still_carries_match_id_and_download_slot(
    spider: gps.GenericPriceSpider,
) -> None:
    target = _target(RobotsPolicy.RESPECT)

    request = spider._request_for(target)

    assert request.meta["match_id"] == target.match_id
    assert request.meta["download_slot"] == str(target.match_id)
    assert request.url == target.url
