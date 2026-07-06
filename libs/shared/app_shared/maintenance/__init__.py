"""Scraping-free retention, rollups & partition maintenance core (SPEC-15).

Pure/DB-facing maintenance logic backing the three scheduled maintenance
jobs: the partition registry + primitives (``registry``, ``partitions``),
the daily rollup aggregation (``rollups``), retention/eligibility + drop
(``retention``), and the dangling soft-reference tolerance check
(``soft_refs``). SQLAlchemy / stdlib only — no Scrapy/Twisted/Playwright
(Constitution I/V, enforced by ``tests/unit/test_import_boundaries.py``).
Empty package init for now — the individual modules land in later phases
of this spec.
"""

from __future__ import annotations

__all__: list[str] = []
