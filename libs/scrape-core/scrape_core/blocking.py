"""Bot-interstitial detection for responses that arrive HTTP 200 (2026-08-03).

Amazon answers a flagged fetch with a **200** carrying a ~5 KB CAPTCHA
interstitial ("Enter the characters you see below", "To discuss automated
access to Amazon data..."), not a 4xx. Nothing upstream can tell that from
a real page: `classify_http_status` sees 200, extraction then finds no
price, and the attempt dies terminal `PRICE_NOT_FOUND` with **no retry** —
which is exactly how 20 of 34 amazon fetches were lost in the 2026-08-03
re-run, while the same URLs through the same proxy returned real pages on
a different exit IP moments later.

The block is per-exit-IP reputation, not per-URL: it is transient and a
retry on a fresh proxy session usually succeeds. So the right response is
"treat it as a failed fetch and let the existing retry ladder re-attempt",
which is what :class:`BlockDetectionMiddleware` does by raising
:class:`BlockedResponseError` from ``process_response``. Scrapy routes an
``IgnoreRequest`` raised there to the request's **errback**, so the
spiders' already-tested retry path (record the attempt, re-decide via
``_prepare_dispatch``, re-dispatch with a fresh proxy assignment) runs
unchanged — no new retry machinery, and the semaphore/lock bookkeeping
the errback already owns keeps working.

Detection is deliberately marker-based and conservative: a real product
page does not contain Amazon's interstitial copy. The size test is only
ever applied **together** with a marker, never on its own, so a genuinely
small page is never mistaken for a block.
"""

from __future__ import annotations

import logging
from typing import Any

from scrapy.exceptions import IgnoreRequest

from scrape_core.errors import BLOCKED

logger = logging.getLogger(__name__)

__all__ = ["BlockedResponseError", "BlockDetectionMiddleware", "looks_blocked"]


class BlockedResponseError(IgnoreRequest):
    """A 200 response that is really a bot/CAPTCHA interstitial.

    Subclasses ``IgnoreRequest`` so Scrapy hands it to the request's
    errback (the retry seam). Carries the explicit ``error_code``
    attribute ``scrape_core.errors.classify_exception`` looks for first —
    the same contract ``SsrfRejectedError``/``RobotsBlockedError`` use, so
    no class-name sniffing is needed for this to classify as ``BLOCKED``.
    """

    error_code = BLOCKED


#: Substrings that only ever appear on a bot-check interstitial. Matched
#: case-insensitively against the response body. Kept narrow on purpose:
#: a false positive turns a real page into a wasted retry.
_BLOCK_MARKERS: tuple[str, ...] = (
    "/errors/validatecaptcha",
    "enter the characters you see below",
    "to discuss automated access",
    "type the characters you see in this image",
    "sorry, we just need to make sure you're not a robot",
    "unusual traffic from your computer network",
    "checking your browser before accessing",
    # noon.com fronts with **Akamai Bot Manager**, which answers a flagged
    # fetch with a ~2.5 KB behavioural-challenge page at HTTP 200 (measured
    # 2026-08-05: 2 of 12 fetches of one product URL, then 0 of 70 an hour
    # later — bursty and reputation-driven, exactly like amazon's CAPTCHA).
    # Without these markers the challenge reaches extraction, finds no
    # price, and dies terminal PRICE_NOT_FOUND with no retry — which is
    # what job 279b32fd's noon failures were.
    #
    # Matched on the challenge's own markup rather than its prose: the CSS
    # class names are stable across Akamai's locales, whereas the visible
    # text is not. The <60 KB size guard above is what keeps these safe —
    # a real noon product page is 430-540 KB and can mention Akamai (it is
    # their CDN) without ever being considered.
    "sec-bc-button-parent",
    "scf-akamai-logo",
    "powered and protected by",
)

#: A bot interstitial is tiny (~5 KB) next to a real product page
#: (200 KB-800 KB). Only consulted once a marker has already matched.
_MAX_INTERSTITIAL_BYTES = 60_000


def looks_blocked(body: bytes | str, *, markers: tuple[str, ...] = _BLOCK_MARKERS) -> bool:
    """``True`` when ``body`` is a bot/CAPTCHA interstitial.

    Requires a marker hit **and** a small body, so a real page that merely
    quotes one of these phrases (a review, a help article) is not thrown
    away — those pages are full-size.
    """
    if isinstance(body, bytes):
        if len(body) > _MAX_INTERSTITIAL_BYTES:
            return False
        text = body.decode("utf-8", "ignore")
    else:
        if len(body.encode("utf-8", "ignore")) > _MAX_INTERSTITIAL_BYTES:
            return False
        text = body
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


class BlockDetectionMiddleware:
    """Downloader middleware: turn a 200-with-interstitial into a retry.

    Placed after the compression middleware so it inspects the decoded
    body. A non-200 response is left alone — ``classify_http_status``
    already owns those.
    """

    def process_response(self, request: Any, response: Any, spider: Any) -> Any:
        if response.status != 200:
            return response
        if not looks_blocked(response.body):
            return response
        logger.warning(
            "block_detected: %s answered a bot interstitial (%d bytes) for %s",
            request.url.split("/")[2] if "//" in request.url else request.url,
            len(response.body),
            request.url,
        )
        raise BlockedResponseError(f"bot interstitial served with HTTP 200 for {request.url}")
