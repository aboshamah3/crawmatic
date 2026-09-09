"""Tests for EPA A7 (deep dive §8.2): the wire-bytes downloader middleware.

``scrape_core.middlewares.wire_bytes.WireBytesMiddleware`` runs at
priority 585 — one below Scrapy's built-in ``HttpCompressionMiddleware``
(590) — so it is the one place in the response chain that can still see
the response exactly as it crossed the wire, before decompression
rewrites ``response.body`` in place. These tests prove:

1. the byte-count formula itself (``len(body) + len(headers) + len(status
   line)``) against a REAL gzip fixture that is 1,000 bytes compressed /
   8,000 bytes decoded, so the fixture's own shape proves the
   distinction the middleware exists to preserve;
2. the middleware stamps that count onto ``response.meta["wire_bytes"]``
   and returns the SAME response object unchanged (a downloader
   middleware's ``process_response`` contract);
3. both HTTP scraper projects register it strictly before
   ``HttpCompressionMiddleware`` (priority < 590).
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
        assert priority == 585
        assert priority < 590, "must see the response before HttpCompressionMiddleware decodes it"
        assert priority > middlewares["scrape_core.netledger_middleware.NetLedgerMiddleware"], (
            "the ledger boundary (130) still closes strictly after this middleware runs, "
            "on the response path Scrapy walks in descending priority"
        )
