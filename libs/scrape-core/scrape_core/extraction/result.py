"""``ExtractionCandidate`` — the pure output of any extraction strategy.

Per ``contracts/extraction.md``: every strategy in ``scrape_core.extraction``
(``jsonld.py`` now, ``css.py``/``regex.py`` in US3) returns either an
``ExtractionCandidate`` or ``None`` (no hit, the pipeline falls through
to the next strategy). The candidate carries the **raw** price text —
it is not parsed to ``Decimal`` here; that is
``scrape_core.validation``'s job (the single §19 money boundary).
"""

from __future__ import annotations

from dataclasses import dataclass

from app_shared.enums import ExtractionMethod, StockStatus

__all__ = ["ExtractionCandidate"]


@dataclass(frozen=True)
class ExtractionCandidate:
    """One strategy's price find, before validation.

    ``raw_price_text`` is handed to ``app_shared.money.parse_money`` by
    ``scrape_core.validation`` — never parsed here. ``matched_text`` is
    the surrounding text a validator's ``reject_if_text_contains`` rule
    matches against (old/installment/discount/"save X"/shipping, US3).
    """

    raw_price_text: str
    currency: str | None
    method: ExtractionMethod
    confidence: float
    selector_used: str | None = None
    raw_title: str | None = None
    stock: StockStatus | None = None
    matched_text: str | None = None
