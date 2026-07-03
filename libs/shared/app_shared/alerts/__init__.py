"""Framework-agnostic price-alert engine (SPEC-09).

Pure business logic turning a variant's client price + its comparable
competitor prices into a deterministic alert type/severity + benchmarks
via the ordered §23 decision tree, the fixed severity map, the currency
filter, and the ordered event-transition rule (D1/D2/D5/D6). Depends on
stdlib ``decimal`` + ``app_shared.enums`` only — no sqlalchemy, celery,
fastapi, scrapy, or redis (see ``tests/unit/test_import_boundaries.py``).
Empty package init for now — ``engine.py`` + its re-exports land in US1.
"""

from __future__ import annotations

__all__: list[str] = []
