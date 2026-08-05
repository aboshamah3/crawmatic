"""Unit tests for the Chrome-impersonating download handler
(`scrape_core.impersonate`, 2026-08-05).

The handler exists because amazon.sa and noon.com reject the
Scrapy/Twisted HTTP client itself (not our headers, not our IPs), so
those two domains -- and only those -- are fetched through ``curl_cffi``
with a real Chrome TLS fingerprint. These tests pin the four behaviours
that a regression would silently break:

* routing (only the configured domains divert; everything else is
  delegated to the wrapped ``HTTP11DownloadHandler`` untouched),
* the proxy credential round-trip (Scrapy carries them in a
  ``Proxy-Authorization`` header, curl_cffi wants them in the URL),
* error classification (the retry ladder keys off exception class
  *names*), and
* response-header sanitisation (a decoded body must not keep its
  ``Content-Encoding``, or ``HttpCompressionMiddleware`` decodes twice).
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from typing import Any

import pytest

from scrapy.http import HtmlResponse, Request

from app_shared.enums import ScrapeErrorCode

import scrape_core.impersonate as imp
from scrape_core.errors import classify_exception

DOMAINS = imp._parse_domains("amazon.sa,noon.com")


# --------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.amazon.sa/dp/B071GLC1DR", True),
        ("https://amazon.sa/dp/X", True),
        ("https://www.noon.com/saudi-en/x/p/", True),
        ("https://deep.sub.noon.com/x", True),
        # Dot-anchored: a domain that merely *ends with* the configured
        # string must not divert.
        ("https://notamazon.sa/dp/X", False),
        ("https://jarir.com/product", False),
        ("https://extra.com/p", False),
    ],
)
def test_domain_routing_is_dot_anchored(url: str, expected: bool) -> None:
    assert imp.should_impersonate(Request(url), DOMAINS) is expected


def test_meta_override_wins_in_both_directions() -> None:
    """`meta["impersonate"]` beats the domain list either way."""
    off = Request("https://www.noon.com/x", meta={"impersonate": False})
    on = Request("https://jarir.com/x", meta={"impersonate": True})
    assert imp.should_impersonate(off, DOMAINS) is False
    assert imp.should_impersonate(on, DOMAINS) is True


def test_empty_domain_config_diverts_nothing() -> None:
    assert imp.should_impersonate(Request("https://www.amazon.sa/x"), ()) is False


# --------------------------------------------------------------------
# Proxy credential round-trip
# --------------------------------------------------------------------
def _request_with_proxy_auth(username: str, password: str) -> Request:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return Request(
        "https://www.noon.com/x",
        meta={"proxy": "http://gw.dataimpulse.com:823"},
        headers={"Proxy-Authorization": f"Basic {token}"},
    )


def test_proxy_url_reassembles_credentials_from_the_header() -> None:
    """The spider keeps creds out of meta["proxy"]; curl needs them in it."""
    req = _request_with_proxy_auth("login__cr.sa;sessid.abc123", "s3cret")
    assert imp._proxy_url(req) == (
        "http://login__cr.sa;sessid.abc123:s3cret@gw.dataimpulse.com:823"
    )


def test_proxy_url_without_auth_header_is_passed_through() -> None:
    req = Request("https://www.noon.com/x", meta={"proxy": "http://gw:823"})
    assert imp._proxy_url(req) == "http://gw:823"


def test_no_proxy_meta_means_no_proxy() -> None:
    assert imp._proxy_url(Request("https://www.noon.com/x")) is None


def test_unparseable_credentials_degrade_to_the_bare_proxy() -> None:
    """A bad credential must not raise (and must not be logged)."""
    req = Request(
        "https://www.noon.com/x",
        meta={"proxy": "http://gw:823"},
        headers={"Proxy-Authorization": "Basic !!!not-base64!!!"},
    )
    assert imp._proxy_url(req) == "http://gw:823"


# --------------------------------------------------------------------
# Error classification -- `classify_exception` keys off the class NAME,
# so these assertions are what make the retry ladder behave.
# --------------------------------------------------------------------
@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (28, "Operation timed out after 30000ms", ScrapeErrorCode.TIMEOUT),
        (5, "Could not resolve proxy: gw.dataimpulse.com", ScrapeErrorCode.PROXY_FAILED),
        (97, "Proxy CONNECT aborted", ScrapeErrorCode.PROXY_FAILED),
        (6, "Could not resolve host: noon.com", ScrapeErrorCode.DNS_ERROR),
        (92, "HTTP/2 stream 1 reset by server", ScrapeErrorCode.UNKNOWN_ERROR),
    ],
)
def test_curl_errors_classify_through_the_existing_ladder(
    code: int, message: str, expected: ScrapeErrorCode
) -> None:
    raw = SimpleNamespace(code=code)
    exc = imp._wrap_error(type("RequestsError", (Exception,), {})(message))
    # `_wrap_error` reads `.code` when present; simulate both paths.
    exc_with_code = imp._wrap_error(
        type("RequestsError", (Exception,), {"code": raw.code})(message)
    )
    assert classify_exception(exc_with_code) is expected
    # Message-only fallback still classifies the three specific cases.
    if expected is not ScrapeErrorCode.UNKNOWN_ERROR:
        assert classify_exception(exc) is expected


# --------------------------------------------------------------------
# Response construction
# --------------------------------------------------------------------
def test_content_encoding_and_length_are_stripped() -> None:
    """curl_cffi returns a DECODED body -- keeping these double-decodes it."""
    raw = {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Encoding": "gzip",
        "Content-Length": "1234",
        "Set-Cookie": "a=b",
    }
    headers = imp._response_headers(raw)
    assert "Content-Encoding" not in headers
    assert "Content-Length" not in headers
    assert headers["Content-Type"] == [b"text/html; charset=utf-8"]


def test_multi_valued_headers_are_preserved() -> None:
    """Several Set-Cookie lines must survive as separate values."""

    class _MultiHeaders:
        def multi_items(self) -> list[tuple[str, str]]:
            return [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")]

    assert imp._response_headers(_MultiHeaders())["Set-Cookie"] == [b"a=1", b"b=2"]


# --------------------------------------------------------------------
# Handler wiring
# --------------------------------------------------------------------
async def _already_done(value: Any) -> Any:
    """Stand-in for `as_awaitable` when the Deferred seam is stubbed out."""
    return value


class _StubFallback:
    """Stands in for the wrapped HTTP11DownloadHandler."""

    def __init__(self) -> None:
        self.calls: list[Request] = []
        self.closed = False

    async def download_request(self, request: Request) -> HtmlResponse:
        self.calls.append(request)
        return HtmlResponse(url=request.url, body=b"<html>delegated</html>")

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def handler(monkeypatch: pytest.MonkeyPatch) -> Any:
    stub = _StubFallback()
    monkeypatch.setattr(
        imp.HTTP11DownloadHandler, "from_crawler", classmethod(lambda cls, crawler: stub)
    )
    settings = {
        "SCRAPE_IMPERSONATE_DOMAINS": "amazon.sa,noon.com",
        "SCRAPE_IMPERSONATE_PROFILE": "chrome131",
    }
    h = imp.ImpersonatingDownloadHandler(
        SimpleNamespace(
            get=lambda k, d=None: settings.get(k, d),
            getfloat=lambda k, d=None: 180.0,
        )
    )
    h._stub = stub  # type: ignore[attr-defined]
    return h


def test_healthy_sites_are_delegated_untouched(handler: Any) -> None:
    """The eight healthy competitors must keep their exact current path."""
    req = Request("https://jarir.com/product")
    resp = asyncio.run(handler.download_request(req))
    assert handler._stub.calls == [req]
    assert resp.body == b"<html>delegated</html>"


def test_configured_domain_takes_the_curl_path(
    handler: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Request] = []

    def _fake_fetch(request: Request) -> HtmlResponse:
        seen.append(request)
        return HtmlResponse(url=request.url, body=b"<html>impersonated</html>")

    monkeypatch.setattr(handler, "_fetch", _fake_fetch)
    # The real path offloads through Twisted's thread pool, which needs a
    # running reactor; stub the seam so this stays a pure unit test. The
    # live proxied run is what exercises the real deferToThread path.
    monkeypatch.setattr(imp, "run_in_thread", lambda fn, *a: fn(*a))
    monkeypatch.setattr(imp, "as_awaitable", _already_done)
    req = Request("https://www.amazon.sa/dp/B071GLC1DR")
    resp = asyncio.run(handler.download_request(req))
    assert seen == [req]
    assert handler._stub.calls == []  # never touched the Twisted client
    assert resp.body == b"<html>impersonated</html>"


def test_close_closes_the_wrapped_handler(handler: Any) -> None:
    asyncio.run(handler.close())
    assert handler._stub.closed is True
