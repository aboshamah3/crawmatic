"""Price validation + confidence gate tests (SPEC-07 US3 T037, contracts/price-validation.md).

``validate_candidate`` is pure — every case here constructs an
``ExtractionCandidate`` directly (no HTML/parsel involved) except the
one combined "fixture -> extraction -> validation" scenario at the end,
which exercises ``discount_save_x.html`` end-to-end through
``scrape_core.extraction.regex`` to prove the reject-term wiring holds
for a realistic candidate, not just a hand-built one.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app_shared.enums import ExtractionMethod, ScrapeErrorCode

from scrape_core.extraction.regex import extract_regex
from scrape_core.extraction.result import ExtractionCandidate
from scrape_core.validation import Accepted, Rejected, validate_candidate

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "html"


def _read_fixture(name: str) -> str:
    return (_FIXTURES_DIR / name).read_text(encoding="utf-8")


def _candidate(**overrides: object) -> ExtractionCandidate:
    defaults: dict[str, object] = dict(
        raw_price_text="99.99",
        currency="USD",
        method=ExtractionMethod.CSS,
        confidence=0.85,
        selector_used=None,
        raw_title=None,
        stock=None,
        matched_text=None,
    )
    defaults.update(overrides)
    return ExtractionCandidate(**defaults)  # type: ignore[arg-type]


# --- 1/2. Money boundary + positivity (Decimal exactness, never rounded) ------


def test_valid_decimal_price_is_accepted_exactly() -> None:
    outcome = validate_candidate(_candidate(raw_price_text="1234.56"), None)

    assert isinstance(outcome, Accepted)
    assert outcome.price == Decimal("1234.56")
    assert outcome.comparable is True
    assert outcome.warning_code is None


def test_float_raw_price_text_is_rejected_not_rounded() -> None:
    # ExtractionCandidate.raw_price_text is typed str, but dataclasses do
    # not enforce type hints at runtime — this directly proves parse_money's
    # "never accepts float" boundary is what validate_candidate relies on.
    outcome = validate_candidate(_candidate(raw_price_text=1234.5), None)

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.INVALID_PRICE_FORMAT


def test_nan_price_is_rejected() -> None:
    outcome = validate_candidate(_candidate(raw_price_text="NaN"), None)

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.INVALID_PRICE_FORMAT


def test_infinity_price_is_rejected() -> None:
    outcome = validate_candidate(_candidate(raw_price_text="Infinity"), None)

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.INVALID_PRICE_FORMAT


def test_over_scale_price_is_rejected_not_rounded() -> None:
    outcome = validate_candidate(_candidate(raw_price_text="19.12345"), None)

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.INVALID_PRICE_FORMAT


def test_zero_price_is_rejected() -> None:
    outcome = validate_candidate(_candidate(raw_price_text="0"), None)

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.INVALID_PRICE_FORMAT


def test_negative_price_is_rejected() -> None:
    outcome = validate_candidate(_candidate(raw_price_text="-5.00"), None)

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.INVALID_PRICE_FORMAT


# --- 3. Currency mismatch — saved, comparable=false, CURRENCY_MISMATCH warning -


def test_currency_mismatch_is_accepted_but_not_comparable() -> None:
    candidate = _candidate(currency="EUR", confidence=0.9)

    outcome = validate_candidate(candidate, {"required_currency": "USD"})

    assert isinstance(outcome, Accepted)
    assert outcome.comparable is False
    assert outcome.warning_code == ScrapeErrorCode.CURRENCY_MISMATCH
    assert outcome.price == Decimal("99.99")


def test_currency_match_is_comparable_with_no_warning() -> None:
    candidate = _candidate(currency="USD", confidence=0.9)

    outcome = validate_candidate(candidate, {"required_currency": "USD"})

    assert isinstance(outcome, Accepted)
    assert outcome.comparable is True
    assert outcome.warning_code is None


def test_currency_check_is_case_insensitive() -> None:
    candidate = _candidate(currency="usd", confidence=0.9)

    outcome = validate_candidate(candidate, {"required_currency": "USD"})

    assert isinstance(outcome, Accepted)
    assert outcome.comparable is True


def test_missing_candidate_currency_is_not_treated_as_a_mismatch() -> None:
    candidate = _candidate(currency=None, confidence=0.9)

    outcome = validate_candidate(candidate, {"required_currency": "USD"})

    assert isinstance(outcome, Accepted)
    assert outcome.comparable is True
    assert outcome.warning_code is None


# --- 4. Bounds ------------------------------------------------------------------


def test_price_below_min_price_is_rejected() -> None:
    candidate = _candidate(raw_price_text="5.00", confidence=0.9)

    outcome = validate_candidate(candidate, {"min_price": "10.00"})

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.PRICE_NOT_FOUND


def test_price_above_max_price_is_rejected() -> None:
    candidate = _candidate(raw_price_text="999.00", confidence=0.9)

    outcome = validate_candidate(candidate, {"max_price": "500.00"})

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.PRICE_NOT_FOUND


def test_price_within_bounds_is_accepted() -> None:
    candidate = _candidate(raw_price_text="50.00", confidence=0.9)

    outcome = validate_candidate(candidate, {"min_price": "10.00", "max_price": "500.00"})

    assert isinstance(outcome, Accepted)
    assert outcome.price == Decimal("50.00")


# --- 5. Text rejects (old/installment/discount/"save X"/shipping) -------------


def test_reject_if_text_contains_matches_old_price_term() -> None:
    candidate = _candidate(confidence=0.9, matched_text="Old price: $79.99, now discounted!")

    outcome = validate_candidate(
        candidate, {"reject_if_text_contains": ["old", "installment", "save", "shipping"]}
    )

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.PRICE_NOT_FOUND


def test_reject_if_text_contains_matches_save_x_term() -> None:
    candidate = _candidate(confidence=0.9, matched_text="Save $10 today on this item!")

    outcome = validate_candidate(candidate, {"reject_if_text_contains": ["save"]})

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.PRICE_NOT_FOUND


def test_reject_if_text_contains_matches_installment_term() -> None:
    candidate = _candidate(
        confidence=0.9, matched_text="Or 4 interest-free installments of $12.50"
    )

    outcome = validate_candidate(candidate, {"reject_if_text_contains": ["installment"]})

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.PRICE_NOT_FOUND


def test_reject_if_text_contains_matches_shipping_term() -> None:
    candidate = _candidate(confidence=0.9, matched_text="Shipping: $6.99")

    outcome = validate_candidate(candidate, {"reject_if_text_contains": ["shipping"]})

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.PRICE_NOT_FOUND


def test_reject_if_text_contains_matches_discount_term() -> None:
    candidate = _candidate(confidence=0.9, matched_text="Discounted clearance price")

    outcome = validate_candidate(candidate, {"reject_if_text_contains": ["discount"]})

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.PRICE_NOT_FOUND


def test_reject_if_text_contains_is_case_insensitive() -> None:
    candidate = _candidate(confidence=0.9, matched_text="SAVE big today")

    outcome = validate_candidate(candidate, {"reject_if_text_contains": ["save"]})

    assert isinstance(outcome, Rejected)


def test_reject_if_text_contains_matches_a_non_latin_configured_term() -> None:
    # reject_if_text_contains terms are DB-configured strings — the match is
    # a plain case-folded substring check, so a non-Latin-script term (e.g.
    # an Arabic word for "installment") works identically to an English one.
    candidate = _candidate(confidence=0.9, matched_text="أو 4 أقساط بقيمة 12.50")

    outcome = validate_candidate(candidate, {"reject_if_text_contains": ["أقساط"]})

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.PRICE_NOT_FOUND


def test_reject_if_text_contains_no_match_is_accepted() -> None:
    candidate = _candidate(confidence=0.9, matched_text="Current price, in stock now")

    outcome = validate_candidate(candidate, {"reject_if_text_contains": ["old", "save"]})

    assert isinstance(outcome, Accepted)


def test_discount_save_x_fixture_regex_candidate_is_rejected_end_to_end() -> None:
    """A regex-extracted candidate from the discount fixture is rejected via
    reject_if_text_contains — proving the extraction -> validation wiring,
    not just a hand-built candidate."""
    html = _read_fixture("discount_save_x.html")

    class _Profile:
        price_regex = r"\$([0-9]+(?:\.[0-9]{2})?)"

    candidate = extract_regex(html, profile=_Profile())
    assert candidate is not None  # the promo div's "$10" is the first text-node hit

    outcome = validate_candidate(
        candidate,
        {"reject_if_text_contains": ["old", "installment", "discount", "save", "shipping"]},
    )

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.PRICE_NOT_FOUND


# --- 6. Confidence gate ----------------------------------------------------------


def test_confidence_below_default_threshold_is_rejected() -> None:
    candidate = _candidate(confidence=0.74)

    outcome = validate_candidate(candidate, None)

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.LOW_CONFIDENCE_PRICE


def test_confidence_at_default_threshold_is_accepted() -> None:
    candidate = _candidate(confidence=0.75)

    outcome = validate_candidate(candidate, None)

    assert isinstance(outcome, Accepted)


def test_single_number_confidence_0_40_is_rejected_by_default() -> None:
    candidate = _candidate(
        raw_price_text="4521",
        currency=None,
        method=ExtractionMethod.SINGLE_NUMBER,
        confidence=0.40,
        matched_text="Model number: 4521.",
    )

    outcome = validate_candidate(candidate, None)

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.LOW_CONFIDENCE_PRICE


def test_confidence_gate_uses_provided_confidence_cfg_override() -> None:
    candidate = _candidate(confidence=0.5)

    outcome = validate_candidate(candidate, None, {"min_accepted_confidence": 0.3})

    assert isinstance(outcome, Accepted)


# --- Price-text normalization (2026-07-12 amazon.sa regression) ---------------
# CSS/regex price nodes carry currency symbols + thousands separators;
# validate_candidate normalizes before the strict §19 money boundary.


def test_currency_prefixed_thousands_separated_price_is_accepted() -> None:
    candidate = _candidate(raw_price_text="SAR11,729.00", currency="SAR")

    outcome = validate_candidate(candidate, {"required_currency": "SAR"})

    assert isinstance(outcome, Accepted)
    assert outcome.price == Decimal("11729.00")
    assert outcome.comparable is True


def test_european_grouped_price_is_accepted() -> None:
    candidate = _candidate(raw_price_text="1.234,56", currency="EUR")

    outcome = validate_candidate(candidate, None)

    assert isinstance(outcome, Accepted)
    assert outcome.price == Decimal("1234.56")


def test_unparseable_price_text_is_rejected_invalid_format() -> None:
    candidate = _candidate(raw_price_text="Price on request")

    outcome = validate_candidate(candidate, None)

    assert isinstance(outcome, Rejected)
    assert outcome.error_code == ScrapeErrorCode.INVALID_PRICE_FORMAT
