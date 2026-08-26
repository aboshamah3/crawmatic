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

# Compact status tokens a stock_regex may capture directly (e.g. a
# `stockLevelStatus` code inside an app-state JSON blob). Mirrors
# ``_OUT_OF_STOCK_TOKENS`` in extraction/jsonld.py, plus retailer codes.
_OUT_OF_STOCK_TOKENS = frozenset(
    {"outofstock", "soldout", "discontinued", "backorder", "preorder"}
)
_IN_STOCK_TOKENS = frozenset({"instock", "lowstock", "limitedavailability"})


# ---------------------------------------------------------------------------
# Customer-supplied regex CPU/memory bounds on the LIVE path (EPA W5.5-L1 §10)
# ---------------------------------------------------------------------------
#
# `price_regex`/`old_price_regex`/`currency_regex`/`stock_regex` are
# DB-supplied, learned, competitor-page-influenced text, and CPython's `re`
# cannot be interrupted mid-match — no timeout, no signal, no thread stops a
# catastrophic backtrack. `extraction/pipeline.py` already applies the W3.2
# budget (`app_shared.strategy.candidate_ranking.search_bounded`) but ONLY on
# the ranked path; every current production caller reaches
# `extract_regex` -> `_first_regex_match` directly and is unbounded.
#
# This wires the same pre-flight into the live path, and it is DEFAULT OFF.
# With the flag off, `_first_regex_match` is byte-for-byte the function it was
# (the bounded helper is not even imported), so switching it on is the only
# behaviour change and it is a single constant.
#
# Two bounds, matching `search_bounded`'s own two halves:
#   1. PATTERN PRE-FLIGHT, run ONCE per pattern (not once per text node):
#      a ReDoS-shaped or oversized pattern is refused outright and the rule
#      finds nothing — FAIL CLOSED, never an exception that would cost the
#      page its other readings, and never an unbounded scan.
#   2. INPUT TRUNCATION: each text node is capped, and the total number of
#      characters any one pattern is allowed to scan across all nodes is
#      capped too. `_text_nodes` returns thousands of nodes on a real product
#      page (6,932 on a live amazon.sa page, per the note above), so a
#      per-node cap alone is not a budget.
#
#: Master switch. DEFAULT OFF — see the block above. Flip to True only after
#: reading the stored-profile pre-flight report (EPA W5.5-L1 item 3), which
#: found 0 of 12 stored production patterns would be refused.
REGEX_BOUNDS_ENABLED = False

#: Longest single text node any bounded pattern may see.
REGEX_BOUNDS_MAX_NODE_CHARS = 65_536

#: Total characters one bounded pattern may scan across every node on a page.
REGEX_BOUNDS_MAX_TOTAL_CHARS = 1_048_576

# TODO(config): promote `REGEX_BOUNDS_ENABLED` /
# `REGEX_BOUNDS_MAX_NODE_CHARS` / `REGEX_BOUNDS_MAX_TOTAL_CHARS` to
# `app_shared.config.Settings` fields
# (`EXTRACTION_REGEX_BOUNDS_ENABLED` / `..._MAX_NODE_CHARS` /
# `..._MAX_TOTAL_CHARS`). They are module constants rather than settings only
# because `config.py` was held by a concurrent worker when this landed.


def _pattern_refused(pattern: str) -> bool:
    """Pre-flight ``pattern`` ONCE through the W3.2 bounded engine.

    ``True`` means the pattern is refused (ReDoS shape, oversized, or
    uncompilable) and must not be run at all. The import is deliberately
    lazy: with ``REGEX_BOUNDS_ENABLED`` off this module never reaches into
    ``app_shared.strategy`` at all, so the flag-off path costs nothing —
    not even an import.

    An empty subject string is passed on purpose: ``search_bounded``'s
    refusal decision is made entirely from the pattern's parse tree, before
    any scanning, so this buys the verdict without buying a scan.
    """
    from app_shared.strategy.candidate_ranking import search_bounded

    _, refusal = search_bounded(pattern, "")
    return refusal is not None


def _text_nodes(html: str, *, exclude_tags: frozenset[str] = frozenset()) -> list[str]:
    """Every non-empty, stripped text node in document order.

    Segmenting by text node (rather than regex-searching the raw HTML
    string) keeps a match's surrounding context to its own element — the
    ``matched_text`` a ``reject_if_text_contains`` rule checks against —
    without also matching inside tag markup/attributes.
    """
    # parsel (<=1.11) tries json.loads(text) BEFORE honoring type="html":
    # any JSON-parseable body becomes a 'json' Selector regardless, and
    # .xpath() on it raises ValueError instead of scanning text nodes
    # (live amazon.sa serves such bodies; pre-guard this crashed the whole
    # extraction chain). A JSON document has no HTML text nodes -- empty.
    selector = Selector(text=html, type="html")
    if selector.type != "html":
        return []
    nodes: list[str] = []
    for text_node in selector.xpath("//text()"):
        # Walk lxml directly instead of calling .xpath()/.get() on the
        # child Selector. parsel re-infers a *per-node* type, and any text
        # node that happens to be JSON-parseable comes back typed 'json' —
        # on which .xpath() raises ValueError. The root-level guard above
        # does not help, because the root really is 'html'.
        #
        # This is not hypothetical: a live amazon.sa product page has 6,932
        # text nodes of which 91 (the embedded <script> JSON blobs) type as
        # 'json'. Before this fix the ValueError escaped `parse` and killed
        # the whole item — 73 of 82 amazon pages in job 279b32fd were
        # fetched successfully and then lost right here.
        #
        # `.root` on a text-node Selector is lxml's _ElementUnicodeResult (a
        # str subclass that knows its parent), so this is both type-agnostic
        # and cheaper than round-tripping through XPath.
        root = text_node.root
        if exclude_tags:
            getparent = getattr(root, "getparent", None)
            parent = getparent() if getparent is not None else None
            parent_tag = getattr(parent, "tag", None)
            if isinstance(parent_tag, str) and parent_tag.lower() in exclude_tags:
                continue
        text = str(root)
        if text.strip():
            nodes.append(text.strip())
    return nodes


def _first_regex_match(nodes: list[str], pattern: str) -> tuple[str, str] | None:
    """``(matched_group, matched_text_node)`` for the first node matching ``pattern``, else ``None``.

    When ``REGEX_BOUNDS_ENABLED`` is on, the pattern is pre-flighted once
    through the W3.2 bounded engine and the text it may scan is capped —
    see the bounds block above. With the flag off this is the original,
    unbounded function.
    """
    if REGEX_BOUNDS_ENABLED and _pattern_refused(pattern):
        # Fail closed: a refused pattern finds nothing. Not an exception —
        # the page's other four readings must still happen.
        return None
    try:
        compiled = re.compile(pattern)
    except re.error:
        return None
    budget = REGEX_BOUNDS_MAX_TOTAL_CHARS
    for node in nodes:
        if REGEX_BOUNDS_ENABLED:
            if budget <= 0:
                return None
            subject = node[: min(REGEX_BOUNDS_MAX_NODE_CHARS, budget)]
            budget -= len(subject)
        else:
            subject = node
        match = compiled.search(subject)
        if match:
            value = match.group(1) if match.groups() else match.group(0)
            # The FULL node is still the returned `matched_text`: it is
            # evidence and downstream `reject_if_text_contains` context, and
            # truncating it would change what a validation rule sees.
            return value, node
    return None


def _stock_from_token(value: str | None) -> StockStatus | None:
    """Classify a regex's *captured value* as a compact stock token, else ``None``.

    A stock_regex reaching into a JSON blob captures the status code
    itself ("outOfStock"); classifying the surrounding node text would be
    meaningless there — blobs carry i18n labels like "Out Of Stock" on
    every page regardless of actual status.
    """
    if not value:
        return None
    token = value.strip().lower()
    if token in _OUT_OF_STOCK_TOKENS:
        return StockStatus.OUT_OF_STOCK
    if token in _IN_STOCK_TOKENS:
        return StockStatus.IN_STOCK
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
            # Captured value as a compact token first; node-text phrase
            # classification only as the fallback for visible-text rules.
            stock = _stock_from_token(stock_match[0]) or _stock_from_text(stock_match[1])

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
