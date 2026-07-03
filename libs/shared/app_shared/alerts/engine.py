"""Pure alert engine (SPEC-09 §23) — deterministic variant price comparison.

Per ``contracts/alert-engine.md``: **DB/framework-free**. Imports stdlib
``decimal`` only, plus :mod:`app_shared.enums` for the alert vocabulary.
**MUST NOT** import sqlalchemy, celery, fastapi, scrapy, or redis
(``tests/unit/test_import_boundaries.py`` guards this) — the NaN/
Infinity/over-scale boundary rejection below duplicates
:func:`app_shared.money.parse_money`'s finite/over-scale *semantics*
(research D2) as pure stdlib code rather than importing that module,
since ``app_shared.money`` itself pulls in ``sqlalchemy`` (for its
``Money`` ``TypeDecorator``) and this engine must stay import-clean of
it even transitively.

Turns a variant's client price + its comparable competitor prices into a
deterministic :class:`AlertOutcome` via the ordered §23 decision tree
(:func:`decide`), the fixed severity map (:func:`severity_for`), the
currency filter (:func:`filter_comparable`), and the ordered
event-transition rule (:func:`transition`, D5). Every Decimal boundary
compare happens **after** quantizing ``discount_vs_average`` to 4dp with
``ROUND_HALF_UP`` (D2) — no float ever touches a compare (SC-001).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app_shared.enums import AlertEventType, AlertSeverity, AlertType

# Mirrors app_shared.money.MONEY_SCALE (NUMERIC(18,4)) without importing
# that module (see module docstring).
_MONEY_SCALE = 4

__all__ = [
    "QUANT",
    "SEVERITY_BY_TYPE",
    "CompetitorPrice",
    "ComparableSplit",
    "AlertOutcome",
    "filter_comparable",
    "discount_vs_average",
    "decide",
    "severity_for",
    "analyze",
    "transition",
]

# 4 decimal places — every boundary compare happens on a value already
# quantized to this scale (D2, FR-008).
QUANT = Decimal("0.0001")

# Severity is derived **solely** from type via this fixed map (FR-011) —
# no independent severity logic anywhere in the engine or the task.
SEVERITY_BY_TYPE: dict[AlertType, AlertSeverity] = {
    AlertType.NO_COMPETITOR_DATA: AlertSeverity.LOW,
    AlertType.RISK: AlertSeverity.CRITICAL,
    AlertType.HIGH_PRICE: AlertSeverity.HIGH,
    AlertType.CHANCE_TO_INCREASE_PRICE: AlertSeverity.MEDIUM,
    AlertType.NORMAL: AlertSeverity.NONE,
    AlertType.CLOSE_TO_COMPETITORS: AlertSeverity.MEDIUM,
}

# The resolved/no-alert types — used by `transition` (D5) to recognize a
# "back to normal" state regardless of which of the two resolved members
# is passed (NORMAL is the only one the engine ever produces; NONE is
# reserved severity vocabulary, not a type, but the set is kept generic).
_NORMAL_TYPES = frozenset({AlertType.NORMAL})


@dataclass(frozen=True)
class CompetitorPrice:
    """One competitor observation as read from ``match_current_prices`` (task-supplied)."""

    match_id: uuid.UUID
    price: Decimal | None
    currency: str | None
    success: bool
    comparable: bool


@dataclass(frozen=True)
class ComparableSplit:
    """The result of partitioning competitor rows by comparability + currency."""

    included_prices: list[Decimal] = field(default_factory=list)
    mismatched_match_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True)
class AlertOutcome:
    """The deterministic result of :func:`analyze` — everything the task persists."""

    type: AlertType
    severity: AlertSeverity
    cheapest: Decimal | None
    average: Decimal | None
    highest: Decimal | None
    comparable_count: int
    benchmark_price: Decimal | None
    discount_vs_average: Decimal | None
    mismatched_match_ids: list[uuid.UUID]
    message: str
    details: dict


def _reject_non_finite(value: Decimal, *, label: str) -> Decimal:
    """Reject a non-finite (NaN/Infinity) or over-scale ``Decimal`` at the boundary.

    Duplicates :func:`app_shared.money.parse_money`'s finite + over-scale
    rules as pure stdlib ``decimal`` code (no sqlalchemy import) — same
    §19 semantics, never silently coerced/rounded.
    """
    if not value.is_finite():
        raise ValueError(f"{label} must be finite (NaN/Infinity are rejected): {value!r}")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > _MONEY_SCALE:
        raise ValueError(
            f"{label} rejects over-scale values (more than {_MONEY_SCALE} decimal "
            f"places), not silently rounding: {value!r}"
        )
    return value


def filter_comparable(
    client_currency: str, rows: list[CompetitorPrice]
) -> ComparableSplit:
    """Partition ``rows`` into included prices and currency-mismatched ids.

    Included iff ``row.success and row.comparable and row.price is not
    None and row.currency == client_currency``. ``mismatched_match_ids``
    are rows whose ``currency`` is present and differs from
    ``client_currency`` (the task flips their ``comparable=false`` +
    ``CURRENCY_MISMATCH``); rows failing for any other reason (not
    success, ``comparable=false`` already, or a ``None`` price) are
    simply excluded, never flagged as a mismatch.
    """
    included: list[Decimal] = []
    mismatched: list[uuid.UUID] = []
    for row in rows:
        if row.currency is not None and row.currency != client_currency:
            mismatched.append(row.match_id)
        if (
            row.success
            and row.comparable
            and row.price is not None
            and row.currency == client_currency
        ):
            included.append(_reject_non_finite(row.price, label="competitor price"))
    return ComparableSplit(included_prices=included, mismatched_match_ids=mismatched)


def discount_vs_average(average: Decimal, client_price: Decimal) -> Decimal:
    """``((average - client_price) / average) * 100``, quantized 4dp ``ROUND_HALF_UP``.

    Precondition: ``average > 0`` (guaranteed by the caller — only invoked
    when at least one comparable, positive competitor price exists; never
    called when ``comparable_count == 0``).
    """
    raw = ((average - client_price) / average) * Decimal(100)
    return raw.quantize(QUANT, rounding=ROUND_HALF_UP)


def decide(
    client_price: Decimal | None,
    cheapest: Decimal | None,
    average: Decimal | None,
    highest: Decimal | None,
    comparable_count: int,
) -> tuple[AlertType, Decimal | None]:
    """The ordered §23 decision tree (steps 0-8).

    ```text
    0. client_price is None (defensive)   -> NO_COMPETITOR_DATA, discount=None
    1. comparable_count == 0              -> NO_COMPETITOR_DATA, discount=None
    2. client_price > highest             -> RISK
    3. client_price > cheapest            -> HIGH_PRICE
    4. d = discount_vs_average(...)       # Decimal, quantized 4dp
    5. d > 5                              -> CHANCE_TO_INCREASE_PRICE
    6. 1 <= d <= 5                        -> NORMAL
    7. 0 <= d < 1                         -> CLOSE_TO_COMPETITORS
    8. else (unreachable defensive)       -> HIGH_PRICE
    ```

    Step 0 is defensive: ``product_variants.current_price`` is ``NOT
    NULL`` (SPEC-04), so :func:`analyze` never passes a null client price
    in practice — the guard exists so this pure function degrades to
    ``NO_COMPETITOR_DATA`` rather than raising if ever called with
    ``None`` (D11, U1).
    """
    # Step 0 — defensive null client price (U1): must not raise.
    if client_price is None:
        return AlertType.NO_COMPETITOR_DATA, None

    # NaN/Infinity/over-scale rejected at the boundary regardless of
    # comparable_count — a malformed client price is always a caller bug.
    client_price = _reject_non_finite(client_price, label="client price")

    # Step 1 — no comparable competitor data at all.
    if comparable_count == 0 or cheapest is None or average is None or highest is None:
        return AlertType.NO_COMPETITOR_DATA, None

    cheapest = _reject_non_finite(cheapest, label="cheapest")
    average = _reject_non_finite(average, label="average")
    highest = _reject_non_finite(highest, label="highest")

    # Steps 2-3 — strict `>` (equal-to-highest/cheapest falls through).
    if client_price > highest:
        return AlertType.RISK, None
    if client_price > cheapest:
        return AlertType.HIGH_PRICE, None

    # Step 4 — quantized discount, Decimal-vs-Decimal from here on.
    d = discount_vs_average(average, client_price)

    # Step 5.
    if d > Decimal("5"):
        return AlertType.CHANCE_TO_INCREASE_PRICE, d
    # Step 6.
    if Decimal("1") <= d <= Decimal("5"):
        return AlertType.NORMAL, d
    # Step 7.
    if Decimal("0") <= d < Decimal("1"):
        return AlertType.CLOSE_TO_COMPETITORS, d
    # Step 8 — unreachable defensive fallback (only reachable via a
    # hand-constructed degenerate input in tests, e.g. d < 0 which cannot
    # arise from the real engine since client_price <= cheapest <= average
    # by this point).
    return AlertType.HIGH_PRICE, d


def severity_for(alert_type: AlertType) -> AlertSeverity:
    """The only source of severity (FR-011) — a total lookup over ``SEVERITY_BY_TYPE``."""
    return SEVERITY_BY_TYPE[alert_type]


_BENCHMARK_FIELD_BY_TYPE: dict[AlertType, str] = {
    AlertType.RISK: "highest",
    AlertType.HIGH_PRICE: "cheapest",
    AlertType.CHANCE_TO_INCREASE_PRICE: "average",
    AlertType.NORMAL: "average",
    AlertType.CLOSE_TO_COMPETITORS: "average",
}


def analyze(
    client_price: Decimal | None,
    client_currency: str,
    competitor_rows: list[CompetitorPrice],
) -> AlertOutcome:
    """Orchestrate the engine over one variant's inputs -> a deterministic :class:`AlertOutcome`.

    Pure and time-free: identical inputs always yield an equal
    ``AlertOutcome`` (SC-001) — the task adds timestamps, not this
    function.
    """
    split = filter_comparable(client_currency, competitor_rows)
    count = len(split.included_prices)

    cheapest: Decimal | None = None
    average: Decimal | None = None
    highest: Decimal | None = None
    if count > 0:
        cheapest = min(split.included_prices)
        highest = max(split.included_prices)
        average = sum(split.included_prices, Decimal(0)) / count

    alert_type, discount = decide(client_price, cheapest, average, highest, count)
    severity = severity_for(alert_type)

    benchmark_field = _BENCHMARK_FIELD_BY_TYPE.get(alert_type)
    benchmark_price: Decimal | None = None
    if benchmark_field == "highest":
        benchmark_price = highest
    elif benchmark_field == "cheapest":
        benchmark_price = cheapest
    elif benchmark_field == "average":
        benchmark_price = average

    message = _build_message(alert_type, count, discount)
    details = {
        "comparable_competitor_count": count,
        "discount_vs_average": str(discount) if discount is not None else None,
        "mismatched_match_ids": [str(mid) for mid in split.mismatched_match_ids],
    }

    return AlertOutcome(
        type=alert_type,
        severity=severity,
        cheapest=cheapest,
        average=average,
        highest=highest,
        comparable_count=count,
        benchmark_price=benchmark_price,
        discount_vs_average=discount,
        mismatched_match_ids=list(split.mismatched_match_ids),
        message=message,
        details=details,
    )


def _build_message(alert_type: AlertType, count: int, discount: Decimal | None) -> str:
    """Deterministic, time-free human message (no timestamps inside — SC-001)."""
    if alert_type is AlertType.NO_COMPETITOR_DATA:
        return "No comparable competitor price data is available for this variant."
    if alert_type is AlertType.RISK:
        return (
            f"Client price is above the highest of {count} comparable competitor price(s)."
        )
    if alert_type is AlertType.HIGH_PRICE:
        return (
            f"Client price is above the cheapest of {count} comparable competitor price(s)."
        )
    if alert_type is AlertType.CHANCE_TO_INCREASE_PRICE:
        return (
            f"Client price is {discount}% below the average of {count} comparable "
            "competitor price(s) — more than 5% below average."
        )
    if alert_type is AlertType.NORMAL:
        return (
            f"Client price is {discount}% below the average of {count} comparable "
            "competitor price(s) — within the normal 1%-5% band."
        )
    # CLOSE_TO_COMPETITORS
    return (
        f"Client price is {discount}% below the average of {count} comparable "
        "competitor price(s) — closely matching competitors."
    )


def transition(
    prev_type: AlertType | None,
    prev_severity: AlertSeverity | None,
    new_type: AlertType,
    new_severity: AlertSeverity,
    *,
    had_history: bool,
) -> AlertEventType | None:
    """The ordered D5 event-transition rule.

    ```text
    prev is None and new in {NORMAL}                 -> None (no event)
    prev is None and new not NORMAL                   -> CREATED
    (prev_type, prev_severity) == (new_type, new_sev) -> None (UNCHANGED, not persisted)
    prev not NORMAL and new in {NORMAL}               -> RESOLVED
    prev in {NORMAL} and new not NORMAL and had_history -> REOPENED
    prev not NORMAL and new not NORMAL (differ)       -> UPDATED
    ```

    ``had_history`` = a prior ``variant_alert_states`` row exists
    (distinguishes CREATED from REOPENED). Returns ``None`` for the
    no-event and UNCHANGED cases — the task then writes no event, only
    advances ``last_seen_at``.
    """
    new_is_normal = new_type in _NORMAL_TYPES

    if prev_type is None:
        if new_is_normal:
            return None
        return AlertEventType.CREATED

    prev_is_normal = prev_type in _NORMAL_TYPES

    if prev_type == new_type and prev_severity == new_severity:
        return None

    if not prev_is_normal and new_is_normal:
        return AlertEventType.RESOLVED

    if prev_is_normal and not new_is_normal and had_history:
        return AlertEventType.REOPENED

    if not prev_is_normal and not new_is_normal:
        return AlertEventType.UPDATED

    # Defensive fallback (e.g. prev NORMAL -> new non-NORMAL without
    # had_history — cannot arise once a NORMAL row itself constitutes
    # history; kept as CREATED-equivalent so a real caller never loses
    # an event entirely).
    return AlertEventType.CREATED
