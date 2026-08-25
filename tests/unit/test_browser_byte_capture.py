"""Unit tests for `scrape_core.browser.byte_capture.ByteAccumulator` (EPA B6b).

Pure/off-reactor -- no real Chromium/Playwright anywhere in this file.
Drives `ByteAccumulator.handle_response` directly via `asyncio.run` with
fake Playwright-``Response``-shaped objects, mirroring
`test_browser_ssrf.py`'s own fake-request convention. Covers the
transport-observed contract from the module docstring: Content-Length
header first, `response.body()` length as fallback, "not measured" (never
fabricated) when neither works, and `finalize()`'s NULL-vs-zero
distinction.
"""

from __future__ import annotations

import asyncio

import pytest

from scrape_core.browser.byte_capture import ByteAccumulator


class _FakeRequest:
    def __init__(self, resource_type: str) -> None:
        self.resource_type = resource_type


class _FakeResponse:
    """Stands in for a `playwright.async_api.Response` -- only the
    attributes/methods `ByteAccumulator` ever touches."""

    def __init__(
        self,
        resource_type: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        body_raises: bool = False,
    ) -> None:
        self.request = _FakeRequest(resource_type)
        self.headers = headers or {}
        self._body = body
        self._body_raises = body_raises

    async def body(self) -> bytes:
        if self._body_raises:
            raise RuntimeError("simulated: response body is unavailable for redirect responses")
        assert self._body is not None
        return self._body


# --- Content-Length header path -----------------------------------------------


def test_content_length_header_is_used_when_present():
    accumulator = ByteAccumulator()
    response = _FakeResponse("document", headers={"content-length": "1234"})

    asyncio.run(accumulator.handle_response(response))

    main_bytes, sub_bytes = accumulator.finalize()
    assert main_bytes == 1234
    assert sub_bytes == 0


def test_non_numeric_content_length_falls_back_to_body_size():
    accumulator = ByteAccumulator()
    response = _FakeResponse(
        "xhr", headers={"content-length": "not-a-number"}, body=b"0123456789"
    )

    asyncio.run(accumulator.handle_response(response))

    main_bytes, sub_bytes = accumulator.finalize()
    assert main_bytes == 0
    assert sub_bytes == 10


# --- body() fallback path -------------------------------------------------------


def test_body_size_used_when_no_content_length_header():
    accumulator = ByteAccumulator()
    response = _FakeResponse("script", headers={}, body=b"x" * 555)

    asyncio.run(accumulator.handle_response(response))

    main_bytes, sub_bytes = accumulator.finalize()
    assert main_bytes == 0
    assert sub_bytes == 555


def test_body_error_is_not_counted_and_never_raises():
    accumulator = ByteAccumulator()
    response = _FakeResponse("document", headers={}, body_raises=True)

    asyncio.run(accumulator.handle_response(response))  # must not raise

    # Nothing was successfully measured -- NULL, not a fabricated 0.
    assert accumulator.finalize() == (None, None)


# --- document vs sub-resource classification (via split_attempt_bytes) --------


def test_document_and_subresources_are_split_correctly():
    accumulator = ByteAccumulator()
    for response in (
        _FakeResponse("document", headers={"content-length": "1000"}),
        _FakeResponse("script", headers={"content-length": "200"}),
        _FakeResponse("xhr", headers={"content-length": "300"}),
    ):
        asyncio.run(accumulator.handle_response(response))

    main_bytes, sub_bytes = accumulator.finalize()
    assert main_bytes == 1000
    assert sub_bytes == 500


def test_multiple_responses_of_the_same_type_accumulate():
    accumulator = ByteAccumulator()
    for response in (
        _FakeResponse("document", headers={"content-length": "500"}),  # redirect hop
        _FakeResponse("document", headers={"content-length": "1500"}),  # final document
    ):
        asyncio.run(accumulator.handle_response(response))

    main_bytes, sub_bytes = accumulator.finalize()
    assert main_bytes == 2000
    assert sub_bytes == 0


# --- finalize(): NULL vs "measured, zero bytes" --------------------------------


def test_finalize_with_no_responses_seen_is_null_not_zero():
    accumulator = ByteAccumulator()
    assert accumulator.finalize() == (None, None)


def test_finalize_with_a_zero_byte_response_is_zero_not_null():
    accumulator = ByteAccumulator()
    response = _FakeResponse("document", headers={"content-length": "0"})

    asyncio.run(accumulator.handle_response(response))

    assert accumulator.finalize() == (0, 0)


# --- never raises out of the listener -------------------------------------------


def test_missing_resource_type_attribute_degrades_to_other_and_never_raises():
    class _BareRequest:
        pass

    class _BareResponse:
        def __init__(self) -> None:
            self.request = _BareRequest()
            self.headers = {"content-length": "42"}

        async def body(self) -> bytes:  # pragma: no cover - not reached (header wins)
            raise AssertionError("should not be called")

    accumulator = ByteAccumulator()
    asyncio.run(accumulator.handle_response(_BareResponse()))  # must not raise

    main_bytes, sub_bytes = accumulator.finalize()
    # "other" is neither "document" -- classified as a sub-resource by
    # `split_attempt_bytes`.
    assert main_bytes == 0
    assert sub_bytes == 42


def test_headers_access_failure_never_raises():
    class _ExplodingHeaders:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("simulated header access failure")

    class _ExplodingHeadersResponse:
        def __init__(self) -> None:
            self.request = _FakeRequest("document")

        @property
        def headers(self):
            raise RuntimeError("simulated: headers unavailable")

        async def body(self) -> bytes:
            return b"abc"

    accumulator = ByteAccumulator()
    asyncio.run(accumulator.handle_response(_ExplodingHeadersResponse()))  # must not raise

    main_bytes, _ = accumulator.finalize()
    assert main_bytes == 3


@pytest.mark.parametrize("resource_type", ["image", "media", "font", "stylesheet"])
def test_a_blocked_resource_type_never_reaches_the_listener_in_practice(resource_type):
    """Documents (rather than exercises production wiring) that this is a
    non-issue in practice: `abort_unsafe_request` calls `route.abort()`
    for a blocked resource type before Chromium ever produces a
    `response` event, so this accumulator never even sees blocked
    traffic -- verified structurally here by confirming the accumulator
    itself has no special-casing for any resource type (it would still
    happily count one if handed one), which is exactly why the exclusion
    must happen upstream, not here."""
    accumulator = ByteAccumulator()
    response = _FakeResponse(resource_type, headers={"content-length": "999"})

    asyncio.run(accumulator.handle_response(response))

    main_bytes, sub_bytes = accumulator.finalize()
    assert main_bytes == 0
    assert sub_bytes == 999
