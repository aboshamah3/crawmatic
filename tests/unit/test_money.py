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

from app_shared.money import Money

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
