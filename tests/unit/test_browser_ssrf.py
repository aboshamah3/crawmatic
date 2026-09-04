"""``scrape_core.browser.ssrf.abort_unsafe_request`` unit tests (SPEC-14
T030, US4, `contracts/browser-safety.md` "SSRF").

Pure/off-reactor -- no Chromium, no real browser, no Twisted reactor.
Drives `abort_unsafe_request` directly via `asyncio.run` (it is a native
coroutine, never a Twisted `Deferred`) with a fake Playwright-request-shaped
object and an injected fake resolver (never real DNS), mirroring
`tests/unit/test_fetch_url_safety.py`'s own injected-resolver convention.
"""

from __future__ import annotations

import asyncio

import pytest

from scrape_core.browser.ssrf import abort_unsafe_request
from scrape_core.safety.rejection_registry import was_recently_rejected

_PUBLIC_IP = "93.184.216.34"  # real, globally-routable IPv4 -- never actually
# dialed here (the injected resolver is a fake returning a canned string; no
# socket is ever opened for this address).


class _FakeRequest:
    """Stands in for a `playwright.async_api.Request` -- the only attributes
    `abort_unsafe_request`/`_is_navigation_request` ever touch."""

    def __init__(
        self,
        url: str,
        *,
        is_navigation: bool = True,
        resource_type: str = "document",
    ) -> None:
        self.url = url
        self._is_navigation = is_navigation
        self.resource_type = resource_type

    def is_navigation_request(self) -> bool:
        return self._is_navigation


def _resolver(ips: list[str]):
    calls: list[str] = []

    def resolve(host: str) -> list[str]:
        calls.append(host)
        return ips

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def _raising_resolver(exc: Exception):
    def resolve(host: str) -> list[str]:
        raise exc

    return resolve


# --- navigation-vs-subresource gating ---------------------------------------


def test_subresource_request_passes_without_any_resolver_call() -> None:
    """A non-navigation resource outside the cost-blocked set (script/xhr/...)
    is never checked -- `is_navigation_request()` False AND `resource_type`
    not "document"."""
    resolver = _resolver(["10.0.0.5"])  # would be rejected if ever consulted
    for resource_type in ("script", "xhr", "fetch", "other"):
        request = _FakeRequest(
            "https://shop.example.com/app.js",
            is_navigation=False,
            resource_type=resource_type,
        )

        result = asyncio.run(abort_unsafe_request(request, resolver=resolver))

        assert result is False
        assert resolver.calls == []  # type: ignore[attr-defined]  -- no resolve attempted


def test_heavy_asset_subresource_aborted_without_resolver_or_registry() -> None:
    """image/media/font/stylesheet sub-resources are aborted for proxy cost
    (PLAN_AMAZON_NOON_PRICING Phase 2) -- no resolve attempted, and the
    rejection registry is NEVER marked (an asset abort must not make the
    page classify as BLOCKED)."""
    from scrape_core.safety import rejection_registry

    resolver = _resolver([_PUBLIC_IP])
    for resource_type in ("image", "media", "font", "stylesheet"):
        request = _FakeRequest(
            "https://shop.example.com/logo.png",
            is_navigation=False,
            resource_type=resource_type,
        )

        result = asyncio.run(abort_unsafe_request(request, resolver=resolver))

        assert result is True, resource_type
        assert resolver.calls == []  # type: ignore[attr-defined]
        assert not rejection_registry.was_recently_rejected("shop.example.com")


def test_navigation_request_via_is_navigation_request_is_checked() -> None:
    """`is_navigation_request()` True (regardless of `resource_type`) is enough
    to trigger the resolved-IP check."""
    resolver = _resolver([_PUBLIC_IP])
    request = _FakeRequest(
        "https://shop.example.com/product/1", is_navigation=True, resource_type="other"
    )

    result = asyncio.run(abort_unsafe_request(request, resolver=resolver))

    assert result is False
    assert resolver.calls == ["shop.example.com"]  # type: ignore[attr-defined]


def test_document_resource_type_without_is_navigation_method_is_checked() -> None:
    """`resource_type == "document"` alone (even if `is_navigation_request()`
    reports False) is also treated as navigation -- the contract's OR gate."""
    resolver = _resolver([_PUBLIC_IP])
    request = _FakeRequest(
        "https://shop.example.com/product/1", is_navigation=False, resource_type="document"
    )

    result = asyncio.run(abort_unsafe_request(request, resolver=resolver))

    assert result is False
    assert resolver.calls == ["shop.example.com"]  # type: ignore[attr-defined]


# --- safe/unsafe decision ----------------------------------------------------


def test_navigation_to_safe_public_ip_is_not_aborted() -> None:
    resolver = _resolver([_PUBLIC_IP])
    request = _FakeRequest("https://shop.example.com/product/1")

    result = asyncio.run(abort_unsafe_request(request, resolver=resolver))

    assert result is False


@pytest.mark.parametrize(
    "label,resolved_ip",
    [
        ("private", "10.0.0.5"),
        ("loopback", "127.0.0.1"),
        ("link_local", "169.254.1.1"),
        ("cloud_metadata", "169.254.169.254"),
    ],
)
def test_navigation_to_unsafe_resolved_ip_is_aborted(label: str, resolved_ip: str) -> None:
    resolver = _resolver([resolved_ip])
    request = _FakeRequest("https://shop.example.com/product/1")

    result = asyncio.run(abort_unsafe_request(request, resolver=resolver))

    assert result is True, label


def test_ip_literal_host_is_aborted_before_any_resolver_call() -> None:
    """The reused save-time IP-literal deny check (inside
    `validate_resolved_target` -> `validate_competitor_url`) rejects a raw
    unsafe IP in the URL itself -- no resolver consultation needed."""
    resolver = _resolver([_PUBLIC_IP])
    request = _FakeRequest("http://127.0.0.1/product/1")

    result = asyncio.run(abort_unsafe_request(request, resolver=resolver))

    assert result is True
    assert resolver.calls == []  # type: ignore[attr-defined]


def test_resolver_error_fails_closed_and_aborts() -> None:
    """A resolution error (DNS failure, malformed address, ...) is never
    silently treated as safe -- fail-closed, matches the module's fail-closed
    posture documented in `abort_unsafe_request`."""
    resolver = _raising_resolver(OSError("simulated DNS failure"))
    request = _FakeRequest("https://shop.example.com/product/1")

    result = asyncio.run(abort_unsafe_request(request, resolver=resolver))

    assert result is True


# --- each redirect hop is independently re-validated ------------------------


def test_each_redirect_hop_is_independently_revalidated() -> None:
    """Mirrors `test_fetch_url_safety.py`'s per-hop test: scrapy-playwright
    calls `abort_unsafe_request` once per navigation hop, each judged on its
    own merits -- a safe first hop grants the next hop no pass."""
    hop1 = _FakeRequest("https://shop.example.com/")
    hop1_result = asyncio.run(abort_unsafe_request(hop1, resolver=_resolver([_PUBLIC_IP])))
    assert hop1_result is False

    hop2 = _FakeRequest("https://shop.example.com/redirected-internal")  # public host 302s to an internal IP
    hop2_result = asyncio.run(abort_unsafe_request(hop2, resolver=_resolver(["10.0.0.5"])))
    assert hop2_result is True


# --- rejection_registry side-channel (so `classify_exception`/
# `classify_browser_failure` can later recognize the abort as BLOCKED) ------


def test_aborting_marks_the_hostname_in_the_rejection_registry() -> None:
    """`abort_unsafe_request` must mark the rejected hostname via
    `rejection_registry.mark_rejected` -- the only surviving signal once
    scrapy-playwright's `route.abort()` discards the real rejection reason
    at the Chromium network layer (module docstring)."""
    request = _FakeRequest("https://blocked-host.example.com/product/1")

    result = asyncio.run(abort_unsafe_request(request, resolver=_resolver(["10.0.0.5"])))

    assert result is True
    assert was_recently_rejected("blocked-host.example.com") is True


def test_safe_navigation_does_not_mark_the_rejection_registry() -> None:
    request = _FakeRequest("https://never-rejected-host.example.com/product/1")

    result = asyncio.run(abort_unsafe_request(request, resolver=_resolver([_PUBLIC_IP])))

    assert result is False
    assert was_recently_rejected("never-rejected-host.example.com") is False


# --- domain_profile_registry wiring (EPA B6b) --------------------------------


def test_subresource_uses_registered_domain_profile_to_allow_a_certified_type() -> None:
    """A sub-resource `resource_type` the default policy would otherwise
    block (image/media/font/stylesheet) passes once its host's real
    profile -- recovered from `domain_profile_registry`, exactly as
    `generic_browser_price_spider._browser_request_for` populates it at
    dispatch time -- certifies that type."""
    from types import SimpleNamespace

    from scrape_core.browser.domain_profile_registry import (
        clear_domain_profile,
        set_domain_profile,
    )

    set_domain_profile(
        "certified-host.example.com", SimpleNamespace(certified_resources={"image"})
    )
    try:
        request = _FakeRequest(
            "https://certified-host.example.com/hero.jpg",
            is_navigation=False,
            resource_type="image",
        )
        result = asyncio.run(abort_unsafe_request(request, resolver=_resolver([_PUBLIC_IP])))
        assert result is False
    finally:
        clear_domain_profile("certified-host.example.com")


def test_subresource_on_an_unregistered_host_still_uses_the_default_policy() -> None:
    """A host with no `domain_profile_registry` entry at all (never
    dispatched in this process) must fall back to the plain default
    policy -- fail-closed to blocking, never to allowing -- exactly as it
    did before B6b's wiring landed."""
    request = _FakeRequest(
        "https://unregistered-host.example.com/hero.jpg",
        is_navigation=False,
        resource_type="image",
    )

    result = asyncio.run(abort_unsafe_request(request, resolver=_resolver([_PUBLIC_IP])))

    assert result is True  # image is still blocked by default -- no certification found


# --- EPA B5: document-only proxied browser legs for LISTED domains -----------
#
# `abort_unsafe_request` IS the real Playwright route handler for this
# fleet: scrapy-playwright wires it as `PLAYWRIGHT_ABORT_REQUEST` and its
# own handler does exactly `route.abort()` when it returns True and
# `route.continue_()` otherwise
# (`scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler._make_request_handler`).
# `_drive_route` below reproduces that one branch so these tests assert the
# ACTUAL route action, not just the boolean.


class _FakeRoute:
    def __init__(self) -> None:
        self.actions: list[str] = []

    async def abort(self) -> None:
        self.actions.append("abort")

    async def continue_(self) -> None:
        self.actions.append("continue_")


class _FakePageRequest(_FakeRequest):
    """A `_FakeRequest` that also exposes `request.frame.url` -- the signal
    `abort_unsafe_request` uses to recover the PAGE's domain for a
    cross-origin sub-resource (an Amazon page's images live on
    `m.media-amazon.com`, which would never match a listed `amazon.sa`)."""

    def __init__(self, url: str, *, page_url: str, **kwargs) -> None:
        super().__init__(url, **kwargs)
        from types import SimpleNamespace

        self.frame = SimpleNamespace(url=page_url)


def _drive_route(request, *, resolver=None):
    """Run the scrapy-playwright route branch; returns the fake route."""
    route = _FakeRoute()

    async def handler() -> None:
        if await abort_unsafe_request(request, resolver=resolver or _resolver([_PUBLIC_IP])):
            await route.abort()
        else:
            await route.continue_()

    asyncio.run(handler())
    return route


@pytest.fixture
def _amazon_listed_and_proxied(monkeypatch):
    """`amazon.sa` listed AND its leg recorded as PROXY, exactly as the
    spider records it at dispatch."""
    from app_shared.profiles import browser_resource_policy
    from types import SimpleNamespace

    from scrape_core.browser.domain_profile_registry import (
        clear_domain_transport,
        set_domain_transport,
    )

    monkeypatch.setattr(
        browser_resource_policy,
        "get_settings",
        lambda: SimpleNamespace(BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS=("amazon.sa",)),
    )
    set_domain_transport("www.amazon.sa", "PROXY")
    try:
        yield
    finally:
        clear_domain_transport("www.amazon.sa")


def test_proxied_listed_domain_script_subresource_is_aborted(_amazon_listed_and_proxied):
    request = _FakePageRequest(
        "https://www.amazon.sa/bundle.js",
        page_url="https://www.amazon.sa/dp/B0ABC",
        is_navigation=False,
        resource_type="script",
    )

    assert _drive_route(request).actions == ["abort"]


def test_proxied_listed_domain_cross_origin_subresource_is_aborted(
    _amazon_listed_and_proxied,
):
    # The sub-resource's OWN host (`m.media-amazon.com`) is not the listed
    # domain -- the page's is. Matching on the page domain is what makes
    # the rule bite at all.
    request = _FakePageRequest(
        "https://m.media-amazon.com/images/I/hero.js",
        page_url="https://www.amazon.sa/dp/B0ABC",
        is_navigation=False,
        resource_type="script",
    )

    assert _drive_route(request).actions == ["abort"]


def test_proxied_listed_domain_document_is_continued(_amazon_listed_and_proxied):
    request = _FakePageRequest(
        "https://www.amazon.sa/dp/B0ABC",
        page_url="https://www.amazon.sa/dp/B0ABC",
        is_navigation=True,
        resource_type="document",
    )

    assert _drive_route(request).actions == ["continue_"]


def test_direct_leg_of_a_listed_domain_keeps_the_pre_change_decision(monkeypatch):
    # Same page, same script -- but the leg was never recorded as proxied,
    # so the registry's DIRECT default applies and the pre-B5 decision
    # (script allowed) stands.
    from app_shared.profiles import browser_resource_policy
    from types import SimpleNamespace

    monkeypatch.setattr(
        browser_resource_policy,
        "get_settings",
        lambda: SimpleNamespace(BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS=("amazon.sa",)),
    )
    request = _FakePageRequest(
        "https://www.amazon.sa/bundle.js",
        page_url="https://www.amazon.sa/dp/B0ABC",
        is_navigation=False,
        resource_type="script",
    )

    assert _drive_route(request).actions == ["continue_"]


def test_default_settings_produce_the_pre_change_decision_on_a_proxied_leg(monkeypatch):
    # The shipped default (`BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS == ()`):
    # a proxied Amazon leg behaves byte-for-byte as it did before B5 --
    # script continues, image still aborts on the default type blocklist.
    from app_shared.profiles import browser_resource_policy
    from types import SimpleNamespace

    from scrape_core.browser.domain_profile_registry import (
        clear_domain_transport,
        set_domain_transport,
    )

    monkeypatch.setattr(
        browser_resource_policy,
        "get_settings",
        lambda: SimpleNamespace(BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS=()),
    )
    set_domain_transport("www.amazon.sa", "PROXY")
    try:
        script = _FakePageRequest(
            "https://www.amazon.sa/bundle.js",
            page_url="https://www.amazon.sa/dp/B0ABC",
            is_navigation=False,
            resource_type="script",
        )
        image = _FakePageRequest(
            "https://www.amazon.sa/hero.jpg",
            page_url="https://www.amazon.sa/dp/B0ABC",
            is_navigation=False,
            resource_type="image",
        )

        assert _drive_route(script).actions == ["continue_"]
        assert _drive_route(image).actions == ["abort"]
    finally:
        clear_domain_transport("www.amazon.sa")


def test_request_without_a_frame_falls_back_to_its_own_host(_amazon_listed_and_proxied):
    # A Playwright request whose `frame` is unavailable (service worker,
    # or any stand-in that never had one) must not raise -- the handler
    # falls back to the request's own hostname, which for a same-origin
    # sub-resource is still the listed domain.
    request = _FakeRequest(
        "https://www.amazon.sa/bundle.js",
        is_navigation=False,
        resource_type="script",
    )

    assert _drive_route(request).actions == ["abort"]
