"""Normalize a human-facing price string into an exact-decimal string.

CSS/regex extraction returns whatever text a price node holds — e.g.
``"SAR11,729.00"``, ``"1.234,56 €"``, ``"749"`` — but the §19 money
boundary (:func:`app_shared.money.parse_money`) only accepts a clean
decimal string. JSON-LD extraction happens to yield clean numbers (it
reads a JSON number field), which is the only reason it worked before
this helper existed; CSS/regex prices were rejected ``INVALID_PRICE_
FORMAT`` on every site whose price node carried a currency symbol or a
thousands separator (found live on amazon.sa, 2026-07-12).

Pure/stdlib. Deliberately conservative: strips currency symbols/letters
and resolves the thousands-vs-decimal separator, but never rounds and
never guesses a magnitude — an unparseable string returns ``None`` so the
caller rejects rather than inventing a price (a wrong price is worse than
a missing one, the validation module's governing rule).
"""

from __future__ import annotations

import re

__all__ = ["normalize_price_text"]

# Everything that is not a digit, separator, or sign becomes a space, so a
# currency prefix/suffix ("SAR", "$", "€", "ر.س") drops out and a
# multi-price blob splits into whitespace-separated numeric tokens.
_NON_NUMERIC = re.compile(r"[^\d.,-]+")


def normalize_price_text(text: object) -> str | None:
    """Return an exact-decimal string for ``text``, or ``None`` if none is found.

    - ``"SAR11,729.00"`` -> ``"11729.00"`` (US grouping: comma thousands, dot decimal)
    - ``"1.234,56"``     -> ``"1234.56"`` (EU grouping: dot thousands, comma decimal)
    - ``"1,50"``         -> ``"1.50"``   (lone comma with <=2 trailing digits = decimal)
    - ``"1,234"``        -> ``"1234"``   (lone comma with 3 trailing digits = thousands)
    - ``"129.99"``       -> ``"129.99"`` (already clean; JSON-LD path unchanged)
    """
    # Only ever normalize genuine extracted text. A non-str (e.g. a float
    # that slipped past the str type hint) returns None so the caller's
    # strict §19 boundary still rejects it -- never coerced to a string
    # and silently accepted (a float can't represent money exactly).
    if not isinstance(text, str):
        return None

    cleaned = _NON_NUMERIC.sub(" ", text).strip()
    if not cleaned:
        return None

    # A price node may concatenate several renderings ("SAR11,729.00
    # SAR11,729 00"); take the first numeric token, never splice them.
    token = cleaned.split()[0]

    negative = token.startswith("-")
    token = token.replace("-", "")

    has_comma = "," in token
    has_dot = "." in token

    if has_comma and has_dot:
        # The rightmost of the two is the decimal separator; the other groups.
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif has_comma:
        head, _, tail = token.rpartition(",")
        # A lone comma with 1-2 trailing digits reads as a decimal comma;
        # otherwise (3-digit group, or several commas) it is grouping.
        if head and 1 <= len(tail) <= 2 and "," not in head:
            token = f"{head}.{tail}"
        else:
            token = token.replace(",", "")
    # dot-only or bare integer: dot is already the decimal separator.

    token = token.strip(".")
    if not token or not any(ch.isdigit() for ch in token):
        return None
    # A stray second dot (e.g. malformed input) makes this un-parseable —
    # reject rather than silently reinterpret.
    if token.count(".") > 1:
        return None

    return f"-{token}" if negative else token
