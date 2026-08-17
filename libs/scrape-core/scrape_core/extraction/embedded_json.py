"""Embedded-JSON Product extractor (Task 3.1, handover 2026-08-15 §7).

Pure ``parsel`` + stdlib ``json`` — no reactor, no Scrapy ``Response``
object required, so this is fully unit-testable off-reactor against
fixture HTML, same as ``jsonld.py``/``css.py``.

Where ``jsonld.py`` understands one *standard* vocabulary (schema.org
``Product``/``Offer``), this strategy understands none: it collects every
inline ``<script>`` body that parses as JSON and resolves a
DB-configured **RFC 6901 JSON pointer** (``price_json_path`` and its
``old_price``/``currency``/``stock``/``title`` siblings on
``scrape_profiles``, mirroring the ``*_selector``/``*_xpath``/``*_regex``
convention) against each. A profile with no ``price_json_path`` is an
immediate ``None`` — this strategy is strictly opt-in and can never
change behaviour for a domain that has not been configured for it.

Script bodies considered, in order:

1. ``<script id="__NEXT_DATA__">`` — the Next.js Pages Router payload.
2. ``<script type="application/json">`` — the generic embedded-state tag.
3. Any other inline ``<script>``, scanned for a ``var X = {...};`` /
   ``window.X = {...};`` assignment.

**How the assignment body is bounded.** Not by a regex and not by a
hand-rolled brace counter: the scanner finds each ``= {``/``= [`` and
hands the offset to :meth:`json.JSONDecoder.raw_decode`, which consumes
exactly one well-formed JSON value and reports where it ended. That is a
real parser, so quoted braces, escapes and nesting are all handled, and
anything that is JavaScript-but-not-JSON (unquoted keys, ``!0``,
template literals, trailing commas) simply fails to decode and is
skipped. The cost of scanning is bounded by ``_MAX_ASSIGNMENT_PROBES``
per script and ``_MAX_DOCUMENTS`` overall.

**Known limitation (measured, not theoretical).** A live noon product
page captured 2026-08-16 (``tests/fixtures/html/noon_product_real.html``)
carries *no* JSON script body at all: noon has migrated off Next.js and
serializes its SSR state as a TanStack Router JS expression (``$R[n]=``
reference captures, ``!0``/``!1`` booleans, unquoted keys). That is not
JSON and this module deliberately does not try to make it into JSON —
``json.loads`` on a best-effort de-JavaScripted string is exactly the
class of silent-corruption bug this strategy exists to replace. noon's
embedded truth is reachable today through ``price_regex`` instead; see
the fixture header and the Task 3.1 report.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from parsel import Selector

from app_shared.enums import ExtractionMethod, StockStatus
from app_shared.profiles.confidence import resolve_confidence_rules

from scrape_core.extraction.result import ExtractionCandidate

__all__ = ["extract_embedded_json"]

#: XPaths for the script bodies worth parsing, cheapest/most-likely first.
_NEXT_DATA_XPATH = '//script[@id="__NEXT_DATA__"]/text()'
_APPLICATION_JSON_XPATH = '//script[@type="application/json"]/text()'
_INLINE_SCRIPT_XPATH = "//script[not(@src)]/text()"

#: Upper bound on JSON documents parsed out of one page. A page with more
#: embedded blobs than this is pathological; the pointer that matters is
#: overwhelmingly in the first one or two.
_MAX_DOCUMENTS = 40

#: Upper bound on ``= {``/``= [`` offsets probed inside a single script.
_MAX_ASSIGNMENT_PROBES = 8

#: ``raw_price_text`` for a **stock-only** candidate — one emitted for a
#: page that positively reports the item as unavailable and, precisely
#: because of that, carries no price to read.
#:
#: This deliberately mirrors the shape JSON-LD already produces for the
#: very same real-world state rather than inventing a second one. noon's
#: unavailable pages publish ``"price": 0`` next to
#: ``availability: OutOfStock``, so ``extract_jsonld`` surfaces
#: ``raw_price_text="0"`` with ``stock=OUT_OF_STOCK``; validation rejects
#: it (``INVALID_PRICE_FORMAT``, "price must be greater than 0"), the
#: spider's ``Rejected`` branch still threads ``candidate_extras``, and so
#: ``stock_status=OUT_OF_STOCK`` reaches ``price_observations`` and
#: ``pipelines`` upserts the out-of-stock badge onto
#: ``match_current_prices``. Emitting the identical shape keeps ONE
#: downstream path for ONE real-world state.
#:
#: An embedded payload that omits the price entirely (noon's real
#: ``variants:[{offers: []}]``) is the same state expressed differently,
#: and must not degrade to ``PRICE_NOT_FOUND`` — on a real noon page none
#: of ``_sniff_out_of_stock``'s markers are present, so the availability
#: signal would simply be lost.
_NO_PRICE_SENTINEL = "0"

#: ``matched_text`` cap. The enclosing object is serialized for
#: ``reject_if_text_contains`` rules to match against; an embedded state
#: blob can be hundreds of KB and that text is persisted on every
#: observation row.
_MAX_MATCHED_TEXT = 4000

# Availability vocabularies. Mirrors `jsonld._stock_from_availability`
# (schema.org URLs / bare tokens) plus the snake_case and SCREAMING_CASE
# spellings that hand-rolled app-state blobs actually use. Anything
# recognized but not explicitly in-stock is out-of-stock; anything
# unrecognized is UNKNOWN — never guessed as in-stock.
_OUT_OF_STOCK_TOKENS = frozenset(
    {
        "outofstock",
        "soldout",
        "discontinued",
        "backorder",
        "preorder",
        "unavailable",
        "notavailable",
        "nostock",
        "false",
    }
)
_IN_STOCK_TOKENS = frozenset(
    {
        "instock",
        "inventoryinstock",
        "limitedavailability",
        "lowstock",
        "onlineonly",
        "instoreonly",
        "available",
        "buyable",
        "true",
    }
)


def _unescape_pointer_token(token: str) -> str:
    """RFC 6901 §3 unescaping: ``~1`` -> ``/``, then ``~0`` -> ``~``.

    Order matters — doing ``~0`` first would turn ``~01`` into ``~1``
    and then into ``/`` instead of the literal ``~1`` it denotes.
    """
    return token.replace("~1", "/").replace("~0", "~")


def _resolve_pointer(document: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 JSON pointer, or ``None`` on any miss.

    Every failure mode — a missing key, a non-numeric or out-of-range
    array index, descending into a scalar, a malformed pointer — is a
    plain ``None``, never an exception: the caller falls through to the
    next document / the next strategy / ``PRICE_NOT_FOUND``.

    A leading ``/`` is optional as an operator convenience: ``a/b`` and
    ``/a/b`` mean the same thing. The empty pointer denotes the whole
    document (RFC 6901 §5).
    """
    if pointer == "":
        return document
    if pointer.startswith("/"):
        pointer = pointer[1:]

    current = document
    for raw_token in pointer.split("/"):
        token = _unescape_pointer_token(raw_token)
        if isinstance(current, dict):
            if token not in current:
                return None
            current = current[token]
        elif isinstance(current, list):
            # RFC 6901 §4: an array index is unsigned digits only. "01"
            # is invalid, and "-" (past-the-end) has no referent here.
            if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
                return None
            index = int(token)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def _scalar_or_none(value: Any) -> str | None:
    """``str`` of a JSON scalar, or ``None`` for containers/``None``/``bool``.

    A ``dict``/``list`` hit means the pointer stopped short of the value
    and is a miss, not a price. ``bool`` is excluded explicitly because
    it is an ``int`` subclass in Python and ``"True"`` is not a price.
    """
    if value is None or isinstance(value, bool | dict | list):
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, int | float):
        return str(value)
    return None


def _optional_scalar(document: Any, pointer: str | None) -> str | None:
    """``_scalar_or_none`` for a pointer the profile may not have configured.

    An unset pointer is ``None`` (the field simply was not asked for) —
    distinct from a configured pointer that misses, which is also
    ``None`` but for a reason the operator can debug from
    ``selector_used``.
    """
    if not pointer:
        return None
    return _scalar_or_none(_resolve_pointer(document, pointer))


def _stock_from_value(value: Any) -> StockStatus | None:
    """Classify an availability value read out of an embedded JSON blob.

    Handles the three shapes these blobs actually use: a schema.org URL
    or status token (``"https://schema.org/OutOfStock"``,
    ``"outOfStock"``), a boolean (``is_buyable``-style), and a numeric
    on-hand count (``stock: 7``). ``None`` when nothing was configured
    or read; ``UNKNOWN`` when a value was read but not recognized.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return StockStatus.IN_STOCK if value else StockStatus.OUT_OF_STOCK
    if isinstance(value, int | float):
        return StockStatus.IN_STOCK if value > 0 else StockStatus.OUT_OF_STOCK
    if not isinstance(value, str):
        return StockStatus.UNKNOWN

    token = value.rsplit("/", 1)[-1].strip().replace("_", "").replace("-", "").lower()
    if not token:
        return None
    if token in _IN_STOCK_TOKENS:
        return StockStatus.IN_STOCK
    if token in _OUT_OF_STOCK_TOKENS:
        return StockStatus.OUT_OF_STOCK
    return StockStatus.UNKNOWN


def _decode_whole(body: str) -> Any:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _decode_assignments(body: str) -> Iterator[Any]:
    """Yield each ``... = {...}`` / ``... = [...]`` JSON value in ``body``.

    ``raw_decode`` does the bounding: it consumes exactly one JSON value
    from the given offset and raises if what starts there is not one.
    """
    decoder = json.JSONDecoder()
    probes = 0
    search_from = 0
    while probes < _MAX_ASSIGNMENT_PROBES:
        equals = body.find("=", search_from)
        if equals == -1:
            return
        start = equals + 1
        while start < len(body) and body[start].isspace():
            start += 1
        if start >= len(body) or body[start] not in "{[":
            search_from = equals + 1
            continue
        probes += 1
        try:
            value, end = decoder.raw_decode(body, start)
        except (json.JSONDecodeError, ValueError):
            search_from = equals + 1
            continue
        search_from = end
        yield value


def _iter_documents(selector: Selector) -> Iterator[Any]:
    """Every JSON document embedded in the page, best candidates first."""
    seen_bodies: set[str] = set()
    emitted = 0

    for xpath in (_NEXT_DATA_XPATH, _APPLICATION_JSON_XPATH, _INLINE_SCRIPT_XPATH):
        for body in selector.xpath(xpath).getall():
            stripped = body.strip()
            if not stripped or stripped in seen_bodies:
                continue
            seen_bodies.add(stripped)

            document = _decode_whole(stripped)
            if document is not None:
                emitted += 1
                yield document
                if emitted >= _MAX_DOCUMENTS:
                    return
                continue

            for document in _decode_assignments(stripped):
                emitted += 1
                yield document
                if emitted >= _MAX_DOCUMENTS:
                    return


def _matched_text(document: Any, price_pointer: str) -> str:
    """The object *enclosing* the price, serialized and length-capped.

    This is what a ``reject_if_text_contains`` rule matches against, so
    it needs the price's siblings (``"strikethrough"``, ``"installment"``
    …), not the bare number — the same reason ``css.py`` reaches for the
    matched element's parent.
    """
    parent_pointer = price_pointer.rsplit("/", 1)[0] if "/" in price_pointer else ""
    parent = _resolve_pointer(document, parent_pointer)
    if parent is None:
        parent = document
    return json.dumps(parent, default=str, ensure_ascii=False)[:_MAX_MATCHED_TEXT]


def extract_embedded_json(html: str, *, profile: Any = None) -> ExtractionCandidate | None:
    """Resolve the profile's ``price_json_path`` against embedded JSON blobs.

    Returns ``None`` — never raises — when the profile configures no
    ``price_json_path``, when ``embedded_json_enabled`` is explicitly
    ``False``, when the page embeds no parseable JSON, or when no
    embedded document resolves the pointer to a scalar. The caller (the
    pipeline orchestrator) falls through to the next strategy /
    ``PRICE_NOT_FOUND``.

    The first document whose ``price_json_path`` resolves wins, and every
    other pointer (``currency``/``stock``/``title``/``old_price``) is
    then resolved **against that same document** — mixing fields across
    two unrelated blobs would silently pair one seller's price with
    another's availability.

    **Stock-only candidates.** If no document resolves the price pointer
    but one resolves ``stock_json_path`` to ``OUT_OF_STOCK``, this returns
    a price-less candidate carrying that classification
    (``raw_price_text`` = :data:`_NO_PRICE_SENTINEL`) instead of ``None``.
    An unavailable item having no price is an *answer*, not an extraction
    miss, and the real shape it takes on noon is an empty ``offers``
    array with the availability field sitting elsewhere on the product —
    returning ``None`` there would degrade to ``PRICE_NOT_FOUND`` and
    lose the only availability signal the page carries. A missing price
    with any *other* availability reading (in stock, unrecognized, or no
    stock pointer configured) still returns ``None`` so the chain falls
    through to CSS/regex.
    """
    if profile is None:
        return None
    if getattr(profile, "embedded_json_enabled", True) is False:
        return None

    price_pointer = getattr(profile, "price_json_path", None)
    if not price_pointer:
        return None

    # parsel (<=1.11) tries json.loads(text) BEFORE honoring type="html";
    # a JSON-parseable body yields a 'json' Selector on which .xpath()
    # raises (see regex.py). A bare JSON document has no <script> tags.
    selector = Selector(text=html, type="html")
    if selector.type != "html":
        return None

    stock_pointer = getattr(profile, "stock_json_path", None)
    confidence = resolve_confidence_rules(getattr(profile, "confidence_rules", None))[
        "embedded_json"
    ]
    stock_only: ExtractionCandidate | None = None

    for document in _iter_documents(selector):
        stock = (
            _stock_from_value(_resolve_pointer(document, stock_pointer))
            if stock_pointer
            else None
        )

        raw_price_text = _scalar_or_none(_resolve_pointer(document, price_pointer))
        if raw_price_text is None:
            # No price at this pointer. Two very different causes, and
            # only the availability field can tell them apart:
            #
            #   * the page says the item is UNAVAILABLE -> there is no
            #     price to find, and that is an answer, not a miss. Hold a
            #     stock-only candidate (see `_NO_PRICE_SENTINEL`).
            #   * anything else (in stock / unrecognized / no stock
            #     pointer configured) -> extraction genuinely missed a
            #     price that may well be on the page. Stay silent so the
            #     chain falls through to CSS/regex, which is the whole
            #     point of being a chain.
            #
            # Either way keep scanning: a later document may carry a real
            # price, and a real price always beats the fallback.
            if stock is StockStatus.OUT_OF_STOCK and stock_only is None:
                stock_only = ExtractionCandidate(
                    raw_price_text=_NO_PRICE_SENTINEL,
                    currency=_optional_scalar(
                        document, getattr(profile, "currency_json_path", None)
                    ),
                    method=ExtractionMethod.EMBEDDED_JSON,
                    confidence=confidence,
                    # The pointer that actually resolved, so an operator
                    # debugging the row sees which rule fired.
                    selector_used=stock_pointer,
                    raw_title=_optional_scalar(
                        document, getattr(profile, "title_json_path", None)
                    ),
                    stock=StockStatus.OUT_OF_STOCK,
                    matched_text=_matched_text(document, stock_pointer or ""),
                )
            continue

        currency = _optional_scalar(document, getattr(profile, "currency_json_path", None))
        title = _optional_scalar(document, getattr(profile, "title_json_path", None))

        matched_text = _matched_text(document, price_pointer)
        old_price_text = _optional_scalar(document, getattr(profile, "old_price_json_path", None))
        if old_price_text and old_price_text not in matched_text:
            matched_text = f"{matched_text} {old_price_text}".strip()

        return ExtractionCandidate(
            raw_price_text=raw_price_text,
            currency=currency,
            method=ExtractionMethod.EMBEDDED_JSON,
            confidence=confidence,
            selector_used=price_pointer,
            raw_title=title,
            stock=stock,
            matched_text=matched_text,
        )

    # No document carried a price. If one of them positively said the item
    # is out of stock, that is the answer.
    return stock_only
