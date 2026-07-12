"""Pure candidate-price validation + confidence gate (contracts/price-validation.md).

``validate_candidate(candidate, validation_rules, confidence_cfg)``:
a wrong price is worse than a missing one, so every check rejects
rather than "fixes" a bad value (never rounds, never guesses).

Full six-check order (contracts/price-validation.md):

1. Money boundary (exact ``Decimal``; float/NaN/Infinity/over-scale ->
   ``INVALID_PRICE_FORMAT``, never rounded).
2. Positivity (``price > 0``, else ``INVALID_PRICE_FORMAT``).
3. Currency (``validation_rules["required_currency"]`` vs
   ``candidate.currency``) — a mismatch is **not** a rejection: marks
   ``comparable=False`` and the caller records a ``CURRENCY_MISMATCH``
   warning; the price is still saved (no FX, FR-011).
4. Bounds (``validation_rules["min_price"]``/``["max_price"]``, via the
   same money boundary) — out of range rejects (``PRICE_NOT_FOUND``: a
   price outside plausible bounds is treated the same as "no genuine
   price found", per spec Acceptance Scenario 4).
5. Text rejects (``validation_rules["reject_if_text_contains"]`` matched
   case-insensitively against ``candidate.matched_text`` — old /
   installment / discount / "save X" / shipping terms, or any
   DB-configured term including non-Latin scripts) -> ``PRICE_NOT_FOUND``.
6. Confidence gate (``candidate.confidence >= min_accepted_confidence``,
   default 0.75 via ``resolve_confidence_rules``) -> ``LOW_CONFIDENCE_PRICE``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from app_shared.enums import ScrapeErrorCode
from app_shared.money import parse_money
from app_shared.profiles.confidence import resolve_confidence_rules

from scrape_core.extraction.result import ExtractionCandidate
from scrape_core.money_text import normalize_price_text

__all__ = ["Accepted", "Rejected", "ValidationOutcome", "validate_candidate"]


@dataclass(frozen=True)
class Accepted:
    """The candidate passed every check — persist as ``success=true``.

    ``warning_code`` is set to ``CURRENCY_MISMATCH`` exactly when
    ``comparable`` is ``False`` (contracts/errors.md: "CURRENCY_MISMATCH
    is a warning on an otherwise successful (comparable=false)
    observation") — this is the single source of truth for that
    decision, so the persistence layer never re-derives it by comparing
    currencies again.
    """

    price: Decimal
    comparable: bool = True
    warning_code: ScrapeErrorCode | None = None


@dataclass(frozen=True)
class Rejected:
    """The candidate failed a check — persist as ``success=false``.

    ``error_code`` is the §34 code the observation/attempt rows record;
    ``message`` is a human-readable detail (``error_message``), never
    parsed by any caller.
    """

    error_code: ScrapeErrorCode
    message: str


ValidationOutcome = Accepted | Rejected


def validate_candidate(
    candidate: ExtractionCandidate,
    validation_rules: Mapping[str, Any] | None,
    confidence_cfg: Mapping[str, Any] | None = None,
) -> ValidationOutcome:
    """Validate ``candidate`` against ``validation_rules`` + the confidence gate.

    ``confidence_cfg`` is the already-resolved rules mapping (i.e. the
    caller's ``resolve_confidence_rules(profile.confidence_rules)``
    result) carrying ``min_accepted_confidence``; ``None`` resolves the
    §17 defaults on the caller's behalf so this function is also usable
    standalone (e.g. in tests) without every call site re-deriving it.
    """
    # --- 1. Money boundary (§19) — exact Decimal, never a float/rounded value. ---
    # Normalize the extracted price text first (strip currency symbols /
    # thousands separators): CSS/regex return raw DOM text like
    # "SAR11,729.00", which the strict §19 boundary would reject outright.
    # JSON-LD text is already clean, so it passes through unchanged.
    normalized_price_text = normalize_price_text(candidate.raw_price_text)
    try:
        price = parse_money(normalized_price_text)
    except (TypeError, ValueError) as exc:
        return Rejected(ScrapeErrorCode.INVALID_PRICE_FORMAT, str(exc))

    # --- 2. Positivity guard. ---
    if price <= 0:
        return Rejected(
            ScrapeErrorCode.INVALID_PRICE_FORMAT,
            f"price must be greater than 0, got {price}",
        )

    comparable = True
    warning_code: ScrapeErrorCode | None = None
    rules: Mapping[str, Any] = validation_rules or {}

    # --- 3. Currency (FR-011) — mismatch is a warning, not a rejection. ---
    required_currency = rules.get("required_currency")
    if required_currency and candidate.currency:
        if str(candidate.currency).strip().upper() != str(required_currency).strip().upper():
            comparable = False
            warning_code = ScrapeErrorCode.CURRENCY_MISMATCH

    # --- 4. Bounds. ---
    min_price = rules.get("min_price")
    if min_price is not None:
        try:
            min_decimal = parse_money(min_price, non_negative=True)
        except (TypeError, ValueError) as exc:
            return Rejected(
                ScrapeErrorCode.INVALID_PRICE_FORMAT,
                f"validation_rules.min_price is not a valid money value: {exc}",
            )
        if price < min_decimal:
            return Rejected(
                ScrapeErrorCode.PRICE_NOT_FOUND,
                f"price {price} is below the configured min_price {min_decimal}",
            )

    max_price = rules.get("max_price")
    if max_price is not None:
        try:
            max_decimal = parse_money(max_price, non_negative=True)
        except (TypeError, ValueError) as exc:
            return Rejected(
                ScrapeErrorCode.INVALID_PRICE_FORMAT,
                f"validation_rules.max_price is not a valid money value: {exc}",
            )
        if price > max_decimal:
            return Rejected(
                ScrapeErrorCode.PRICE_NOT_FOUND,
                f"price {price} is above the configured max_price {max_decimal}",
            )

    # --- 5. Text rejects (old/installment/discount/"save X"/shipping, or
    # any DB-configured term — matched case-insensitively; substring match
    # so this works unchanged for non-Latin-script terms too). ---
    reject_terms = rules.get("reject_if_text_contains")
    if reject_terms and candidate.matched_text:
        haystack = candidate.matched_text.casefold()
        for term in reject_terms:
            if isinstance(term, str) and term and term.casefold() in haystack:
                return Rejected(
                    ScrapeErrorCode.PRICE_NOT_FOUND,
                    f"matched_text contains the reject_if_text_contains term {term!r}",
                )

    # --- 6. Confidence gate (§17). ---
    resolved_confidence_cfg = (
        confidence_cfg if confidence_cfg is not None else resolve_confidence_rules(None)
    )
    min_accepted = resolved_confidence_cfg["min_accepted_confidence"]
    if candidate.confidence < min_accepted:
        return Rejected(
            ScrapeErrorCode.LOW_CONFIDENCE_PRICE,
            f"confidence {candidate.confidence} below min_accepted_confidence {min_accepted}",
        )

    return Accepted(price=price, comparable=comparable, warning_code=warning_code)
