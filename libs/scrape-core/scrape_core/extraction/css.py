"""CSS-selector Product extractor (contracts/extraction.md #2, SPEC-07 US3 T032).

Pure ``parsel`` — no reactor, no Scrapy ``Response`` object required, so
this is fully unit-testable off-reactor against fixture HTML, same as
``jsonld.py``. Selectors come from the resolved DB-configured
``ScrapeProfile`` (``price_selector``/``old_price_selector``/
``currency_selector``/``stock_selector``/``title_selector`` —
data-model.md / SPEC-06 ``scrape_profiles``), never hardcoded.
"""

from __future__ import annotations

from typing import Any

from parsel import Selector

from app_shared.enums import ExtractionMethod, StockStatus
from app_shared.profiles.confidence import resolve_confidence_rules

from scrape_core.extraction.result import ExtractionCandidate

__all__ = ["extract_css"]

_OUT_OF_STOCK_PHRASES = ("out of stock", "sold out", "unavailable", "no longer available")
_IN_STOCK_PHRASES = ("in stock", "available", "add to cart", "add to basket")


def _first_text(selector: Selector, css_query: str | None) -> str | None:
    """Return the stripped text content of the first element matching ``css_query``."""
    if not css_query:
        return None
    match = selector.css(css_query)
    if not match:
        return None
    text = match.xpath("string(.)").get()
    if text is None:
        return None
    text = text.strip()
    return text or None


def _context_text(selector: Selector, css_query: str | None) -> str | None:
    """Text of the matched element's parent — the broader context a
    ``reject_if_text_contains`` rule matches against (an "Old Price"/"Save
    X" label a sibling element away from the price node itself)."""
    if not css_query:
        return None
    match = selector.css(css_query)
    if not match:
        return None
    parent = match.xpath("./..")
    text = parent.xpath("string(.)").get() if parent else None
    if not text:
        text = match.xpath("string(.)").get()
    return text.strip() if text else None


def _stock_from_text(text: str | None) -> StockStatus | None:
    """Classify free-text stock copy (e.g. "In Stock") — never guessed as
    in-stock when unrecognized, mirroring ``jsonld._stock_from_availability``."""
    if not text:
        return None
    lowered = text.lower()
    if any(phrase in lowered for phrase in _OUT_OF_STOCK_PHRASES):
        return StockStatus.OUT_OF_STOCK
    if any(phrase in lowered for phrase in _IN_STOCK_PHRASES):
        return StockStatus.IN_STOCK
    return StockStatus.UNKNOWN


def extract_css(html: str, *, profile: Any = None) -> ExtractionCandidate | None:
    """Extract a price via the profile's CSS selectors.

    Returns ``None`` when the profile has no ``price_selector`` configured,
    or the selector matches nothing / empty text — the caller (the
    pipeline orchestrator) falls through to the next strategy /
    ``PRICE_NOT_FOUND``, never raised.
    """
    price_selector = getattr(profile, "price_selector", None) if profile is not None else None
    if not price_selector:
        return None

    selector = Selector(text=html)
    raw_price_text = _first_text(selector, price_selector)
    if not raw_price_text:
        return None

    profile_confidence_rules = getattr(profile, "confidence_rules", None) if profile else None
    confidence = resolve_confidence_rules(profile_confidence_rules)["css"]

    currency = _first_text(selector, getattr(profile, "currency_selector", None))
    stock_text = _first_text(selector, getattr(profile, "stock_selector", None))
    title = _first_text(selector, getattr(profile, "title_selector", None))

    matched_text = _context_text(selector, price_selector) or raw_price_text
    old_price_text = _context_text(selector, getattr(profile, "old_price_selector", None))
    if old_price_text and old_price_text not in matched_text:
        matched_text = f"{matched_text} {old_price_text}".strip()

    return ExtractionCandidate(
        raw_price_text=raw_price_text,
        currency=currency,
        method=ExtractionMethod.CSS,
        confidence=confidence,
        selector_used=price_selector,
        raw_title=title,
        stock=_stock_from_text(stock_text),
        matched_text=matched_text,
    )
