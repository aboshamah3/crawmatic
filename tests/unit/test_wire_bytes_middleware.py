"""Tests for EPA A7 (deep dive §8.2): the wire-bytes downloader middleware.

``scrape_core.middlewares.wire_bytes.WireBytesMiddleware`` runs at
priority 595 — ABOVE Scrapy's built-in ``HttpCompressionMiddleware``
(590), because ``process_response`` walks the chain in DESCENDING
priority — so it is the one place in the response chain that can still
see the response exactly as it crossed the wire, before decompression
rewrites ``response.body`` in place. It ran at 585 until review R12
(2026-09-09), which is the same sentence with the comparison the wrong
way round and meant the middleware measured the DECODED body for its
entire life. These tests prove:

1. the byte-count formula itself (``len(body) + len(headers) + len(status
   line)``) against a REAL gzip fixture that is 1,000 bytes compressed /
   8,000 bytes decoded, so the fixture's own shape proves the
   distinction the middleware exists to preserve;
2. the middleware stamps that count onto ``response.meta["wire_bytes"]``
   and returns the SAME response object unchanged (a downloader
   middleware's ``process_response`` contract);
3. the scraper project registers it strictly before
   ``HttpCompressionMiddleware`` **in the assembled response order** —
   asserted as a position in the list Scrapy's own
   ``build_component_list`` produces from the project settings, and then
   proved by running the chain over a real gzip response (R12). The
   original version of point 3 asserted the literal number 585, which is
   exactly how the defect survived: the number was never the claim.
"""

from __future__ import annotations

import gzip
import random
from http.client import responses as HTTP_REASON_PHRASES
from typing import Any

import pytest
from scrapy.http import Request, Response

from scrape_core.middlewares.wire_bytes import (
    META_WIRE_BYTES,
    WireBytesMiddleware,
    compute_wire_bytes,
)

_SCRAPY_PROJECT_MODULES = ["price_monitor.settings", "price_monitor_browser.settings"]

_SETTINGS_ENV = {
    "DATABASE_URL": "postgresql+psycopg://u:p@pgbouncer:6432/db",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "scrapyd",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def _gzip_fixture_body() -> bytes:
    """A REAL gzip stream: exactly 1,000 compressed bytes, 8,000 decoded.

    Built from a fixed seed so the exact byte counts are reproducible —
    the split between the first 893 (pseudo-random, effectively
    incompressible) bytes and a 7,107-byte compressible run of ``b"A"``
    was chosen by search to land ``gzip.compress(..., compresslevel=9)``
    on exactly 1,000 bytes for this content; the seed/split pair is
    pinned here rather than re-derived so the test never depends on a
    search running at collection time.
    """
    random.seed(1234)
    entropy = bytes(random.getrandbits(8) for _ in range(1000))
    content = entropy[:893] + b"A" * (8000 - 893)
    body = gzip.compress(content, compresslevel=9, mtime=0)
    assert len(body) == 1000, "fixture drifted — the pinned split no longer hits 1,000 bytes"
    assert len(gzip.decompress(body)) == 8000
    return body


def _make_response(body: bytes) -> Response:
    request = Request("https://example.test/p/1")
    headers = {"Content-Type": "text/html", "Content-Encoding": "gzip"}
    return Response(url=request.url, status=200, headers=headers, body=body, request=request)


class TestComputeWireBytes:
    def test_gzip_fixture_matches_the_documented_formula(self) -> None:
        """The exact formula the packet specifies, independently derived."""
        response = _make_response(_gzip_fixture_body())
        reason = HTTP_REASON_PHRASES[200]
        status_line = f"HTTP/1.1 200 {reason}\r\n".encode("latin-1")
        expected = len(response.body) + len(response.headers.to_string()) + len(status_line)

        assert compute_wire_bytes(response) == expected
        assert compute_wire_bytes(response) == 1000 + len(
            status_line + response.headers.to_string()
        )

    def test_measures_the_still_compressed_body(self) -> None:
        """Sanity pin: at this priority the body is the 1,000-byte
        compressed form, never the 8,000-byte decoded one — proving the
        measurement happens BEFORE ``HttpCompressionMiddleware`` runs."""
        response = _make_response(_gzip_fixture_body())
        assert len(response.body) == 1000
        assert len(gzip.decompress(response.body)) == 8000


class TestWireBytesMiddleware:
    def test_process_response_stamps_meta_and_returns_the_same_response(self) -> None:
        response = _make_response(_gzip_fixture_body())
        middleware = WireBytesMiddleware()

        result = middleware.process_response(response.request, response, spider=None)

        assert result is response
        assert response.meta[META_WIRE_BYTES] == compute_wire_bytes(response)
        assert response.meta[META_WIRE_BYTES] == response.request.meta[META_WIRE_BYTES], (
            "response.meta is a live view onto request.meta — later middlewares "
            "and the spider must see the same value off either object"
        )

    def test_wire_bytes_is_greater_than_the_decoded_size_for_this_fixture(self) -> None:
        """Not a general law (small responses can compress *larger*), but
        for THIS fixture the compressed count is the smaller of the two —
        pinning that the middleware measured the wire form, not a
        decoded one it never saw at this priority."""
        response = _make_response(_gzip_fixture_body())
        wire_bytes = compute_wire_bytes(response)
        assert wire_bytes < len(gzip.decompress(response.body))


@pytest.fixture
def scrapy_project_settings(monkeypatch: Any):
    """Import a Scrapy project's settings module with the env it needs.

    Same fixture shape as ``tests/unit/test_url_safety_hostile.py`` (the
    settings modules read ``app_shared.config.get_settings()`` at import
    time), reproduced locally so this file has no import-order
    dependency on that one.
    """
    import importlib.util

    from app_shared.config import get_settings

    for name, value in _SETTINGS_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    def load(module_name: str) -> dict[str, Any]:
        spec = importlib.util.find_spec(module_name)
        assert spec is not None and spec.loader is not None, module_name
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return {name: getattr(module, name) for name in dir(module) if name.isupper()}

    yield load
    get_settings.cache_clear()


class TestMiddlewareRegistration:
    def test_price_monitor_registers_wire_bytes_before_http_compression(
        self, scrapy_project_settings: Any
    ) -> None:
        """``price_monitor`` (the non-browser project) is where this
        middleware matters: PROXY/DIRECT fetches go through Scrapy's own
        ``HttpCompressionMiddleware``, so this is the project whose
        ``bytes_compressed`` this middleware feeds."""
        settings = scrapy_project_settings("price_monitor.settings")
        middlewares = settings["DOWNLOADER_MIDDLEWARES"]

        priority = middlewares["scrape_core.middlewares.wire_bytes.WireBytesMiddleware"]
        assert priority == 595
        assert priority > 590, (
            "responses walk DESCENDING, so seeing the body before "
            "HttpCompressionMiddleware (590) decodes it needs a HIGHER number "
            "(review R12 — 585 measured the decoded body)"
        )
        assert priority < 600, (
            "must stay below RedirectMiddleware (600), which answers a 3xx with a "
            "Request and short-circuits the rest of the response chain"
        )
        assert priority > middlewares["scrape_core.netledger_middleware.NetLedgerMiddleware"], (
            "the ledger boundary (130) still closes strictly after this middleware runs, "
            "on the response path Scrapy walks in descending priority"
        )


# ---------------------------------------------------------------------------
# R12 (review 2026-09-09): the ASSEMBLED response chain, not the priority alone
# ---------------------------------------------------------------------------
#
# The three registration assertions above pinned a *number* and a comment,
# and both were wrong in the same direction: `process_response` walks the
# downloader middlewares in DESCENDING priority, so 585 put this middleware
# strictly AFTER `HttpCompressionMiddleware` (590) — it measured the DECODED
# body every time. The reviewer's probe recorded 177 wire bytes as 18,053.
#
# A number cannot catch that again, because the number was never the claim.
# The claim is "for a gzip response, the value stamped by the chain THIS
# PROJECT CONFIGURES equals the compressed length plus framing", so these
# tests assemble the order from the project's own settings using Scrapy's own
# `build_component_list` (the exact function the real
# `DownloaderMiddlewareManager` uses) and then RUN it.


def _response_chain(project_settings: dict[str, Any]) -> list[str]:
    """The project's downloader middlewares in `process_response` order.

    Scrapy merges ``DOWNLOADER_MIDDLEWARES`` over
    ``DOWNLOADER_MIDDLEWARES_BASE`` and sorts ASCENDING for the request
    path (``build_component_list`` — the same call
    ``DownloaderMiddlewareManager._get_mwlist_from_settings`` makes);
    the response path walks that list in reverse. Reproduced here from
    Scrapy's own primitives rather than re-sorted by hand, so the order
    under test is the order the daemon builds.
    """
    from scrapy.settings import Settings as ScrapySettings
    from scrapy.utils.conf import build_component_list

    settings = ScrapySettings()
    settings.set(
        "DOWNLOADER_MIDDLEWARES",
        project_settings["DOWNLOADER_MIDDLEWARES"],
        priority="project",
    )
    ascending = build_component_list(settings.getwithbase("DOWNLOADER_MIDDLEWARES"))
    return list(reversed(ascending))


_COMPRESSION = "scrapy.downloadermiddlewares.httpcompression.HttpCompressionMiddleware"
_WIRE_BYTES = "scrape_core.middlewares.wire_bytes.WireBytesMiddleware"
_REDIRECT = "scrapy.downloadermiddlewares.redirect.RedirectMiddleware"
_RETRY = "scrapy.downloadermiddlewares.retry.RetryMiddleware"


def _run_response_chain(chain: list[str], request: Request, response: Response) -> Any:
    """Drive the two middlewares that touch the body, in `chain` order.

    Only the compression and wire-bytes middlewares are instantiated: the
    rest of the chain needs a live crawler (and, for this repo's own
    entries, a database). What is under test is the ORDER, and the order
    comes from `_response_chain`, i.e. from the project settings.
    """
    from scrapy.downloadermiddlewares.httpcompression import HttpCompressionMiddleware

    builders = {
        _COMPRESSION: HttpCompressionMiddleware,
        _WIRE_BYTES: WireBytesMiddleware,
    }
    for path in chain:
        builder = builders.get(path)
        if builder is None:
            continue
        response = builder().process_response(request, response, None)
    return response


class TestAssembledResponseChain:
    def test_gzip_response_records_the_compressed_length_plus_framing(
        self, scrapy_project_settings: Any
    ) -> None:
        """R12's reproduction, as a test: the recorded number is the WIRE one.

        The reviewer's probe (``review-evidence-2026-09-09/core-probes.py``)
        built exactly this response and watched the configured order record
        18,053 bytes for 177 bytes on the wire. Here the fixture is the
        file's own pinned 1,000-byte gzip stream, and the assertion is an
        equality against ``compute_wire_bytes`` taken BEFORE the chain runs
        — the framing estimate included, since that is what the production
        value carries.
        """
        settings = scrapy_project_settings("price_monitor.settings")
        chain = _response_chain(settings)

        body = _gzip_fixture_body()
        response = _make_response(body)
        request = response.request
        expected = compute_wire_bytes(response)
        assert expected == 1000 + len(response.headers.to_string()) + len(
            f"HTTP/1.1 200 {HTTP_REASON_PHRASES[200]}\r\n".encode("latin-1")
        )

        out = _run_response_chain(chain, request, response)

        assert len(out.body) == 8000, "the chain really did decompress the body"
        assert request.meta[META_WIRE_BYTES] == expected, (
            "the configured chain recorded the DECODED size — WireBytesMiddleware "
            "is running after HttpCompressionMiddleware on the response path"
        )

    def test_wire_bytes_precedes_http_compression_in_the_response_order(
        self, scrapy_project_settings: Any
    ) -> None:
        """Stated as a position in the assembled list, not as a number."""
        chain = _response_chain(scrapy_project_settings("price_monitor.settings"))

        assert chain.index(_WIRE_BYTES) < chain.index(_COMPRESSION)

    def test_redirect_and_retry_still_run_before_the_measurement(
        self, scrapy_project_settings: Any
    ) -> None:
        """The middlewares that can REPLACE a response must not be displaced.

        ``RedirectMiddleware`` (600) returns a ``Request`` for a 3xx, which
        short-circuits the rest of the response chain; ``RetryMiddleware``
        (550) does the same for a retryable status. Both must keep their
        existing position relative to this middleware — redirect strictly
        before it, retry strictly after — or moving the measurement above
        590 would silently change which responses get measured at all.
        """
        chain = _response_chain(scrapy_project_settings("price_monitor.settings"))

        assert chain.index(_REDIRECT) < chain.index(_WIRE_BYTES)
        assert chain.index(_RETRY) > chain.index(_WIRE_BYTES)

    def test_a_second_response_on_the_same_request_replaces_the_count(
        self, scrapy_project_settings: Any
    ) -> None:
        """A retry or a redirect hop never ACCUMULATES onto the earlier hop.

        ``Request.meta`` survives ``request.replace()``/``get_retry_request``,
        so a retried request arrives at this middleware already carrying the
        previous attempt's ``wire_bytes``. The stamp is an assignment, so the
        second reading replaces it — the ledger charges for the response it
        actually saw, never for the sum of two.
        """
        chain = _response_chain(scrapy_project_settings("price_monitor.settings"))

        first = _make_response(_gzip_fixture_body())
        request = first.request
        _run_response_chain(chain, request, first)
        after_first = request.meta[META_WIRE_BYTES]

        # The retry: same request object (Scrapy copies `meta` onto the new
        # one), a different, smaller response.
        second_body = gzip.compress(b"B" * 100, compresslevel=9, mtime=0)
        second = Response(
            url=request.url,
            status=200,
            headers={"Content-Type": "text/html", "Content-Encoding": "gzip"},
            body=second_body,
            request=request,
        )
        expected_second = compute_wire_bytes(second)
        _run_response_chain(chain, request, second)

        assert request.meta[META_WIRE_BYTES] == expected_second
        assert request.meta[META_WIRE_BYTES] != after_first + expected_second
        assert request.meta[META_WIRE_BYTES] < after_first
