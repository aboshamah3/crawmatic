"""``Money`` type-boundary tests (FR-005, SC-004).

Per ``contracts/money.md`` / research.md D4: ``Money.process_bind_param``
must reject ``float`` and non-finite/over-scale ``Decimal`` values (never
silently rounding), while accepting ``Decimal``/``int`` input and
round-tripping valid in-scale values as an exact ``Decimal`` — never a
float. Calls ``process_bind_param``/``process_result_value`` directly
with ``dialect=None`` (no DB required), matching the convention used by
``tests/unit/test_enums.py``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app_shared.money import Money, parse_money

_MONEY = Money()


def test_none_passes_through_bind() -> None:
    assert _MONEY.process_bind_param(None, dialect=None) is None


def test_none_passes_through_result() -> None:
    assert _MONEY.process_result_value(None, dialect=None) is None


def test_float_is_rejected() -> None:
    with pytest.raises((TypeError, ValueError)):
        _MONEY.process_bind_param(1.1, dialect=None)


@pytest.mark.parametrize(
    "value",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")],
    ids=["nan", "infinity", "neg-infinity"],
)
def test_non_finite_decimal_is_rejected(value: Decimal) -> None:
    with pytest.raises(ValueError):
        _MONEY.process_bind_param(value, dialect=None)


def test_over_scale_decimal_is_rejected_not_rounded() -> None:
    with pytest.raises(ValueError):
        _MONEY.process_bind_param(Decimal("1.23456"), dialect=None)


@pytest.mark.parametrize(
    "value",
    [Decimal("19.99"), Decimal("0.0001"), 42],
    ids=["decimal-19.99", "decimal-0.0001", "int-42"],
)
def test_valid_input_is_accepted_and_coerced_to_decimal(value: Decimal | int) -> None:
    bound = _MONEY.process_bind_param(value, dialect=None)
    assert isinstance(bound, Decimal)
    assert bound == Decimal(value)


def test_valid_in_scale_decimal_round_trips_exactly_as_decimal() -> None:
    original = Decimal("19.99")
    bound = _MONEY.process_bind_param(original, dialect=None)
    result = _MONEY.process_result_value(bound, dialect=None)

    assert result == original
    assert isinstance(result, Decimal)
    assert not isinstance(result, float)


def test_zero_scale_boundary_decimal_is_accepted() -> None:
    # Exactly 4 decimal places is the boundary, not over-scale.
    bound = _MONEY.process_bind_param(Decimal("0.0001"), dialect=None)
    assert bound == Decimal("0.0001")


# --- `parse_money` pure boundary (SPEC-06 US4 T044, FR-022, SC-006) -----------
#
# `parse_money` is the extracted pure §19 money boundary reused by both
# `Money.process_bind_param` (non_negative=False, unchanged historical
# behavior, exercised above via the `Money` wrapper) and
# `app_shared.profiles.validation` (non_negative=True). These tests drive
# `parse_money` directly, including its `non_negative` option.


@pytest.mark.parametrize(
    "value",
    [Decimal("19.99"), Decimal("0.0001"), Decimal("0"), Decimal("0.00"), 42, "19.99"],
    ids=["decimal-19.99", "decimal-0.0001", "decimal-0", "decimal-0.00", "int-42", "str-19.99"],
)
def test_parse_money_accepts_finite_in_scale_non_negative_decimal(value: Decimal | int | str) -> None:
    result = parse_money(value, non_negative=True)
    assert isinstance(result, Decimal)
    assert result == Decimal(value)


def test_parse_money_default_non_negative_is_false() -> None:
    # non_negative defaults to False, matching Money.process_bind_param's
    # historical (unchanged) behavior — a negative value is accepted.
    assert parse_money(Decimal("-19.99")) == Decimal("-19.99")


@pytest.mark.parametrize(
    "value",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")],
    ids=["nan", "infinity", "neg-infinity"],
)
def test_parse_money_rejects_non_finite(value: Decimal) -> None:
    with pytest.raises(ValueError):
        parse_money(value)


def test_parse_money_rejects_over_scale_not_rounded() -> None:
    with pytest.raises(ValueError):
        parse_money(Decimal("1.23456"))


def test_parse_money_accepts_exactly_scale_4_boundary() -> None:
    assert parse_money(Decimal("1.2345")) == Decimal("1.2345")


def test_parse_money_rejects_negative_when_non_negative_true() -> None:
    with pytest.raises(ValueError):
        parse_money(Decimal("-0.01"), non_negative=True)


def test_parse_money_accepts_negative_when_non_negative_false() -> None:
    assert parse_money(Decimal("-0.01"), non_negative=False) == Decimal("-0.01")


def test_parse_money_rejects_float_input() -> None:
    with pytest.raises(TypeError):
        parse_money(1.1)


def test_parse_money_rejects_bool_input() -> None:
    with pytest.raises(TypeError):
        parse_money(True)


def test_parse_money_rejects_non_numeric_type() -> None:
    with pytest.raises(TypeError):
        parse_money(object())


def test_parse_money_rejects_invalid_decimal_string() -> None:
    with pytest.raises(ValueError):
        parse_money("not-a-number")
