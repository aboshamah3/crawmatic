"""Wire-format byte count, captured before decompression (EPA A7, §8.2).

``scrape_core.netledger_middleware`` (priority 130) already records
``bytes_decompressed`` from the final, decoded body — measurable only
because it runs at a priority AFTER Scrapy's built-in
``HttpCompressionMiddleware`` (590; ``process_response`` walks
DESCENDING, so a higher number runs first). Nothing at that position can
also see the COMPRESSED count directly from the response object, because
by the time control reaches 130 the body has already been rewritten in
place.

This module is the fix: a tiny middleware registered at priority **585**
— one below ``HttpCompressionMiddleware``'s 590, so it still sees the
response BEFORE that middleware decodes it — that measures exactly what
the earlier docstring called "the truest available reading of what the
wire carried" and stores it on ``response.meta["wire_bytes"]`` for
``netledger_middleware`` to pick up. It replaces the previous
``bytes_received``-signal approximation (raw TCP chunks, no framing) with
the actual application-layer count: body + headers + status line, all
still in their on-the-wire (compressed) form at this priority.

Why 585 and not "the lowest number below 590"
----------------------------------------------
Nothing else in this repo's ``DOWNLOADER_MIDDLEWARES`` currently occupies
120-590, so 585 is not fighting for a slot — it is chosen close to 590 so
that the only thing plausibly able to run between this middleware and
``HttpCompressionMiddleware`` is another well-known Scrapy built-in
(there is none registered here), keeping "not yet decompressed" an
invariant this module can actually rely on rather than merely hope for.

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

    Called at priority 585, strictly before ``HttpCompressionMiddleware``
    (590) rewrites ``response.body`` in place, so ``response.body`` here
    is still the compressed bytes that actually crossed the wire.
    """
    status_line = _status_line_bytes(response)
    headers_bytes = response.headers.to_string()
    return len(response.body) + len(headers_bytes) + len(status_line)


class WireBytesMiddleware:
    """Stamps ``response.meta["wire_bytes"]`` before decompression.

    Registered at priority 585 in ``DOWNLOADER_MIDDLEWARES`` (see this
    module's docstring for why). No settings, no state — one instance is
    as good as none, so this needs neither ``__init__`` nor
    ``from_crawler``.
    """

    def process_response(self, request: Any, response: Any, spider: Any) -> Any:
        response.meta[META_WIRE_BYTES] = compute_wire_bytes(response)
        return response
