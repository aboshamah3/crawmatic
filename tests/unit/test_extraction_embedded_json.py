"""``EMBEDDED_JSON`` extraction strategy tests (Task 3.1, handover 2026-08-15 §7).

Two distinct bodies of test live here and they must not be confused:

* **Adapter contract** — exercised against
  ``tests/fixtures/html/embedded_json_next_data*.html``, which are
  **synthetic** fixtures modelling the generic Next.js shape the adapter
  is specified against (JSON-LD advertising ``"price": 0`` while
  ``__NEXT_DATA__`` carries the truth).
* **noon reality** — exercised against
  ``tests/fixtures/html/noon_product_real.html`` and
  ``noon_unavailable_real.html``, both **real captures** taken
  2026-08-16. They pin the two facts that Task 3.1's premise turned out
  to get wrong: noon serves no ``__NEXT_DATA__`` any more (its SSR state
  is a TanStack Router JS expression, not JSON), and a noon page whose
  JSON-LD says ``"price": 0`` carries no hidden true price either — its
  embedded ``offers`` array is genuinely empty.
"""

from __future__ import annotations

from pathlib import Path

from app_shared.enums import ExtractionMethod, ScrapeErrorCode, StockStatus

from scrape_core.extraction.embedded_json import extract_embedded_json
from scrape_core.extraction.jsonld import extract_jsonld
from scrape_core.extraction.pipeline import _METHOD_TO_STRATEGY, extract
from scrape_core.extraction.regex import extract_regex
from scrape_core.validation import Rejected, validate_candidate

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "html"

_PRICE_PATH = "/props/pageProps/product/offers/0/pricing/amount"
_CURRENCY_PATH = "/props/pageProps/product/offers/0/pricing/currency"
_STOCK_PATH = "/props/pageProps/product/offers/0/availability"
_TITLE_PATH = "/props/pageProps/product/title"


def _read_fixture(name: str) -> str:
    return (_FIXTURES_DIR / name).read_text(encoding="utf-8")


class _JsonPathProfile:
    """Pointers matching ``tests/fixtures/html/embedded_json_next_data.html``."""

    price_json_path = _PRICE_PATH
    currency_json_path = _CURRENCY_PATH
    stock_json_path = _STOCK_PATH
    title_json_path = _TITLE_PATH


class _JsonPathNoJsonLdProfile(_JsonPathProfile):
    """The shape a "JSON-LD lies here" domain actually needs.

    Chain position alone cannot beat a lying JSON-LD block: JSON-LD runs
    first and ``"price": 0`` is a hit, not a miss. The profile has to say
    so, exactly as extra.com's stale-JSON-LD profile already does
    (``test_pipeline_extra_appstate_regex_beats_stale_jsonld_when_disabled``).
    """

    jsonld_enabled = False


class _NoJsonPathProfile:
    """A profile that configures no JSON pointer at all (the status quo)."""

    price_json_path = None


class _NoPriceProfile:
    """Pointers matching ``embedded_json_next_data_no_price.html``.

    The price pointer walks into an **empty** ``offers`` array and so
    resolves to nothing, exactly as it would on the real noon
    ``variants:[{offers: []}]`` shape; availability sits on the product
    instead and resolves fine.
    """

    jsonld_enabled = False
    price_json_path = "/props/pageProps/product/offers/0/pricing/amount"
    stock_json_path = "/props/pageProps/product/availability"
    title_json_path = "/props/pageProps/product/title"


# --- extract_embedded_json ----------------------------------------------------


def test_embedded_json_reads_next_data_price_currency_title_at_confidence_0_90() -> None:
    html = _read_fixture("embedded_json_next_data.html")

    candidate = extract_embedded_json(html, profile=_JsonPathProfile())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.EMBEDDED_JSON
    assert candidate.raw_price_text == "249.5"
    assert candidate.currency == "SAR"
    assert candidate.raw_title == "Widget Pro 2000"
    assert candidate.stock == StockStatus.IN_STOCK
    assert candidate.confidence == 0.90
    assert candidate.selector_used == _PRICE_PATH


def test_embedded_json_returns_none_without_a_configured_price_json_path() -> None:
    html = _read_fixture("embedded_json_next_data.html")

    assert extract_embedded_json(html, profile=_NoJsonPathProfile()) is None
    assert extract_embedded_json(html) is None


def test_embedded_json_skipped_when_profile_disables_it() -> None:
    html = _read_fixture("embedded_json_next_data.html")

    class _Disabled(_JsonPathProfile):
        embedded_json_enabled = False

    assert extract_embedded_json(html, profile=_Disabled()) is None


def test_embedded_json_confidence_overridden_by_profile_confidence_rules() -> None:
    html = _read_fixture("embedded_json_next_data.html")

    class _Tuned(_JsonPathProfile):
        confidence_rules = {"embedded_json": 0.62}

    candidate = extract_embedded_json(html, profile=_Tuned())

    assert candidate is not None
    assert candidate.confidence == 0.62


def test_embedded_json_unescapes_rfc6901_tilde_and_slash_pointer_tokens() -> None:
    """``~1`` is a literal ``/`` in a key, ``~0`` a literal ``~`` (RFC 6901 §3)."""
    html = (
        "<html><body>"
        '<script type="application/json">'
        '{"a/b": {"c~d": {"amount": "77.25"}}}'
        "</script></body></html>"
    )

    class _Escaped:
        price_json_path = "/a~1b/c~0d/amount"

    candidate = extract_embedded_json(html, profile=_Escaped())

    assert candidate is not None
    assert candidate.raw_price_text == "77.25"


def test_embedded_json_resolves_list_indices() -> None:
    html = (
        "<html><body>"
        '<script type="application/json">{"offers": [{"p": 1}, {"p": 2}, {"p": 3}]}</script>'
        "</body></html>"
    )

    class _Indexed:
        price_json_path = "/offers/2/p"

    candidate = extract_embedded_json(html, profile=_Indexed())

    assert candidate is not None
    assert candidate.raw_price_text == "3"


def test_embedded_json_out_of_range_index_is_a_clean_miss() -> None:
    html = '<html><body><script type="application/json">{"o": [{"p": 1}]}</script></body></html>'

    class _TooFar:
        price_json_path = "/o/9/p"

    assert extract_embedded_json(html, profile=_TooFar()) is None


def test_embedded_json_missing_key_is_a_clean_miss_not_an_exception() -> None:
    html = _read_fixture("embedded_json_next_data.html")

    class _Wrong:
        price_json_path = "/props/pageProps/product/nope/0/amount"

    assert extract_embedded_json(html, profile=_Wrong()) is None


def test_embedded_json_pointer_resolving_to_a_container_is_a_clean_miss() -> None:
    """A price must be a scalar — a dict/list/None hit is not a price."""
    html = _read_fixture("embedded_json_next_data.html")

    class _Container:
        price_json_path = "/props/pageProps/product/offers/0/pricing"

    assert extract_embedded_json(html, profile=_Container()) is None


def test_embedded_json_skips_malformed_script_and_uses_the_next_one() -> None:
    html = (
        "<html><body>"
        '<script type="application/json">{"amount": NOT VALID JSON,,,</script>'
        '<script type="application/json">{"amount": "58.00"}</script>'
        "</body></html>"
    )

    class _Amount:
        price_json_path = "/amount"

    candidate = extract_embedded_json(html, profile=_Amount())

    assert candidate is not None
    assert candidate.raw_price_text == "58.00"


def test_embedded_json_skips_a_document_that_lacks_the_pointer() -> None:
    html = (
        "<html><body>"
        '<script type="application/json">{"unrelated": {"amount": "1.00"}}</script>'
        '<script type="application/json">{"amount": "42.00"}</script>'
        "</body></html>"
    )

    class _Amount:
        price_json_path = "/amount"

    candidate = extract_embedded_json(html, profile=_Amount())

    assert candidate is not None
    assert candidate.raw_price_text == "42.00"


def test_embedded_json_reads_a_var_assignment_script_body() -> None:
    html = (
        "<html><body><script>\n"
        "  window.dataLayer = window.dataLayer || [];\n"
        '  var __APP_STATE__ = {"product": {"price": "310.00", "sold": false}};\n'
        "  boot(__APP_STATE__);\n"
        "</script></body></html>"
    )

    class _AppState:
        price_json_path = "/product/price"
        stock_json_path = "/product/sold"

    candidate = extract_embedded_json(html, profile=_AppState())

    assert candidate is not None
    assert candidate.raw_price_text == "310.00"


def test_embedded_json_returns_none_when_no_script_body_parses_as_json() -> None:
    html = "<html><body><script>boot({a: !0, b: `x`});</script></body></html>"

    class _Any:
        price_json_path = "/a"

    assert extract_embedded_json(html, profile=_Any()) is None


def test_embedded_json_on_a_json_document_returns_none_without_crashing() -> None:
    """parsel types a JSON-parseable body as 'json'; ``.css()`` would raise."""

    class _Any:
        price_json_path = "/a"

    assert extract_embedded_json('{"a": 1}', profile=_Any()) is None


# --- availability classification (brief Step 4) -------------------------------


def test_embedded_json_classifies_out_of_stock_from_the_availability_pointer() -> None:
    html = _read_fixture("embedded_json_next_data_oos.html")

    candidate = extract_embedded_json(html, profile=_JsonPathProfile())

    assert candidate is not None
    assert candidate.raw_price_text == "189.0"
    assert candidate.stock == StockStatus.OUT_OF_STOCK


def test_embedded_json_classifies_boolean_and_numeric_availability_values() -> None:
    def _stock_for(value: str) -> StockStatus | None:
        html = (
            "<html><body>"
            f'<script type="application/json">{{"p": "9.99", "a": {value}}}</script>'
            "</body></html>"
        )

        class _P:
            price_json_path = "/p"
            stock_json_path = "/a"

        candidate = extract_embedded_json(html, profile=_P())
        assert candidate is not None
        return candidate.stock

    assert _stock_for("true") == StockStatus.IN_STOCK
    assert _stock_for("false") == StockStatus.OUT_OF_STOCK
    assert _stock_for("7") == StockStatus.IN_STOCK
    assert _stock_for("0") == StockStatus.OUT_OF_STOCK
    assert _stock_for('"outOfStock"') == StockStatus.OUT_OF_STOCK
    assert _stock_for('"IN_STOCK"') == StockStatus.IN_STOCK
    assert _stock_for('"https://schema.org/SoldOut"') == StockStatus.OUT_OF_STOCK
    assert _stock_for('"nothing recognizable"') == StockStatus.UNKNOWN


# --- price-less out-of-stock (review fix round 1, finding 2) ------------------


def test_embedded_json_emits_stock_only_candidate_when_offers_array_is_empty() -> None:
    """The real noon shape: no price anywhere, availability says unavailable.

    Returning ``None`` here would degrade to ``PRICE_NOT_FOUND``, and on a
    real noon page none of ``_sniff_out_of_stock``'s markers are present,
    so the only availability signal on the page would be lost.
    """
    html = _read_fixture("embedded_json_next_data_no_price.html")

    candidate = extract_embedded_json(html, profile=_NoPriceProfile())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.EMBEDDED_JSON
    assert candidate.stock == StockStatus.OUT_OF_STOCK
    assert candidate.raw_title == "Widget Pro 2000"
    # Mirrors the shape JSON-LD already emits for this same real-world
    # state (noon publishes `"price": 0` + OutOfStock).
    assert candidate.raw_price_text == "0"
    assert candidate.selector_used == "/props/pageProps/product/availability"


def test_embedded_json_emits_stock_only_candidate_when_price_is_null() -> None:
    """``"amount": null`` is a price-less page too, not a malformed one."""
    html = (
        "<html><body>"
        '<script type="application/json">'
        '{"amount": null, "availability": "outOfStock"}'
        "</script></body></html>"
    )

    class _P:
        price_json_path = "/amount"
        stock_json_path = "/availability"

    candidate = extract_embedded_json(html, profile=_P())

    assert candidate is not None
    assert candidate.stock == StockStatus.OUT_OF_STOCK
    assert candidate.raw_price_text == "0"


def test_embedded_json_stays_silent_when_price_is_missing_but_item_is_in_stock() -> None:
    """An in-stock page with no price at the pointer is a genuine miss.

    The chain must stay alive so CSS/regex get their turn.
    """
    html = (
        "<html><body>"
        '<script type="application/json">{"availability": "inStock"}</script>'
        "</body></html>"
    )

    class _P:
        price_json_path = "/amount"
        stock_json_path = "/availability"

    assert extract_embedded_json(html, profile=_P()) is None


def test_embedded_json_stays_silent_when_availability_is_unrecognized() -> None:
    """``UNKNOWN`` is not permission to invent an out-of-stock answer."""
    html = (
        "<html><body>"
        '<script type="application/json">{"availability": "who knows"}</script>'
        "</body></html>"
    )

    class _P:
        price_json_path = "/amount"
        stock_json_path = "/availability"

    assert extract_embedded_json(html, profile=_P()) is None


def test_embedded_json_stays_silent_when_no_stock_pointer_is_configured() -> None:
    html = '<html><body><script type="application/json">{"other": 1}</script></body></html>'

    class _P:
        price_json_path = "/amount"

    assert extract_embedded_json(html, profile=_P()) is None


def test_embedded_json_prefers_a_real_price_over_the_stock_only_fallback() -> None:
    """A later document carrying an actual price always wins."""
    html = (
        "<html><body>"
        '<script type="application/json">{"availability": "outOfStock"}</script>'
        '<script type="application/json">{"amount": "12.50", "availability": "inStock"}</script>'
        "</body></html>"
    )

    class _P:
        price_json_path = "/amount"
        stock_json_path = "/availability"

    candidate = extract_embedded_json(html, profile=_P())

    assert candidate is not None
    assert candidate.raw_price_text == "12.50"
    assert candidate.stock == StockStatus.IN_STOCK


def test_stock_only_candidate_survives_validation_as_a_classified_out_of_stock() -> None:
    """The end-to-end claim, proven rather than asserted in prose.

    ``validate_candidate`` rejects the sentinel price
    (``INVALID_PRICE_FORMAT``), but the spider's ``Rejected`` branch still
    threads ``candidate_extras=candidate``, so ``result_builder`` persists
    ``stock_status=OUT_OF_STOCK`` and ``pipelines`` upserts the badge.
    This is byte-for-byte the path noon's JSON-LD ``"price": 0`` already
    takes today.
    """
    html = _read_fixture("embedded_json_next_data_no_price.html")
    candidate = extract_embedded_json(html, profile=_NoPriceProfile())
    assert candidate is not None

    outcome = validate_candidate(candidate, None)

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.INVALID_PRICE_FORMAT
    # The classification the row is persisted with is still intact.
    assert candidate.stock == StockStatus.OUT_OF_STOCK


def test_pipeline_price_less_out_of_stock_page_is_classified_not_price_not_found() -> None:
    html = _read_fixture("embedded_json_next_data_no_price.html")

    candidate = extract(html, _NoPriceProfile())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.EMBEDDED_JSON
    assert candidate.stock == StockStatus.OUT_OF_STOCK


def test_pipeline_in_stock_page_with_a_missing_pointer_still_reaches_css() -> None:
    """Chain semantics are preserved: a miss must not swallow the chain."""
    html = (
        "<html><body>"
        '<p class="price">SAR 4.00</p>'
        '<script type="application/json">{"availability": "inStock"}</script>'
        "</body></html>"
    )

    class _P:
        jsonld_enabled = False
        price_json_path = "/amount"
        stock_json_path = "/availability"
        price_selector = "p.price"

    candidate = extract(html, _P())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.CSS


def test_embedded_json_stock_is_none_when_no_stock_pointer_is_configured() -> None:
    html = '<html><body><script type="application/json">{"p": "9.99"}</script></body></html>'

    class _P:
        price_json_path = "/p"

    candidate = extract_embedded_json(html, profile=_P())

    assert candidate is not None
    assert candidate.stock is None


# --- chain wiring (brief Step 2) ----------------------------------------------


def test_embedded_json_is_registered_in_the_method_to_strategy_map() -> None:
    assert _METHOD_TO_STRATEGY[ExtractionMethod.EMBEDDED_JSON] is extract_embedded_json


def test_pipeline_embedded_json_beats_lying_jsonld_when_price_json_path_is_set() -> None:
    """The whole point: JSON-LD says 0, ``__NEXT_DATA__`` says 249.5."""
    html = _read_fixture("embedded_json_next_data.html")

    lying = extract_jsonld(html)
    assert lying is not None
    assert lying.raw_price_text == "0"

    candidate = extract(html, _JsonPathNoJsonLdProfile())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.EMBEDDED_JSON
    assert candidate.raw_price_text == "249.5"


def test_pipeline_lying_jsonld_still_wins_unless_the_profile_disables_it() -> None:
    """A load-bearing seeding requirement, pinned so it cannot be forgotten.

    ``price_json_path`` on its own is not enough for a domain whose
    JSON-LD advertises a worthless price: JSON-LD is first in the chain
    and ``0`` is a hit. The profile must also set
    ``jsonld_enabled = false``.
    """
    html = _read_fixture("embedded_json_next_data.html")

    candidate = extract(html, _JsonPathProfile())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.JSON_LD
    assert candidate.raw_price_text == "0"


def test_pipeline_returns_jsonld_result_when_price_json_path_is_unset() -> None:
    """No behavior change for any profile that does not opt in."""
    html = _read_fixture("embedded_json_next_data.html")

    for profile in (None, _NoJsonPathProfile()):
        candidate = extract(html, profile)
        assert candidate is not None
        assert candidate.method == ExtractionMethod.JSON_LD
        assert candidate.raw_price_text == "0"


def test_pipeline_tries_embedded_json_before_css() -> None:
    """Chain position is JSON-LD -> EMBEDDED_JSON -> CSS -> regex."""
    html = (
        "<html><body>"
        '<p class="price">SAR 1.00</p>'
        '<script type="application/json">{"amount": "58.00"}</script>'
        "</body></html>"
    )

    class _Both:
        price_json_path = "/amount"
        price_selector = "p.price"

    candidate = extract(html, _Both())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.EMBEDDED_JSON
    assert candidate.raw_price_text == "58.00"


def test_pipeline_falls_through_to_css_when_the_json_pointer_misses() -> None:
    html = (
        "<html><body>"
        '<p class="price">SAR 1.00</p>'
        '<script type="application/json">{"other": "58.00"}</script>'
        "</body></html>"
    )

    class _Both:
        price_json_path = "/amount"
        price_selector = "p.price"

    candidate = extract(html, _Both())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.CSS


def test_pipeline_preferred_method_embedded_json_is_now_honoured() -> None:
    """``EMBEDDED_JSON`` was a forward-compat no-op in ``_ordered_strategies``."""
    html = _read_fixture("embedded_json_next_data.html")

    candidate = extract(
        html, _JsonPathProfile(), preferred_method=ExtractionMethod.EMBEDDED_JSON
    )

    assert candidate is not None
    assert candidate.method == ExtractionMethod.EMBEDDED_JSON


def test_pipeline_unavailable_item_is_classified_out_of_stock_from_json(
) -> None:
    """Brief Step 4: the availability field, not an Arabic string sniff."""
    html = _read_fixture("embedded_json_next_data_oos.html")

    candidate = extract(html, _JsonPathNoJsonLdProfile())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.EMBEDDED_JSON
    assert candidate.stock == StockStatus.OUT_OF_STOCK


# --- noon reality (real captures, 2026-08-16) ---------------------------------


def test_real_noon_page_ships_a_tanstack_js_payload_not_json() -> None:
    """Positive evidence for the shape noon actually serves.

    The absence of ``__NEXT_DATA__`` from this fixture proves little on
    its own — the file is a hand-trimmed slice and its own header says
    markup was dropped. What *is* load-bearing is the TanStack Router
    payload that is present, and the three JS-not-JSON constructs inside
    it that make ``json.loads`` structurally unable to read it.
    """
    html = _read_fixture("noon_product_real.html")

    assert 'id="$tsr-stream-barrier"' in html  # TanStack Router SSR payload
    assert "$R[1837]=" in html  # reference-capture assignment expression
    assert "is_bestseller:!1" in html  # JS boolean literal, unquoted key
    assert "sale_price:129" in html  # the true price, unquoted key


def test_real_noon_page_carries_no_json_script_body_so_embedded_json_misses() -> None:
    """Pins the documented limitation against the captured payload.

    Scoped honestly: this asserts the adapter misses on *this* TanStack
    payload, which is the mechanism that matters. It is not by itself
    proof about every noon page — a proxied re-capture is runbook Step 0.
    If this ever starts returning a candidate, noon has moved to a JSON
    payload and the profile should gain a ``price_json_path``.
    """
    html = _read_fixture("noon_product_real.html")

    class _AnyPointer:
        price_json_path = "/props/pageProps/product/price"

    assert extract_embedded_json(html, profile=_AnyPointer()) is None


def test_real_noon_available_page_true_price_is_reachable_by_regex_today() -> None:
    """``sale_price:129`` lives in the TanStack blob; REGEX can already read it."""
    html = _read_fixture("noon_product_real.html")

    class _NoonRegexProfile:
        price_regex = r"sale_price:([0-9]+(?:\.[0-9]+)?)"
        stock_regex = r"is_buyable:(!0|!1)"

    candidate = extract_regex(html, profile=_NoonRegexProfile())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.REGEX
    assert candidate.raw_price_text == "129"


def test_real_noon_unavailable_page_jsonld_reports_zero_and_out_of_stock() -> None:
    """The captured shape behind the 269 noon failures.

    JSON-LD is not lying — it reports ``price: 0`` **and**
    ``availability: OutOfStock``, and the embedded payload's ``offers``
    array is genuinely empty. There is no hidden true price to recover.
    """
    html = _read_fixture("noon_unavailable_real.html")

    candidate = extract_jsonld(html)

    assert candidate is not None
    assert candidate.raw_price_text == "0"
    assert candidate.stock == StockStatus.OUT_OF_STOCK
    assert "offers:$R[1594]=[]" in html
