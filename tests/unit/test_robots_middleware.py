"""Robots-policy middleware tests (SPEC-07 US2 T031, contracts/robots-middleware.md).

`RobotsPolicyMiddleware.process_request` offloads the `RESPECT` path's
robots.txt fetch through `scrape_core.db.run_in_thread` (reactor-safe —
never blocks the reactor thread), which returns an un-fired `Deferred`
outside a running Twisted reactor loop. These tests therefore exercise:

- the two **synchronous, no-IO** branches (`IGNORE_AFTER_APPROVAL`,
  `REVIEW_REQUIRED`) directly through `process_request`;
- the `RESPECT` decision logic through `_decide_respect`, the pure/
  synchronous core `process_request` defers to — calling it directly
  (with a fixture-backed, no-network `robots_fetcher` injected) is
  exactly the "fixtures supply a robots body without a network call"
  seam the contract describes, and needs no reactor to drive.

No real network call is made anywhere in this file (FR-021/SC-007).
"""

from __future__ import annotations

import pytest

from app_shared.enums import RobotsPolicy

from scrape_core.robots import RobotsBlockedError, RobotsPolicyMiddleware


class _FakeRequest:
    def __init__(self, url: str, meta: dict | None = None) -> None:
        self.url = url
        self.meta = meta or {}


_ROBOTS_BODY_DISALLOW_PRIVATE = "User-agent: *\nDisallow: /private/\n"


def _fetcher_returning(body: str | None):
    calls: list[str] = []

    def fetch(robots_url: str) -> str | None:
        calls.append(robots_url)
        return body

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


# --- RESPECT: disallowed path skipped (BLOCKED) ---------------------------------


def test_respect_blocks_a_disallowed_path() -> None:
    fetcher = _fetcher_returning(_ROBOTS_BODY_DISALLOW_PRIVATE)
    middleware = RobotsPolicyMiddleware(robots_fetcher=fetcher)

    with pytest.raises(RobotsBlockedError):
        middleware._decide_respect(
            "https://shop.example.com/private/secret", "price_monitor"
        )

    assert fetcher.calls == ["https://shop.example.com/robots.txt"]  # type: ignore[attr-defined]


def test_respect_allows_a_path_not_disallowed() -> None:
    fetcher = _fetcher_returning(_ROBOTS_BODY_DISALLOW_PRIVATE)
    middleware = RobotsPolicyMiddleware(robots_fetcher=fetcher)

    # No exception raised -- allowed.
    middleware._decide_respect("https://shop.example.com/product/1", "price_monitor")


def test_respect_caches_the_robots_fetch_per_origin() -> None:
    fetcher = _fetcher_returning(_ROBOTS_BODY_DISALLOW_PRIVATE)
    middleware = RobotsPolicyMiddleware(robots_fetcher=fetcher)

    middleware._decide_respect("https://shop.example.com/product/1", "price_monitor")
    middleware._decide_respect("https://shop.example.com/product/2", "price_monitor")

    assert fetcher.calls == ["https://shop.example.com/robots.txt"]  # type: ignore[attr-defined]


def test_respect_allows_everything_when_robots_txt_is_absent() -> None:
    """Fetch failure / no robots.txt (`None` body) -- conventional "absence means allow"."""
    fetcher = _fetcher_returning(None)
    middleware = RobotsPolicyMiddleware(robots_fetcher=fetcher)

    middleware._decide_respect("https://shop.example.com/anything", "price_monitor")


def test_process_request_offloads_respect_through_run_in_thread() -> None:
    """The reactor-safety contract: RESPECT's IO-bearing decision is
    dispatched via `run_in_thread` (never called synchronously inline
    on the reactor thread) -- verified by asserting `process_request`
    returns a Deferred rather than performing the fetch itself."""
    from twisted.internet.defer import Deferred

    fetcher = _fetcher_returning(_ROBOTS_BODY_DISALLOW_PRIVATE)
    middleware = RobotsPolicyMiddleware(robots_fetcher=fetcher)
    request = _FakeRequest(
        "https://shop.example.com/private/x",
        meta={"robots_policy": RobotsPolicy.RESPECT},
    )

    result = middleware.process_request(request, spider=None)

    assert isinstance(result, Deferred)
    # The fetch itself has not (yet) happened synchronously here -- it
    # only runs inside the offloaded thread once the reactor drives it.
    assert fetcher.calls == []  # type: ignore[attr-defined]


# --- IGNORE_AFTER_APPROVAL: fetches regardless, no robots.txt lookup -----------


def test_ignore_after_approval_fetches_without_any_robots_lookup() -> None:
    fetcher = _fetcher_returning(_ROBOTS_BODY_DISALLOW_PRIVATE)
    middleware = RobotsPolicyMiddleware(robots_fetcher=fetcher)
    request = _FakeRequest(
        "https://shop.example.com/private/x",
        meta={"robots_policy": RobotsPolicy.IGNORE_AFTER_APPROVAL},
    )

    result = middleware.process_request(request, spider=None)

    assert result is None
    assert fetcher.calls == []  # type: ignore[attr-defined]


# --- REVIEW_REQUIRED: conservative skip, no robots.txt lookup ------------------


def test_review_required_is_blocked_without_any_robots_lookup() -> None:
    fetcher = _fetcher_returning(_ROBOTS_BODY_DISALLOW_PRIVATE)
    middleware = RobotsPolicyMiddleware(robots_fetcher=fetcher)
    request = _FakeRequest(
        "https://shop.example.com/product/1",
        meta={"robots_policy": RobotsPolicy.REVIEW_REQUIRED},
    )

    with pytest.raises(RobotsBlockedError):
        middleware.process_request(request, spider=None)

    assert fetcher.calls == []  # type: ignore[attr-defined]


# --- policy is read per-request from config, not a global toggle ---------------


def test_policy_is_read_per_request_not_global() -> None:
    """Two competitors, two policies, one middleware instance, one run:
    the same middleware makes a different decision per request based
    solely on that request's own `meta["robots_policy"]`."""
    fetcher = _fetcher_returning(_ROBOTS_BODY_DISALLOW_PRIVATE)
    middleware = RobotsPolicyMiddleware(robots_fetcher=fetcher)

    ignoring_request = _FakeRequest(
        "https://competitor-a.example.com/private/x",
        meta={"robots_policy": RobotsPolicy.IGNORE_AFTER_APPROVAL},
    )
    respecting_request = _FakeRequest(
        "https://competitor-b.example.com/private/x",
        meta={"robots_policy": RobotsPolicy.RESPECT},
    )

    assert middleware.process_request(ignoring_request, spider=None) is None

    with pytest.raises(RobotsBlockedError):
        middleware._decide_respect(respecting_request.url, "price_monitor")


def test_missing_meta_defaults_to_conservative_respect() -> None:
    """A request with no explicit `robots_policy` in meta defaults to the
    same conservative `RESPECT` the `Competitor` model itself defaults to."""
    fetcher = _fetcher_returning(_ROBOTS_BODY_DISALLOW_PRIVATE)
    middleware = RobotsPolicyMiddleware(robots_fetcher=fetcher)
    request = _FakeRequest("https://shop.example.com/private/x", meta={})

    from twisted.internet.defer import Deferred

    result = middleware.process_request(request, spider=None)

    # RESPECT is the only branch that offloads via run_in_thread.
    assert isinstance(result, Deferred)
