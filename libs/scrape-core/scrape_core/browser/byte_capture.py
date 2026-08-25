"""Live per-attempt transport-byte accounting for the browser path (EPA B6b).

B6 built the pure classification half of this contract
(:func:`app_shared.profiles.browser_resource_policy.split_attempt_bytes`)
and the DB column plumbing (``request_attempts.main_document_bytes``/
``subresource_bytes``, ``alembic/versions/d5e8a3c164f2_...``) but left the
live half -- actually observing bytes as a real Chromium page loads --
deferred. This module is that live half: a per-attempt
:class:`ByteAccumulator` whose bound :meth:`~ByteAccumulator.handle_response`
is wired onto the page as a ``response`` event listener (via
scrapy-playwright's ``request.meta["playwright_page_event_handlers"]``
seam -- ``_attach_page_event_handlers`` in ``scrapy_playwright.handler``
calls ``page.on(event, handler)`` for every entry, the same install point
this repo already uses for ``PLAYWRIGHT_ABORT_REQUEST``), one fresh
instance per dispatched request (never shared/reused across attempts --
each browser attempt gets its own page, closed at the end of that
attempt).

**What "transport-observed" means here, concretely** (the contract this
repo already committed to in the migration's own docstring -- see there
for why this is recorded as a DISTINCT fact from *provider-billed*
bytes, reconciled only in a later step, never conflated here):

1. **``Content-Length`` response header, when present** -- read via
   ``Response.headers`` (a synchronous property backed by the headers
   Chromium already parsed off the wire for this response; no extra
   round-trip to the browser process).
2. **Else, the response body's actual byte length**, via
   ``await Response.body()`` -- one extra round-trip to Chromium's
   already-buffered response body (cheap: the browser already holds the
   bytes in memory for the page to render; this call does not re-fetch
   over the network). Only attempted when (1) is unavailable.
3. **Else, not counted for that one response** -- some responses
   (redirects, some opaque/service-worker-served responses) can raise on
   ``body()``; this module never fabricates a number for those, per the
   NEVER-FABRICATE contract in the migration docstring. A response this
   module could not measure simply contributes nothing to either total;
   it is not the same as "zero bytes" (see :meth:`ByteAccumulator.finalize`).

A response for a request the resource-blocking policy (`scrape_core.
browser.ssrf.abort_unsafe_request`) aborted never reaches this listener
at all -- ``route.abort()`` happens before Chromium ever produces a
``response`` event for that request, so blocked image/media/font/
stylesheet/ad-host traffic is correctly absent from both totals, not
double-subtracted or specially handled here.

**Known limitation** (documented, not silently swallowed): a genuinely
huge subresource that streams via `body()` could in principle add
latency to the attempt; in practice this only affects resources the B6
cost policy already let through (a small, curated set per domain), so
the added round-trip is bounded by exactly the traffic this module
exists to measure.
"""

from __future__ import annotations

import logging
from typing import Any

from app_shared.profiles.browser_resource_policy import split_attempt_bytes

logger = logging.getLogger(__name__)

__all__ = ["ByteAccumulator"]


async def _measured_byte_count(response: Any) -> int | None:
    """``Content-Length`` header if present and parseable, else
    ``await response.body()``'s length, else ``None`` (module docstring
    point 3 -- never fabricated)."""
    try:
        headers = response.headers or {}
    except Exception:  # noqa: BLE001 - defensive; a header-access failure is not fatal here
        headers = {}
    content_length = headers.get("content-length") if isinstance(headers, dict) else None
    if content_length is not None:
        try:
            return int(content_length)
        except (TypeError, ValueError):
            pass  # non-numeric/garbled header -- fall through to the body-size fallback

    try:
        body = await response.body()
    except Exception:  # noqa: BLE001 - Playwright raises a generic Error for
        # redirect/opaque responses whose body is unavailable; that is an
        # ordinary, expected "not measurable" case here, never a bug to
        # propagate into the attempt's own success/failure classification.
        return None
    try:
        return len(body)
    except TypeError:
        return None


class ByteAccumulator:
    """Accumulates ``(resource_type, byte_count)`` pairs for one browser
    attempt's page, via its bound :meth:`handle_response` wired as a
    Playwright ``response`` event listener.

    Never raises out of :meth:`handle_response` -- a measurement failure
    for one response degrades to "not counted for that response" (module
    docstring), never to an unhandled exception inside a Playwright event
    callback (which scrapy-playwright would otherwise have no clean way
    to surface back to this attempt's own success/failure classification).
    """

    def __init__(self) -> None:
        self._observed: list[tuple[str, int]] = []

    async def handle_response(self, response: Any) -> None:
        try:
            request = response.request
            resource_type = getattr(request, "resource_type", None) or "other"
        except Exception:  # noqa: BLE001 - defensive; never let listener bookkeeping
            # crash the page/navigation it is merely observing.
            resource_type = "other"

        try:
            byte_count = await _measured_byte_count(response)
        except Exception:  # noqa: BLE001 - see class docstring: a measurement
            # failure must never propagate out of an event listener.
            logger.debug("ByteAccumulator: failed to measure response bytes", exc_info=True)
            byte_count = None

        if byte_count is not None and byte_count >= 0:
            self._observed.append((resource_type, byte_count))

    def finalize(self) -> tuple[int | None, int | None]:
        """``(main_document_bytes, subresource_bytes)`` for the whole attempt.

        ``(None, None)`` when nothing was ever successfully measured
        (either no ``response`` event fired at all -- e.g. the navigation
        failed before Chromium produced one -- or every response this
        accumulator saw failed to measure) -- the correct "not measured"
        NULL per ``request_attempts``' own contract, never conflated with
        "measured, zero bytes" (:func:`~app_shared.profiles.
        browser_resource_policy.split_attempt_bytes`'s own docstring makes
        this same distinction for its pure half).
        """
        if not self._observed:
            return None, None
        return split_attempt_bytes(self._observed)
