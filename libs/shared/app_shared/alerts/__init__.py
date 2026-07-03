"""Framework-agnostic price-alert engine (SPEC-09).

Pure business logic turning a variant's client price + its comparable
competitor prices into a deterministic alert type/severity + benchmarks
via the ordered §23 decision tree, the fixed severity map, the currency
filter, and the ordered event-transition rule (D1/D2/D5/D6). Depends on
stdlib ``decimal`` + ``app_shared.enums`` only — no sqlalchemy, celery,
fastapi, scrapy, or redis (see ``tests/unit/test_import_boundaries.py``).
"""

from __future__ import annotations

from app_shared.alerts.engine import (
    QUANT,
    SEVERITY_BY_TYPE,
    AlertOutcome,
    ComparableSplit,
    CompetitorPrice,
    analyze,
    decide,
    discount_vs_average,
    filter_comparable,
    severity_for,
    transition,
)

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
