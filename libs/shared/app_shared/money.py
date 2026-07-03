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


def parse_money(value: Any, *, non_negative: bool = False) -> Decimal:
    """Pure §19 money boundary: coerce ``value`` to an exact, in-scale ``Decimal``.

    Shared by :meth:`Money.process_bind_param` (via ``non_negative=False``,
    its historical behavior, unchanged) and
    ``app_shared.profiles.validation`` (via ``non_negative=True`` for
    ``min_price``/``max_price``, SPEC-06 FR-008/FR-022), so §19 has exactly
    one implementation (Principle VII).

    * **rejects** ``float`` and ``bool`` outright — never an exact money
      representation;
    * accepts ``Decimal``, ``int``, and exact numeric ``str`` input, coercing
      each to ``Decimal``;
    * rejects non-finite values (``NaN``, ``Infinity``, ``-Infinity``);
    * rejects over-scale values (more than ``MONEY_SCALE`` fractional digits)
      rather than silently rounding them;
    * when ``non_negative`` is ``True``, also rejects negative values.
    """
    if isinstance(value, float):
        raise TypeError(
            "Money values never accept float — pass a Decimal (or int) "
            f"instead of {value!r}"
        )

    if isinstance(value, bool):
        # bool is a subclass of int; treating it as money is almost
        # certainly a caller bug, so reject it explicitly rather than
        # silently storing 0/1.
        raise TypeError(f"Money values do not accept bool: {value!r}")

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
            f"Money values accept Decimal, int, or exact numeric str, got {type(value)!r}"
        )

    if not decimal_value.is_finite():
        raise ValueError(
            f"Money values reject non-finite values (NaN/Infinity): {decimal_value!r}"
        )

    exponent = decimal_value.as_tuple().exponent
    # exponent is an int for finite Decimals (is_finite() already
    # excluded the 'n'/'N'/'F' special-value exponents).
    if isinstance(exponent, int) and -exponent > MONEY_SCALE:
        raise ValueError(
            f"Money values reject over-scale values (more than {MONEY_SCALE} "
            f"decimal places), not silently rounding: {decimal_value!r}"
        )

    if non_negative and decimal_value < 0:
        raise ValueError(f"Money values reject negative amounts: {decimal_value!r}")

    return decimal_value


class Money(TypeDecorator[Decimal]):
    """Exact-decimal money column over ``NUMERIC(18,4)`` (never float)."""

    impl = Numeric(precision=MONEY_PRECISION, scale=MONEY_SCALE, asdecimal=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        return parse_money(value)

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        # Numeric(asdecimal=True) already yields a Decimal; return it
        # unchanged so round-trips are exact and never surfaced as float.
        return value
