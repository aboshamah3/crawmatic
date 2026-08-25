"""Arabic-locale currency normalization (EPA B4, folded-in B5 finding).

Task B5 found (2026-08-25) that ``amazon.sa``'s default, non-``/-/en/``
locale renders the currency node as the Arabic word ``ريال`` rather than
the ISO code ``SAR``, and that **no currency normalization existed
anywhere in the codebase** (checked ``app_shared.money``,
``scrape_core.money_text``). The price itself extracted correctly in both
locales; only the currency did not.

That is not cosmetic. ``app_shared.alerts.engine.filter_comparable``
compares the stored ``currency`` string to the client's currency with
``!=``, so every Arabic-locale observation was flagged
``CURRENCY_MISMATCH``, flipped ``comparable=false``, and **excluded from
every price comparison** — a silent, total loss of competitor coverage on
the Arabic locale.

Scope discipline (the reason for the ``UNKNOWN`` tests below): the bare
word ``ريال`` is written identically for the Saudi, Qatari, Omani,
Yemeni and Iranian riyal. This module maps the exact Saudi-locale forms
observed in the B5 fixtures (plus the forms that *name* Saudi Arabia
explicitly) and deliberately refuses to map any wording that names a
different country, or a symbol shared across countries — those pass
through unchanged so the existing ``CURRENCY_MISMATCH`` warning still
fires. A wrong currency is worse than a flagged one.
"""

from __future__ import annotations

import pytest

from app_shared.enums import ExtractionMethod, ScrapeErrorCode
from scrape_core.extraction.result import ExtractionCandidate
from scrape_core.money_text import normalize_currency
from scrape_core.validation import Accepted, validate_candidate

# The exact byte sequence captured from amazon.sa's `.a-price-symbol`
# node in tests/fixtures/amazon_labeled/_locale_diagnostic/*.
AMAZON_SA_ARABIC_CURRENCY = "ريال"


@pytest.mark.parametrize(
    "raw",
    [
        AMAZON_SA_ARABIC_CURRENCY,
        "  ريال  ",
        "‏ريال‎",  # wrapped in RTL/LTR marks, as the DOM node yields it
        "ر.س",
        "ر.س.",
        "ريال سعودي",
        "ريال سعودى",
        "SAR",
        "sar",
        "⃀",  # U+20C0 SAUDI RIYAL SIGN (Saudi-specific by definition)
    ],
)
def test_saudi_riyal_forms_normalize_to_sar(raw: str) -> None:
    assert normalize_currency(raw) == "SAR"


@pytest.mark.parametrize(
    "raw",
    [
        "ريال قطري",
        "ر.ق",
        "ريال عماني",
        "ر.ع",
        "ريال يمني",
        "﷼",  # U+FDFC RIAL SIGN — shared by SA/QA/OM/YE/IR, never Saudi-specific
    ],
)
def test_other_or_shared_riyal_forms_are_never_mapped_to_sar(raw: str) -> None:
    """Documented refusal. These wordings either name a different country
    or are a symbol several countries share; silently reading them as
    ``SAR`` would fabricate a currency. They pass through unchanged so
    the existing ``CURRENCY_MISMATCH`` warning still fires and a human
    (or a later, market-aware rule) decides."""
    assert normalize_currency(raw) != "SAR"


def test_iso_codes_pass_through_uppercased() -> None:
    assert normalize_currency("usd") == "USD"
    assert normalize_currency("EUR") == "EUR"


def test_unknown_text_is_returned_stripped_never_invented() -> None:
    assert normalize_currency("  حاجة تانية  ") == "حاجة تانية"
    assert normalize_currency(None) is None
    assert normalize_currency("") is None
    assert normalize_currency(123) is None  # never coerced from a non-str


def test_extraction_candidate_normalizes_its_currency_at_construction() -> None:
    """The single normalization point: every extractor (css/jsonld/regex/
    embedded_json) and every adapter builds an ``ExtractionCandidate``, so
    normalizing here is what makes the *persisted* currency ISO — which is
    what ``filter_comparable`` actually reads."""
    candidate = ExtractionCandidate(
        raw_price_text="7990.00",
        currency=AMAZON_SA_ARABIC_CURRENCY,
        method=ExtractionMethod.CSS,
        confidence=0.9,
    )
    assert candidate.currency == "SAR"


def test_arabic_locale_price_is_comparable_against_sar() -> None:
    """The end-to-end regression: before this fix the same candidate came
    back ``comparable=False`` with a ``CURRENCY_MISMATCH`` warning and was
    dropped from every comparison."""
    candidate = ExtractionCandidate(
        raw_price_text="7,990.00 ريال",
        currency=AMAZON_SA_ARABIC_CURRENCY,
        method=ExtractionMethod.CSS,
        confidence=0.9,
    )
    outcome = validate_candidate(candidate, {"required_currency": "SAR"}, None)
    assert isinstance(outcome, Accepted)
    assert outcome.comparable is True
    assert outcome.warning_code is None


def test_a_genuinely_different_currency_still_mismatches() -> None:
    candidate = ExtractionCandidate(
        raw_price_text="129.99",
        currency="USD",
        method=ExtractionMethod.CSS,
        confidence=0.9,
    )
    outcome = validate_candidate(candidate, {"required_currency": "SAR"}, None)
    assert isinstance(outcome, Accepted)
    assert outcome.comparable is False
    assert outcome.warning_code is ScrapeErrorCode.CURRENCY_MISMATCH


def test_a_qatari_riyal_wording_still_mismatches_against_sar() -> None:
    candidate = ExtractionCandidate(
        raw_price_text="129.99",
        currency="ريال قطري",
        method=ExtractionMethod.CSS,
        confidence=0.9,
    )
    outcome = validate_candidate(candidate, {"required_currency": "SAR"}, None)
    assert isinstance(outcome, Accepted)
    assert outcome.comparable is False
    assert outcome.warning_code is ScrapeErrorCode.CURRENCY_MISMATCH
