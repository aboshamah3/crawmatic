"""`HardDeadlineExtension` terminates a spider process that outlives its
graceful deadline, and only then (2026-09-23 wedged-spider incidents)."""

from __future__ import annotations

import pytest
from scrapy.exceptions import NotConfigured
from twisted.internet.task import Clock

from scrape_core.extensions.hard_deadline import HARD_DEADLINE_EXIT_CODE, HardDeadlineExtension


class _Spider:
    name = "generic_price_spider"


def _build(clock: Clock, *, timeout: int = 1800, grace: int = 120) -> tuple[HardDeadlineExtension, list[int]]:
    exits: list[int] = []
    ext = HardDeadlineExtension(
        timeout_seconds=timeout,
        grace_seconds=grace,
        call_later=clock.callLater,
        exit_fn=exits.append,
    )
    return ext, exits


def test_a_spider_still_alive_after_timeout_plus_grace_is_terminated() -> None:
    clock = Clock()
    ext, exits = _build(clock, timeout=1800, grace=120)
    ext.spider_opened(_Spider())

    clock.advance(1919)
    assert exits == [], "must not fire before the graceful deadline plus grace"
    clock.advance(1)
    assert exits == [HARD_DEADLINE_EXIT_CODE]


def test_a_spider_that_closes_in_time_is_left_alone() -> None:
    clock = Clock()
    ext, exits = _build(clock)
    spider = _Spider()
    ext.spider_opened(spider)
    clock.advance(600)
    ext.spider_closed(spider, reason="finished")

    clock.advance(10_000)
    assert exits == []


def test_a_project_without_a_graceful_timeout_gets_no_hard_deadline() -> None:
    with pytest.raises(NotConfigured):
        HardDeadlineExtension(
            timeout_seconds=0, grace_seconds=120, call_later=Clock().callLater, exit_fn=lambda c: None
        )
