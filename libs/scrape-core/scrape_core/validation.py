"""Pure candidate-price validation + confidence gate (contracts/price-validation.md).

``validate_candidate(candidate, validation_rules, confidence_cfg)``:
a wrong price is worse than a missing one, so every check rejects
rather than "fixes" a bad value (never rounds, never guesses).

This US1 slice implements the **core** path — the money boundary, the
positivity guard, and the confidence gate (contracts/price-validation.md
checks 1, 2, 6). US3 (T035) extends this same function with the full
rule set: currency match/mismatch (check 3), min/max bounds (check 4),
and ``reject_if_text_contains`` (check 5) — the ``# US3:`` comments
below mark exactly where each slots into the existing check order so
the order-of-checks contract is preserved when they land.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from app_shared.enums import ScrapeErrorCode
from app_shared.money import parse_money
from app_shared.profiles.confidence import resolve_confidence_rules

from scrape_core.extraction.result import ExtractionCandidate

__all__ = ["Accepted", "Rejected", "ValidationOutcome", "validate_candidate"]


@dataclass(frozen=True)
class Accepted:
    """The candidate passed every check — persist as ``success=true``."""

    price: Decimal
    comparable: bool = True


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
    try:
        price = parse_money(candidate.raw_price_text)
    except (TypeError, ValueError) as exc:
        return Rejected(ScrapeErrorCode.INVALID_PRICE_FORMAT, str(exc))

    # --- 2. Positivity guard. ---
    if price <= 0:
        return Rejected(
            ScrapeErrorCode.INVALID_PRICE_FORMAT,
            f"price must be greater than 0, got {price}",
        )

    comparable = True
    # US3 (T035) slots in here, in this order, before the confidence gate:
    #   3. Currency: validation_rules["required_currency"] vs candidate.currency
    #      mismatch -> comparable = False + record CURRENCY_MISMATCH (still
    #      accepted, not rejected — no FX, FR-011).
    #   4. Bounds: validation_rules["min_price"] / ["max_price"] (via
    #      parse_money) -> out of range rejects with INVALID_PRICE_FORMAT... a
    #      dedicated bounds code per contracts/price-validation.md.
    #   5. Text rejects: validation_rules["reject_if_text_contains"] matched
    #      against candidate.matched_text -> reject.
    _ = validation_rules  # accepted now for the future rule set; unused in US1.

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

    return Accepted(price=price, comparable=comparable)
