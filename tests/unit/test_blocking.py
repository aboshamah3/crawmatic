"""`scrape_core.blocking` — bot-interstitial-served-as-200 detection.

Per the module docstring: Amazon answers a flagged fetch with HTTP 200 and
a ~5 KB CAPTCHA page, which used to die terminal `PRICE_NOT_FOUND` with no
retry. These tests pin the three properties that matter: a marker + small
body is a block, a marker inside a full-size page is NOT (a real page may
quote the phrase), and the middleware raises so Scrapy routes it to the
spider's errback classified as BLOCKED.
"""

from __future__ import annotations

import pytest

from scrape_core.blocking import (
    BlockDetectionMiddleware,
    BlockedResponseError,
    looks_blocked,
)
from scrape_core.errors import BLOCKED, classify_exception

_CAPTCHA = (
    b"<html><head><title>Amazon.sa</title></head><body>"
    b"<h4>Enter the characters you see below</h4>"
    b"<p>Sorry, we just need to make sure you're not a robot.</p>"
    b"<form action='/errors/validateCaptcha'></form>"
    b"<p>To discuss automated access to Amazon data please contact...</p>"
    b"</body></html>"
)


def test_marker_plus_small_body_is_blocked() -> None:
    assert looks_blocked(_CAPTCHA) is True


def test_marker_in_a_full_size_page_is_not_blocked() -> None:
    """A real product page that merely quotes the phrase must survive --
    the size test exists precisely so a marker alone can't discard one."""
    page = _CAPTCHA + b"x" * 200_000
    assert looks_blocked(page) is False


def test_ordinary_page_is_not_blocked() -> None:
    assert looks_blocked(b"<html><body><span class='a-offscreen'>SAR201.62</span></body></html>") is False


def test_str_body_accepted() -> None:
    assert looks_blocked(_CAPTCHA.decode()) is True


class _Resp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body


class _Req:
    url = "https://www.amazon.sa/dp/B001QBBOLQ"


def test_middleware_raises_on_interstitial() -> None:
    mw = BlockDetectionMiddleware()
    with pytest.raises(BlockedResponseError):
        mw.process_response(_Req(), _Resp(200, _CAPTCHA), None)


def test_middleware_passes_real_page_through() -> None:
    mw = BlockDetectionMiddleware()
    response = _Resp(200, b"<html>real product page</html>")
    assert mw.process_response(_Req(), response, None) is response


def test_middleware_ignores_non_200() -> None:
    """Non-2xx already has an owner (`classify_http_status`) -- this
    middleware must not steal a 503 and turn it into a BLOCKED."""
    mw = BlockDetectionMiddleware()
    response = _Resp(503, _CAPTCHA)
    assert mw.process_response(_Req(), response, None) is response


def test_blocked_error_classifies_as_blocked() -> None:
    """The spiders' errback runs `classify_exception` on whatever Scrapy
    hands it -- the explicit `error_code` attribute is what makes this
    surface as BLOCKED (a retryable transport failure) rather than
    UNKNOWN_ERROR."""
    assert classify_exception(BlockedResponseError("blocked")) is BLOCKED


class _HttpErrorLike(Exception):
    """Shape of Scrapy's `HttpError`: carries the filtered `.response`."""

    def __init__(self, status: int) -> None:
        super().__init__("Ignoring non-200 response")
        self.response = type("R", (), {"status": status})()


@pytest.mark.parametrize(
    "status,expected_name",
    [(429, "HTTP_429"), (403, "HTTP_403"), (404, "HTTP_404"), (503, "UNKNOWN_ERROR")],
)
def test_http_error_classifies_by_real_status(status: int, expected_name: str) -> None:
    """Scrapy filters every non-2xx to the errback as HttpError, so
    `parse`'s classify_http_status branch never runs -- these used to all
    land as UNKNOWN_ERROR (717 S-Tech throttle responses on 2026-08-03)."""
    assert classify_exception(_HttpErrorLike(status)).name == expected_name


def test_non_http_exception_still_unknown() -> None:
    assert classify_exception(Exception("boom")).name == "UNKNOWN_ERROR"
