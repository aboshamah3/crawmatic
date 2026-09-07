"""Tests for EPA A7 (deep dive §8.2): browser child ``duration_ms``.

Before A7, ``NetLedgerMiddleware._drain_subresources`` built every
sub-resource's :class:`~app_shared.netledger.recorder.OperationOutcome`
without ever passing ``duration_ms`` — a child row's own fetch time was
simply never recorded. This module proves the fix:
``NetLedgerMiddleware._child_duration_ms`` reads Playwright/CDP resource
timing off the accumulator's observed dict (``{"requestStart": 0,
"responseEnd": 240}`` -> ``240``) and, when the browser path never
captured timing for a response, yields ``None`` rather than a fabricated
number — the same NEVER-FABRICATE posture as every other figure this
module writes.

Offline throughout: a hand-built fake accumulator stands in for
``scrape_core.browser.byte_capture.ByteAccumulator`` (that module is out
of this task's scope — a parallel EPA task owns it), and
``_drain_subresources`` is exercised directly, with no recorder, no
reactor, no database.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import scrapy

from scrape_core.netledger_middleware import NetLedgerMiddleware


def _bare_middleware() -> NetLedgerMiddleware:
    """A middleware instance with no recorder — enough to call the pure
    ``_child_duration_ms``/``_drain_subresources`` methods under test,
    which touch neither ``self._recorder`` nor the reactor."""
    return NetLedgerMiddleware.__new__(NetLedgerMiddleware)


class TestChildDurationFromTiming:
    @pytest.mark.parametrize(
        ("timing", "expected"),
        [
            ({"requestStart": 0, "responseEnd": 240}, 240),
            ({"requestStart": 100, "responseEnd": 340}, 240),
            (None, None),
            ({}, None),
            ({"requestStart": 0}, None),  # responseEnd missing
            ({"responseEnd": 240}, None),  # requestStart missing
            ({"requestStart": None, "responseEnd": 240}, None),
        ],
    )
    def test_duration_from_timing_dict(
        self, timing: dict[str, Any] | None, expected: int | None
    ) -> None:
        observed = {"timing": timing} if timing is not None else {}
        assert NetLedgerMiddleware._child_duration_ms(observed) == expected

    def test_no_timing_key_at_all_yields_none(self) -> None:
        """The ordinary case today: the accumulator did not capture
        timing for this response at all."""
        assert NetLedgerMiddleware._child_duration_ms({"url": "https://x/a.js"}) is None


class _FakeAccumulator:
    """Stands in for ``ByteAccumulator.drain_subresources()``."""

    def __init__(self, subresources: list[dict[str, Any]]) -> None:
        self._subresources = list(subresources)

    def drain_subresources(self) -> list[dict[str, Any]]:
        drained, self._subresources = self._subresources, []
        return drained


class TestDrainSubresourcesCarriesDuration:
    def test_children_carry_the_observed_duration(self) -> None:
        middleware = _bare_middleware()
        accumulator = _FakeAccumulator(
            [
                {
                    "url": "https://cdn.example.test/a.js",
                    "status": 200,
                    "method": "GET",
                    "resource_type": "script",
                    "byte_count": 3_000,
                    "timing": {"requestStart": 0, "responseEnd": 240},
                },
                {
                    "url": "https://cdn.example.test/b.css",
                    "status": 200,
                    "method": "GET",
                    "resource_type": "stylesheet",
                    "byte_count": 1_500,
                    # No timing captured for this one.
                },
            ]
        )
        request = scrapy.Request(
            url="https://shop.example.test/p/1",
            meta={
                "playwright": True,
                "playwright_context": "proxy:abc",
                "_byte_accumulator": accumulator,
            },
            dont_filter=True,
        )
        spider = SimpleNamespace(workspace_id=None, scrape_job_id=None, authorization_id=None)

        children = middleware._drain_subresources(request, uuid.uuid4(), spider)

        assert len(children) == 2
        (js_intent, js_outcome), (css_intent, css_outcome) = children
        assert js_intent.url == "https://cdn.example.test/a.js"
        assert js_outcome.duration_ms == 240
        assert css_intent.url == "https://cdn.example.test/b.css"
        assert css_outcome.duration_ms is None

    def test_draining_twice_yields_nothing_the_second_time(self) -> None:
        """Not this task's contract, but pinned so a duration_ms change
        cannot silently reintroduce a double-drain."""
        middleware = _bare_middleware()
        accumulator = _FakeAccumulator(
            [
                {
                    "url": "https://cdn.example.test/a.js",
                    "status": 200,
                    "method": "GET",
                    "resource_type": "script",
                    "byte_count": 3_000,
                    "timing": {"requestStart": 0, "responseEnd": 240},
                }
            ]
        )
        request = scrapy.Request(
            url="https://shop.example.test/p/1",
            meta={"playwright": True, "_byte_accumulator": accumulator},
            dont_filter=True,
        )
        spider = SimpleNamespace(workspace_id=None, scrape_job_id=None, authorization_id=None)

        first = middleware._drain_subresources(request, uuid.uuid4(), spider)
        second = middleware._drain_subresources(request, uuid.uuid4(), spider)

        assert len(first) == 1
        assert second == []
