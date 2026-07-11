"""Extraction strategy tests (SPEC-07 US1 T024 / US3 T036, contracts/extraction.md).

US1 (T024) covers the JSON-LD strategy and the orchestrator's
first-hit-wins / ``PRICE_NOT_FOUND`` behavior. US3 (T036) adds CSS-only
(0.85) and regex-only (0.75) fixture extraction, the full JSON-LD -> CSS
-> regex fallback order, and the single unlabeled-number heuristic
(0.40).
"""

from __future__ import annotations

from pathlib import Path

from app_shared.enums import ExtractionMethod, StockStatus

from scrape_core.extraction.css import extract_css
from scrape_core.extraction.jsonld import extract_jsonld
from scrape_core.extraction.pipeline import extract
from scrape_core.extraction.regex import extract_regex

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "html"


def _read_fixture(name: str) -> str:
    return (_FIXTURES_DIR / name).read_text(encoding="utf-8")


# --- extract_jsonld -----------------------------------------------------------


def test_jsonld_extracts_price_currency_and_confidence_0_95() -> None:
    html = _read_fixture("jsonld_product.html")

    candidate = extract_jsonld(html)

    assert candidate is not None
    assert candidate.method == ExtractionMethod.JSON_LD
    assert candidate.raw_price_text == "129.99"
    assert candidate.currency == "USD"
    assert candidate.confidence == 0.95


def test_jsonld_extracts_stock_status_and_title() -> None:
    html = _read_fixture("jsonld_product.html")

    candidate = extract_jsonld(html)

    assert candidate is not None
    assert candidate.stock == StockStatus.IN_STOCK
    assert candidate.raw_title == "Generic Widget Pro"


def test_jsonld_returns_none_when_no_ld_json_block_present() -> None:
    html = "<html><body><p>No structured data here.</p></body></html>"

    assert extract_jsonld(html) is None


def test_jsonld_returns_none_for_malformed_json() -> None:
    html = (
        '<html><head><script type="application/ld+json">{not valid json'
        "</script></head><body></body></html>"
    )

    assert extract_jsonld(html) is None


def test_jsonld_skipped_when_profile_disables_it() -> None:
    html = _read_fixture("jsonld_product.html")

    class _Profile:
        jsonld_enabled = False

    assert extract_jsonld(html, profile=_Profile()) is None


def test_jsonld_confidence_overridden_by_profile_confidence_rules() -> None:
    html = _read_fixture("jsonld_product.html")

    class _Profile:
        jsonld_enabled = True
        confidence_rules = {"jsonld": 0.99}

    candidate = extract_jsonld(html, profile=_Profile())

    assert candidate is not None
    assert candidate.confidence == 0.99


# --- extract (orchestrator) ----------------------------------------------------


def test_pipeline_returns_jsonld_candidate_first() -> None:
    html = _read_fixture("jsonld_product.html")

    candidate = extract(html)

    assert candidate is not None
    assert candidate.method == ExtractionMethod.JSON_LD


def test_pipeline_returns_none_when_nothing_matches() -> None:
    html = "<html><body><p>Just some text, no price anywhere.</p></body></html>"

    assert extract(html) is None


# --- SPEC-07 US3 (T036): CSS / regex / single-number + full fallback order -----


class _CssProfile:
    """Selectors matching ``tests/fixtures/html/css_only.html``."""

    jsonld_enabled = True
    price_selector = "span.price"
    old_price_selector = "span.old-price"
    currency_selector = "span.currency"
    stock_selector = "span.stock"
    title_selector = "h1.product-title"
    confidence_rules: dict[str, float] | None = None


class _RegexProfile:
    """Regex rules matching ``tests/fixtures/html/regex_only.html``."""

    jsonld_enabled = True
    price_selector: str | None = None
    price_regex = r'"price"\s*:\s*"?([0-9.,]+)'
    currency_regex = r'"currency"\s*:\s*"([A-Z]{3})"'
    confidence_rules: dict[str, float] | None = None


def test_extract_css_finds_price_currency_stock_title_at_confidence_0_85() -> None:
    html = _read_fixture("css_only.html")

    candidate = extract_css(html, profile=_CssProfile())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.CSS
    assert candidate.raw_price_text == "74.50"
    assert candidate.currency == "USD"
    assert candidate.confidence == 0.85
    assert candidate.stock == StockStatus.IN_STOCK
    assert candidate.raw_title == "Sturdy Bracket Set"


def test_extract_css_returns_none_without_a_configured_price_selector() -> None:
    html = _read_fixture("css_only.html")

    class _NoSelectorProfile:
        price_selector = None

    assert extract_css(html, profile=_NoSelectorProfile()) is None


def test_extract_regex_finds_price_and_currency_at_confidence_0_75() -> None:
    html = _read_fixture("regex_only.html")

    candidate = extract_regex(html, profile=_RegexProfile())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.REGEX
    assert candidate.raw_price_text == "56.25"
    assert candidate.currency == "USD"
    assert candidate.confidence == 0.75


def test_extract_regex_single_unlabeled_number_scores_0_40() -> None:
    html = _read_fixture("single_number.html")

    candidate = extract_regex(html)

    assert candidate is not None
    assert candidate.method == ExtractionMethod.SINGLE_NUMBER
    assert candidate.raw_price_text == "4521"
    assert candidate.confidence == 0.40


def test_extract_regex_returns_none_when_multiple_bare_numbers_are_ambiguous() -> None:
    html = _read_fixture("discount_save_x.html")

    assert extract_regex(html) is None


def test_pipeline_falls_back_from_jsonld_to_css_when_no_ld_json_present() -> None:
    html = _read_fixture("css_only.html")

    candidate = extract(html, profile=_CssProfile())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.CSS
    assert candidate.confidence == 0.85


def test_pipeline_falls_back_from_jsonld_and_css_to_regex() -> None:
    html = _read_fixture("regex_only.html")

    candidate = extract(html, profile=_RegexProfile())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.REGEX
    assert candidate.confidence == 0.75


def test_pipeline_prefers_jsonld_over_css_and_regex_when_all_are_configured() -> None:
    html = _read_fixture("jsonld_product.html")

    class _AllStrategiesProfile:
        jsonld_enabled = True
        price_selector = "span.price"
        price_regex = r'"price"\s*:\s*"?([0-9.,]+)'
        confidence_rules: dict[str, float] | None = None

    candidate = extract(html, profile=_AllStrategiesProfile())

    assert candidate is not None
    assert candidate.method == ExtractionMethod.JSON_LD
    assert candidate.confidence == 0.95


# --- parsel type pinning (2026-07-11 live-amazon.sa regression) ---------------
# parsel's Selector auto-detection can misclassify a page whose leading
# bytes look JSON-ish as a 'json' Selector, on which .xpath()/.css()
# raise ValueError instead of scanning -- seen live on amazon.sa product
# HTML, where it crashed the whole extraction chain (and with it the
# domain's entire discovery run). Every strategy pins type="html" now;
# a JSON document must flow through as "no price found", never a crash.

_JSON_DOCUMENT = '{"product": {"name": "widget", "price": 129.99, "currency": "USD"}}'


def test_extract_regex_on_json_document_returns_without_crashing() -> None:
    candidate = extract_regex(_JSON_DOCUMENT)

    # The single-number heuristic may or may not match inside the JSON
    # text -- the contract under test is only "never raises".
    assert candidate is None or candidate.method in (
        ExtractionMethod.REGEX,
        ExtractionMethod.SINGLE_NUMBER,
    )


def test_extract_jsonld_on_json_document_returns_none_without_crashing() -> None:
    assert extract_jsonld(_JSON_DOCUMENT) is None


def test_extract_css_on_json_document_returns_none_without_crashing() -> None:
    class _CssProfile:
        price_selector = "span.price"
        confidence_rules: dict[str, float] | None = None

    assert extract_css(_JSON_DOCUMENT, profile=_CssProfile()) is None


def test_full_pipeline_on_json_document_never_crashes() -> None:
    class _AllStrategiesProfile:
        jsonld_enabled = True
        price_selector = "span.price"
        price_regex = r'"price"\s*:\s*"?([0-9.,]+)'
        confidence_rules: dict[str, float] | None = None

    # Must not raise; a JSON body either yields a regex candidate or None.
    extract(_JSON_DOCUMENT, profile=_AllStrategiesProfile())
