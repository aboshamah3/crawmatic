"""Money value type: an exact-decimal boundary over ``NUMERIC(18,4)``.

Per ``contracts/money.md`` / research.md D4 (§19): monetary values are
never floats. ``Money`` is a SQLAlchemy ``TypeDecorator`` over
``Numeric(precision=18, scale=4, asdecimal=True)`` that:

* passes ``None`` through untouched (nullable columns are allowed);
* **rejects** ``float`` outright — floats cannot represent money exactly;
* accepts ``Decimal``, ``int``, and exact numeric ``str`` input, coercing
  each to ``Decimal``;
* rejects non-finite values (``NaN``, ``Infinity``, ``-Infinity``);
* rejects over-scale values (more than 4 fractional digits) rather than
  silently rounding them;
* returns the stored value as a ``Decimal`` on read, so a round-tripped
  value is exact and never surfaced as a float (SC-004).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Numeric
from sqlalchemy.types import TypeDecorator

MONEY_PRECISION = 18
MONEY_SCALE = 4


class Money(TypeDecorator[Decimal]):
    """Exact-decimal money column over ``NUMERIC(18,4)`` (never float)."""

    impl = Numeric(precision=MONEY_PRECISION, scale=MONEY_SCALE, asdecimal=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None

        if isinstance(value, float):
            raise TypeError(
                "Money columns never accept float — pass a Decimal (or int) "
                f"instead of {value!r}"
            )

        if isinstance(value, bool):
            # bool is a subclass of int; treating it as money is almost
            # certainly a caller bug, so reject it explicitly rather than
            # silently storing 0/1.
            raise TypeError(f"Money columns do not accept bool: {value!r}")

        if isinstance(value, Decimal):
            decimal_value = value
        elif isinstance(value, int):
            decimal_value = Decimal(value)
        elif isinstance(value, str):
            try:
                decimal_value = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError(f"{value!r} is not a valid decimal string") from exc
        else:
            raise TypeError(
                f"Money columns accept Decimal, int, or exact numeric str, got {type(value)!r}"
            )

        if not decimal_value.is_finite():
            raise ValueError(
                f"Money columns reject non-finite values (NaN/Infinity): {decimal_value!r}"
            )

        exponent = decimal_value.as_tuple().exponent
        # exponent is an int for finite Decimals (is_finite() already
        # excluded the 'n'/'N'/'F' special-value exponents).
        if isinstance(exponent, int) and -exponent > MONEY_SCALE:
            raise ValueError(
                f"Money columns reject over-scale values (more than {MONEY_SCALE} "
                f"decimal places), not silently rounding: {decimal_value!r}"
            )

        return decimal_value

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        # Numeric(asdecimal=True) already yields a Decimal; return it
        # unchanged so round-trips are exact and never surfaced as float.
        return value
