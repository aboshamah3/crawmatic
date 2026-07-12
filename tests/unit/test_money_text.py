"""Unit tests for `scrape_core.money_text.normalize_price_text`
(2026-07-12 live-amazon.sa regression: CSS/regex price text carried
currency symbols + thousands separators the strict §19 money boundary
rejected outright).
"""

from __future__ import annotations

import pytest

from scrape_core.money_text import normalize_price_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # US grouping (comma thousands, dot decimal) with currency prefix.
        ("SAR11,729.00", "11729.00"),
        ("$1,234.56", "1234.56"),
        ("SAR960.14", "960.14"),
        # EU grouping (dot thousands, comma decimal).
        ("1.234,56", "1234.56"),
        ("1.234.567,89 €", "1234567.89"),
        # Lone comma: <=2 trailing digits reads as a decimal comma...
        ("1,50", "1.50"),
        # ...3 trailing digits reads as thousands grouping.
        ("1,234", "1234"),
        # Already-clean JSON-LD-style values pass through unchanged.
        ("129.99", "129.99"),
        ("11999.00", "11999.00"),
        ("749", "749"),
        # Currency suffix + whitespace.
        ("2,499.00 SAR", "2499.00"),
        # Negative sign preserved.
        ("-42.00", "-42.00"),
    ],
)
def test_normalize_known_price_strings(raw: str, expected: str) -> None:
    assert normalize_price_text(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "SAR", "N/A", "Price on request", None])
def test_normalize_returns_none_when_no_number(raw: object) -> None:
    assert normalize_price_text(raw) is None


def test_normalize_rejects_ambiguous_double_dot() -> None:
    # After grouping resolution a value that still has two dots is
    # malformed -- reject rather than silently reinterpret.
    assert normalize_price_text("12.34.56") is None


def test_normalize_takes_first_token_of_a_concatenated_blob() -> None:
    # A price node that concatenates several renderings must never splice
    # them into one giant number.
    assert normalize_price_text("SAR11,729.00 SAR11,729 00") == "11729.00"
