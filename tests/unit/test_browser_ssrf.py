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
    """A non-navigation resource (image/script/xhr/...) is never checked --
    `is_navigation_request()` False AND `resource_type` not "document"."""
    resolver = _resolver(["10.0.0.5"])  # would be rejected if ever consulted
    request = _FakeRequest(
        "https://shop.example.com/logo.png", is_navigation=False, resource_type="image"
    )

    result = asyncio.run(abort_unsafe_request(request, resolver=resolver))

    assert result is False
    assert resolver.calls == []  # type: ignore[attr-defined]  -- no resolve attempted


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
