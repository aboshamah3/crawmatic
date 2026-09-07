"""Regex Product extractor + the single-number heuristic (contracts/extraction.md #3, SPEC-07 US3 T033).

Pure ``parsel`` (for text-node segmentation) + the ``regex`` engine for
every DB-supplied pattern (stdlib ``re`` is kept only for this module's own
hard-coded expressions, which cannot backtrack pathologically) — no
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

import contextlib
import contextvars
import re
import time
from collections.abc import Iterator
from typing import Any

import regex as regex_engine
from parsel import Selector

from app_shared.config import get_settings
from app_shared.enums import ExtractionMethod, StockStatus
from app_shared.profiles.confidence import resolve_confidence_rules

from scrape_core.extraction.result import ExtractionCandidate

__all__ = [
    "RegexDeadlineExceeded",
    "extract_regex",
    "regex_deadline_tripped",
    "regex_deadline_watch",
    "search_with_deadline",
]

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
# A2/F02 (2026-09-07): the three bounds below are now
# `app_shared.config.Settings` fields — `EXTRACTION_REGEX_BOUNDS_ENABLED`,
# `EXTRACTION_REGEX_BOUNDS_MAX_NODE_CHARS` and
# `EXTRACTION_REGEX_BOUNDS_MAX_TOTAL_CHARS` (the TODO(config) that used to
# stand here is discharged). They stay DEFAULT OFF: `search_with_deadline`
# below is the primary containment now, and it is unconditional.
#
# The module-level names are kept as read-through accessors rather than
# constants so `get_settings()` (cached, env-overridable) is the single
# source of truth and no import-time snapshot can go stale.


#: Shipped defaults, duplicated from `app_shared.config.Settings` on purpose.
#: They are what `_setting` falls back to when a process cannot build a
#: `Settings` at all — which in practice means only the unit suite, whose
#: environment is deliberately incomplete (`tests/conftest.py`). Every real
#: process validates its configuration at startup, long before a page is
#: extracted, so the fallback never decides anything in production; it exists
#: so that "config is unavailable" degrades to the documented default instead
#: of costing the page an exception from inside a text-node loop.
_DEFAULTS: dict[str, Any] = {
    "EXTRACTION_REGEX_TIMEOUT_SECONDS": 0.25,
    "EXTRACTION_REGEX_BOUNDS_ENABLED": False,
    "EXTRACTION_REGEX_BOUNDS_MAX_NODE_CHARS": 65_536,
    "EXTRACTION_REGEX_BOUNDS_MAX_TOTAL_CHARS": 1_048_576,
}


def _setting(name: str) -> Any:
    try:
        return getattr(get_settings(), name)
    except Exception:  # noqa: BLE001 - see _DEFAULTS
        return _DEFAULTS[name]


def _bounds_enabled() -> bool:
    return bool(_setting("EXTRACTION_REGEX_BOUNDS_ENABLED"))


def _bounds_max_node_chars() -> int:
    return int(_setting("EXTRACTION_REGEX_BOUNDS_MAX_NODE_CHARS"))


def _bounds_max_total_chars() -> int:
    return int(_setting("EXTRACTION_REGEX_BOUNDS_MAX_TOTAL_CHARS"))


def _regex_timeout_seconds() -> float:
    return float(_setting("EXTRACTION_REGEX_TIMEOUT_SECONDS"))


# ---------------------------------------------------------------------------
# Hard execution deadline (A2/F02)
# ---------------------------------------------------------------------------


class RegexDeadlineExceeded(Exception):
    """A profile-supplied regex ran past its wall-clock deadline.

    Carries the offending ``pattern`` and the ``timeout`` (seconds) it blew,
    so the caller can name the profile field in a ``REGEX_TIMEOUT``
    observation without re-deriving anything.
    """

    def __init__(self, pattern: str, timeout: float) -> None:
        self.pattern = pattern
        self.timeout = timeout
        super().__init__(f"regex {pattern!r} exceeded its {timeout}s deadline")


def search_with_deadline(
    pattern: str, subject: str, *, timeout: float
) -> regex_engine.Match | None:
    """``pattern.search(subject)`` that cannot run longer than ``timeout``.

    Uses the third-party ``regex`` engine, whose ``search()`` accepts a
    ``timeout=`` and raises :class:`TimeoutError`; CPython's stdlib ``re``
    offers no such thing (no timeout, no signal, no thread interrupts a
    catastrophic backtrack), which is exactly why every DB-supplied pattern
    goes through here.

    Raises :class:`RegexDeadlineExceeded` on deadline. A pattern that does
    not compile raises ``regex.error`` — callers on the live path treat that
    the same way they always treated ``re.error``: the rule simply finds
    nothing.
    """
    compiled = regex_engine.compile(pattern)
    try:
        return compiled.search(subject, timeout=timeout)
    except TimeoutError as exc:
        raise RegexDeadlineExceeded(pattern, timeout) from exc


# The signal the *caller* (the spider) reads to record
# `ScrapeErrorCode.REGEX_TIMEOUT` instead of `PRICE_NOT_FOUND`. A ContextVar
# rather than a threaded-through parameter because the deadline is raised
# four layers below the classification site (`extract_regex` ->
# `pipeline.extract` -> `adapters/html.adapt` -> the spider's `parse`) and
# every one of those signatures is public API pinned by its own tests.
_DEADLINE_TRIPPED: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "scrape_core_regex_deadline_tripped", default=None
)


@contextlib.contextmanager
def regex_deadline_watch() -> Iterator[None]:
    """Scope in which a regex deadline is recorded rather than lost.

    Inside the block, :func:`regex_deadline_tripped` returns the offending
    ``{"pattern": ..., "timeout": ...}`` once a deadline has fired, else
    ``None``. The token is reset on exit, so nesting and reuse across
    requests on the same thread/task are safe.
    """
    token = _DEADLINE_TRIPPED.set(None)
    try:
        yield
    finally:
        _DEADLINE_TRIPPED.reset(token)


def regex_deadline_tripped() -> dict[str, Any] | None:
    """The deadline recorded in the current :func:`regex_deadline_watch`, if any."""
    return _DEADLINE_TRIPPED.get()


def _record_deadline(exc: RegexDeadlineExceeded) -> None:
    try:
        _DEADLINE_TRIPPED.set({"pattern": exc.pattern, "timeout": exc.timeout})
    except LookupError:  # pragma: no cover - ContextVar has a default
        pass


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

    Every node is searched through :func:`search_with_deadline`, so no single
    node can cost more than ``EXTRACTION_REGEX_TIMEOUT_SECONDS``. On top of
    that per-node deadline there is a **cumulative per-page budget of
    ``4 x timeout``**: a real product page has thousands of text nodes
    (6,932 on a live amazon.sa page), so a per-node cap alone is not a
    budget. Either bound firing raises :class:`RegexDeadlineExceeded` —
    ``extract_regex`` turns that into ``None`` and the deadline is recorded
    for the caller (see :func:`regex_deadline_watch`).

    When ``EXTRACTION_REGEX_BOUNDS_ENABLED`` is on, the pattern is
    additionally pre-flighted once through the W3.2 bounded engine and the
    text it may scan is capped — see the bounds block above.
    """
    bounds_on = _bounds_enabled()
    if bounds_on and _pattern_refused(pattern):
        # Fail closed: a refused pattern finds nothing. Not an exception —
        # the page's other four readings must still happen.
        return None
    try:
        regex_engine.compile(pattern)
    except (regex_engine.error, re.error, ValueError):
        return None

    per_node_timeout = _regex_timeout_seconds()
    page_budget = 4 * per_node_timeout
    spent = 0.0
    char_budget = _bounds_max_total_chars()
    max_node_chars = _bounds_max_node_chars()

    for node in nodes:
        if bounds_on:
            if char_budget <= 0:
                return None
            subject = node[: min(max_node_chars, char_budget)]
            char_budget -= len(subject)
        else:
            subject = node

        remaining = page_budget - spent
        if remaining <= 0:
            raise RegexDeadlineExceeded(pattern, page_budget)
        started = time.monotonic()
        match = search_with_deadline(
            pattern, subject, timeout=min(per_node_timeout, remaining)
        )
        spent += time.monotonic() - started

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


def _regex_quarantined(profile: Any) -> bool:
    """``True`` when this profile's regex strategy is quarantined (A2/F02).

    Reads ``regex_quarantined_at`` off the resolved profile row. Absent
    attribute (a stub/dict-shaped profile in a test, or a pre-migration
    row) means "not quarantined" — this must never fail closed on a
    profile that simply predates the column.
    """
    return getattr(profile, "regex_quarantined_at", None) is not None


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
        if _regex_quarantined(profile):
            # A2/F02 step 4: the profile's regex strategy is quarantined
            # (it blew its deadline `EXTRACTION_REGEX_QUARANTINE_AFTER`
            # times). Skip the DB rules entirely — the page still gets the
            # single-number heuristic and, through the pipeline, its other
            # four readings.
            return _extract_single_number(html, confidence_rules["single_number"])
        nodes = _text_nodes(html)
        try:
            candidate = _extract_via_price_regex(
                nodes, profile, price_regex, confidence_rules["regex"]
            )
        except RegexDeadlineExceeded as exc:
            # Never raised at the caller: a blown deadline costs this page
            # its REGEX reading, not the other four. The deadline is
            # recorded so the spider stamps `REGEX_TIMEOUT` rather than
            # `PRICE_NOT_FOUND` if nothing else reads a price.
            _record_deadline(exc)
            return None
        if candidate is not None:
            return candidate

    return _extract_single_number(html, confidence_rules["single_number"])
