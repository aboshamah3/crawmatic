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


# --- noon.com / Akamai Bot Manager (2026-08-05) ----------------------------

#: Trimmed from a real noon.com challenge captured through the production SA
#: proxy on 2026-08-05 (~2.5 KB, served at HTTP 200 on a product URL).
_NOON_AKAMAI_CHALLENGE = """<!DOCTYPE html><html><head><title></title></head><body>
<div class="sec-bc-button-parent">
  <div class="behavioral-button progress-btn-disabled">
    <div class="btn" id="progress-button" role="button" disabled></div>
  </div>
</div>
<div class="scf-akamai-logo-sec-abc">
  <p class="scf-akamai-protected-by">Powered and protected by</p>
  <img src="https://www.akamai.com/site/ko/images/logo/akamai-logo1.svg" class="scf-akamai-logo">
</div>
</body></html>"""


def test_noon_akamai_challenge_is_detected() -> None:
    """noon serves this at HTTP 200 instead of the product page.

    Undetected it reaches extraction, finds no price and dies terminal
    PRICE_NOT_FOUND with no retry -- which is what every noon failure in
    job 279b32fd actually was. Detected, it becomes a retryable BLOCKED
    and the existing ladder re-fetches on a fresh exit.
    """
    assert looks_blocked(_NOON_AKAMAI_CHALLENGE.encode()) is True


def test_real_noon_product_page_mentioning_akamai_is_not_blocked() -> None:
    """The size guard is what makes the Akamai markers safe.

    noon's real product pages are 430-540 KB and legitimately reference
    Akamai (it is their CDN), so a marker alone must never condemn a page.
    """
    big_page = (
        '<html><body><script src="https://cdn.akamai.com/x.js"></script>'
        '<p>Powered and protected by Akamai</p>'
        + "<div>product copy</div>" * 8000
        + "</body></html>"
    )
    assert len(big_page.encode()) > 60_000
    assert looks_blocked(big_page.encode()) is False
