"""JSON-LD Product/Offer extractor (contracts/extraction.md #1).

Pure ``parsel`` + stdlib ``json`` — no reactor, no Scrapy Response
object required (a plain HTML string is enough), so this is fully
unit-testable off-reactor against fixture HTML.
"""

from __future__ import annotations

import json
from typing import Any

from parsel import Selector

from app_shared.enums import ExtractionMethod, StockStatus
from app_shared.profiles.confidence import resolve_confidence_rules

from scrape_core.extraction.result import ExtractionCandidate

__all__ = ["extract_jsonld"]

_JSONLD_SCRIPT_CSS = 'script[type="application/ld+json"]::text'

# schema.org `Offer.availability` URLs (or bare tokens), last path segment
# lower-cased, mapped to our StockStatus vocabulary. Anything recognized
# but not explicitly in-stock is treated as out-of-stock; anything
# unrecognized is UNKNOWN (never guessed as in-stock).
_OUT_OF_STOCK_TOKENS = frozenset(
    {"outofstock", "soldout", "discontinued", "backorder", "preorder"}
)
_IN_STOCK_TOKENS = frozenset({"instock", "limitedavailability", "onlineonly", "instoreonly"})


def _stock_from_availability(value: Any) -> StockStatus | None:
    if not isinstance(value, str) or not value.strip():
        return None
    token = value.rsplit("/", 1)[-1].strip().lower()
    if token in _IN_STOCK_TOKENS:
        return StockStatus.IN_STOCK
    if token in _OUT_OF_STOCK_TOKENS:
        return StockStatus.OUT_OF_STOCK
    return StockStatus.UNKNOWN


def _has_type(node: dict[str, Any], type_name: str) -> bool:
    node_type = node.get("@type")
    candidates = node_type if isinstance(node_type, list) else [node_type]
    return any(isinstance(t, str) and t.lower() == type_name for t in candidates)


def _iter_dicts(node: Any) -> Any:
    """Depth-first walk yielding every ``dict`` reachable from ``node``.

    JSON-LD is commonly a single object, a ``@graph`` array, or a bare
    list of objects — this walk finds a ``Product`` wherever it is
    nested rather than assuming a fixed top-level shape.
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item)


def _find_product(data: Any) -> dict[str, Any] | None:
    for node in _iter_dicts(data):
        if _has_type(node, "product"):
            return node
    return None


def _find_offer(product: dict[str, Any]) -> dict[str, Any] | None:
    offers = product.get("offers")
    if isinstance(offers, list):
        for item in offers:
            if isinstance(item, dict):
                return item
        return None
    if isinstance(offers, dict):
        return offers
    return None


def extract_jsonld(html: str, *, profile: Any = None) -> ExtractionCandidate | None:
    """Parse every ``<script type="application/ld+json">`` block for a Product/Offer.

    Returns the first block yielding a ``Product`` with an ``Offer``
    carrying a non-``None`` ``price`` — malformed JSON or a block
    lacking a usable price is skipped, never raised (the caller falls
    through to the next strategy / ``PRICE_NOT_FOUND``).

    Skipped outright when ``profile.jsonld_enabled`` is explicitly
    ``False`` (a profile that disables this strategy); a missing
    ``profile`` or a profile without the attribute is treated as
    enabled (the historical/simple default).
    """
    if profile is not None and getattr(profile, "jsonld_enabled", True) is False:
        return None

    selector = Selector(text=html)
    for raw in selector.css(_JSONLD_SCRIPT_CSS).getall():
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

        product = _find_product(data)
        if product is None:
            continue

        offer = _find_offer(product)
        if offer is None:
            continue

        price = offer.get("price")
        if price is None:
            continue

        profile_confidence_rules = getattr(profile, "confidence_rules", None) if profile else None
        confidence = resolve_confidence_rules(profile_confidence_rules)["jsonld"]

        currency = offer.get("priceCurrency")
        return ExtractionCandidate(
            raw_price_text=str(price),
            currency=str(currency) if currency else None,
            method=ExtractionMethod.JSON_LD,
            confidence=confidence,
            selector_used=_JSONLD_SCRIPT_CSS,
            raw_title=product.get("name") if isinstance(product.get("name"), str) else None,
            stock=_stock_from_availability(offer.get("availability")),
            matched_text=json.dumps(offer, default=str),
        )
    return None
