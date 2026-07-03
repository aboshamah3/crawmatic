"""Regex Product extractor + the single-number heuristic (contracts/extraction.md #3, SPEC-07 US3 T033).

Pure ``parsel`` (for text-node segmentation) + stdlib ``re`` — no
reactor. Two independent paths:

1. **DB regex rules** (``price_regex``/``old_price_regex``/``currency_regex``/
   ``stock_regex`` from the resolved ``ScrapeProfile``) applied to each
   visible text node in document order, first match wins. Method
   ``REGEX``, default confidence **0.75**.
2. **Single unlabeled-number heuristic** — only tried when no configured
   ``price_regex`` matched (or none is configured): if exactly *one* bare
   number appears anywhere in the page's visible text, it is surfaced as
   a ``SINGLE_NUMBER`` candidate, confidence **0.40** ("reject by
   default" — the reject decision itself is validation's, via the
   confidence gate, not this module's). Zero or more than one bare
   number is ambiguous — returns ``None`` rather than guessing.
"""

from __future__ import annotations

import re
from typing import Any

from parsel import Selector

from app_shared.enums import ExtractionMethod, StockStatus
from app_shared.profiles.confidence import resolve_confidence_rules

from scrape_core.extraction.result import ExtractionCandidate

__all__ = ["extract_regex"]

# <script>/<style> text is never "visible page text" — excluded from the
# single-number heuristic's scan (a script full of numbers is not "one
# bare number on the page"). A *configured* price_regex intentionally has
# no such exclusion: DB rules are written to reach into inline JSON blobs
# (see tests/fixtures/html/regex_only.html).
_NON_VISIBLE_TAGS = frozenset({"script", "style"})

_BARE_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

_OUT_OF_STOCK_PHRASES = ("out of stock", "sold out", "unavailable", "false")
_IN_STOCK_PHRASES = ("in stock", "available", "true")


def _text_nodes(html: str, *, exclude_tags: frozenset[str] = frozenset()) -> list[str]:
    """Every non-empty, stripped text node in document order.

    Segmenting by text node (rather than regex-searching the raw HTML
    string) keeps a match's surrounding context to its own element — the
    ``matched_text`` a ``reject_if_text_contains`` rule checks against —
    without also matching inside tag markup/attributes.
    """
    selector = Selector(text=html)
    nodes: list[str] = []
    for text_node in selector.xpath("//text()"):
        if exclude_tags:
            parent_tag = text_node.xpath("parent::*/name()").get()
            if parent_tag and parent_tag.lower() in exclude_tags:
                continue
        text = text_node.get()
        if text and text.strip():
            nodes.append(text.strip())
    return nodes


def _first_regex_match(nodes: list[str], pattern: str) -> tuple[str, str] | None:
    """``(matched_group, matched_text_node)`` for the first node matching ``pattern``, else ``None``."""
    try:
        compiled = re.compile(pattern)
    except re.error:
        return None
    for node in nodes:
        match = compiled.search(node)
        if match:
            value = match.group(1) if match.groups() else match.group(0)
            return value, node
    return None


def _stock_from_text(text: str | None) -> StockStatus | None:
    if not text:
        return None
    lowered = text.lower()
    if any(phrase in lowered for phrase in _OUT_OF_STOCK_PHRASES):
        return StockStatus.OUT_OF_STOCK
    if any(phrase in lowered for phrase in _IN_STOCK_PHRASES):
        return StockStatus.IN_STOCK
    return StockStatus.UNKNOWN


def _extract_via_price_regex(
    nodes: list[str], profile: Any, price_regex: str, confidence: float
) -> ExtractionCandidate | None:
    match = _first_regex_match(nodes, price_regex)
    if match is None:
        return None
    raw_price_text, matched_text = match

    currency = None
    currency_regex = getattr(profile, "currency_regex", None) if profile is not None else None
    if currency_regex:
        currency_match = _first_regex_match(nodes, currency_regex)
        if currency_match:
            currency = currency_match[0]

    stock = None
    stock_regex = getattr(profile, "stock_regex", None) if profile is not None else None
    if stock_regex:
        stock_match = _first_regex_match(nodes, stock_regex)
        if stock_match:
            stock = _stock_from_text(stock_match[1])

    old_price_regex = getattr(profile, "old_price_regex", None) if profile is not None else None
    if old_price_regex:
        old_price_match = _first_regex_match(nodes, old_price_regex)
        if old_price_match and old_price_match[1] not in matched_text:
            matched_text = f"{matched_text} {old_price_match[1]}".strip()

    return ExtractionCandidate(
        raw_price_text=raw_price_text,
        currency=currency,
        method=ExtractionMethod.REGEX,
        confidence=confidence,
        selector_used=price_regex,
        raw_title=None,
        stock=stock,
        matched_text=matched_text,
    )


def _extract_single_number(html: str, confidence: float) -> ExtractionCandidate | None:
    nodes = _text_nodes(html, exclude_tags=_NON_VISIBLE_TAGS)
    found: list[tuple[str, str]] = []
    for node in nodes:
        for number_match in _BARE_NUMBER.finditer(node):
            found.append((number_match.group(0), node))
            if len(found) > 1:
                # Already ambiguous — no need to keep scanning.
                return None
    if len(found) != 1:
        return None
    raw_price_text, matched_text = found[0]
    return ExtractionCandidate(
        raw_price_text=raw_price_text,
        currency=None,
        method=ExtractionMethod.SINGLE_NUMBER,
        confidence=confidence,
        selector_used=None,
        raw_title=None,
        stock=None,
        matched_text=matched_text,
    )


def extract_regex(html: str, *, profile: Any = None) -> ExtractionCandidate | None:
    """DB regex rules first, else the single unlabeled-number heuristic.

    Returns ``None`` when neither path finds a usable price — the caller
    falls through to ``PRICE_NOT_FOUND``, never raised.
    """
    profile_confidence_rules = getattr(profile, "confidence_rules", None) if profile else None
    confidence_rules = resolve_confidence_rules(profile_confidence_rules)

    price_regex = getattr(profile, "price_regex", None) if profile is not None else None
    if price_regex:
        nodes = _text_nodes(html)
        candidate = _extract_via_price_regex(nodes, profile, price_regex, confidence_rules["regex"])
        if candidate is not None:
            return candidate

    return _extract_single_number(html, confidence_rules["single_number"])
