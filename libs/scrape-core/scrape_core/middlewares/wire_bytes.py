"""Wire-format byte count, captured before decompression (EPA A7, §8.2).

``scrape_core.netledger_middleware`` (priority 130) already records
``bytes_decompressed`` from the final, decoded body — measurable only
because it runs at a priority AFTER Scrapy's built-in
``HttpCompressionMiddleware`` (590; ``process_response`` walks
DESCENDING, so a higher number runs first). Nothing at that position can
also see the COMPRESSED count directly from the response object, because
by the time control reaches 130 the body has already been rewritten in
place.

This module is the fix: a tiny middleware registered at priority **595**
— ABOVE ``HttpCompressionMiddleware``'s 590, so on the descending
response walk it runs BEFORE that middleware decodes the body — that
measures exactly what the earlier docstring called "the truest available
reading of what the wire carried" and stores it on
``response.meta["wire_bytes"]`` for ``netledger_middleware`` to pick up.
It replaces the previous ``bytes_received``-signal approximation (raw TCP
chunks, no framing) with the actual application-layer count: body +
headers + status line, all still in their on-the-wire (compressed) form
at this priority.

Why 595, and why it was 585 until review R12 (2026-09-09)
---------------------------------------------------------
This module originally said "one below 590 so it still sees the response
BEFORE that middleware decodes it", and registered 585. That sentence
contradicts the one two paragraphs above it: ``process_response`` walks
DESCENDING, so 585 runs strictly AFTER 590. The middleware therefore
measured the DECODED body for its entire life — a 177-byte gzip response
was recorded as 18,053 bytes, reproduced in
``review-evidence-2026-09-09/core-probes.py`` and now pinned by
``tests/unit/test_wire_bytes_middleware.py``'s assembled-chain tests,
which read the order from the project settings through Scrapy's own
``build_component_list`` instead of asserting a number.

595 is the corrected slot: strictly above ``HttpCompressionMiddleware``
(590) and strictly below ``RedirectMiddleware`` (600). The upper bound
matters as much as the lower one. ``RedirectMiddleware`` answers a 3xx
with a ``Request``, which short-circuits the remainder of the response
chain, and ``RetryMiddleware`` (550) does the same for a retryable
status; keeping this middleware between them means the correction
changed *what is measured* without changing *which responses are
measured*. Nothing else in this repo's ``DOWNLOADER_MIDDLEWARES``
occupies 590-600, so 595 is not fighting for a slot.

``response.meta`` is a live view onto ``request.meta`` (Scrapy's
``Response.meta`` property forwards to ``self.request.meta``), so writing
here is visible to every later middleware and to the spider itself,
exactly like every other meta key this repo threads through the chain.
"""

from __future__ import annotations

from http.client import responses as _HTTP_REASON_PHRASES
from typing import Any

__all__ = ["WireBytesMiddleware", "compute_wire_bytes"]

#: Meta key this middleware stamps. Read by
#: ``scrape_core.netledger_middleware._outcome_for`` for PROXY/DIRECT
#: transports; the BROWSER path measures its own bytes via
#: ``scrape_core.browser.byte_capture.ByteAccumulator`` and never sees
#: this middleware (Playwright responses do not carry a compressible
#: wire body Scrapy itself decodes).
META_WIRE_BYTES = "wire_bytes"


def _status_line_bytes(response: Any) -> bytes:
    """Reconstruct the status line Scrapy itself discards.

    Scrapy's ``Response`` never keeps the raw status line it received —
    only ``status`` (an int) and, when the download handler set it,
    ``protocol`` (``None`` for most of this repo's fetches, which do not
    negotiate HTTP/2). Rather than leave that byte cost out of the count
    entirely, this reconstructs the line an HTTP/1.1 server would have
    sent for that status, using the standard reason phrase — a small,
    deterministic estimate of the framing bytes the connection actually
    spent, consistent between this module and its own test fixture (the
    exact reason-phrase text is this module's choice, not a wire fact
    recoverable after Scrapy has already parsed it away).
    """
    protocol = getattr(response, "protocol", None) or "HTTP/1.1"
    reason = _HTTP_REASON_PHRASES.get(response.status, "")
    line = f"{protocol} {response.status} {reason}".rstrip() + "\r\n"
    return line.encode("latin-1", errors="replace")


def compute_wire_bytes(response: Any) -> int:
    """``len(body) + len(headers) + len(status line)`` at THIS priority.

    Called at priority 595, strictly before ``HttpCompressionMiddleware``
    (590) rewrites ``response.body`` in place — "before" in the
    descending order ``process_response`` is walked in, which is why the
    number is HIGHER, not lower — so ``response.body`` here is still the
    compressed bytes that actually crossed the wire.

    The status-line component is an ESTIMATE and is documented as one in
    :func:`_status_line_bytes`: Scrapy discards the raw status line, so
    that part of the framing is reconstructed, not observed.
    """
    status_line = _status_line_bytes(response)
    headers_bytes = response.headers.to_string()
    return len(response.body) + len(headers_bytes) + len(status_line)


class WireBytesMiddleware:
    """Stamps ``response.meta["wire_bytes"]`` before decompression.

    Registered at priority 595 in ``DOWNLOADER_MIDDLEWARES`` (see this
    module's docstring for why that number, and for what 585 got wrong).
    The stamp is an ASSIGNMENT, never an accumulation: a retried or
    redirected request carries the previous hop's ``meta`` forward, and
    the ledger must charge for the response this call actually saw rather
    than for the sum of two. No settings, no state — one instance is as
    good as none, so this needs neither ``__init__`` nor ``from_crawler``.
    """

    def process_response(self, request: Any, response: Any, spider: Any) -> Any:
        response.meta[META_WIRE_BYTES] = compute_wire_bytes(response)
        return response
