"""Extraction strategy tests (SPEC-07 US1 T024, contracts/extraction.md).

CSS-only / regex-only / single-number fixture cases are added in US3
(T036) once ``extraction/css.py`` + ``extraction/regex.py`` exist and
the pipeline's ordered chain grows past JSON-LD. This slice covers the
JSON-LD strategy itself and the orchestrator's first-hit-wins /
``PRICE_NOT_FOUND`` behavior.
"""

from __future__ import annotations

from pathlib import Path

from app_shared.enums import ExtractionMethod, StockStatus

from scrape_core.extraction.jsonld import extract_jsonld
from scrape_core.extraction.pipeline import extract

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
