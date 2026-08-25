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

from scrape_core.money_text import normalize_currency

__all__ = ["ExtractionCandidate"]


@dataclass(frozen=True)
class ExtractionCandidate:
    """One strategy's price find, before validation.

    ``raw_price_text`` is handed to ``app_shared.money.parse_money`` by
    ``scrape_core.validation`` — never parsed here. ``matched_text`` is
    the surrounding text a validator's ``reject_if_text_contains`` rule
    matches against (old/installment/discount/"save X"/shipping, US3).

    ``currency`` is the ONE normalization point in the system (EPA B4,
    folded-in Task B5 finding): every extraction strategy and every
    adapter builds one of these, so normalizing here — rather than at
    each of the six-plus construction sites — is what makes the
    *persisted* ``price_observations.currency`` an ISO code. That matters
    because ``app_shared.alerts.engine.filter_comparable`` compares the
    stored string to the client currency with ``!=``: before this,
    amazon.sa's Arabic-locale ``"ريال"`` was silently dropped from every
    comparison. ``scrape_core.money_text.normalize_currency`` maps only
    the Saudi forms it can justify and returns anything else unchanged,
    so an unrecognized currency still raises ``CURRENCY_MISMATCH``
    instead of being invented.
    """

    raw_price_text: str
    currency: str | None
    method: ExtractionMethod
    confidence: float
    selector_used: str | None = None
    raw_title: str | None = None
    stock: StockStatus | None = None
    matched_text: str | None = None

    def __post_init__(self) -> None:
        normalized = normalize_currency(self.currency)
        if normalized != self.currency:
            # frozen dataclass: the canonical in-__post_init__ assignment.
            object.__setattr__(self, "currency", normalized)
