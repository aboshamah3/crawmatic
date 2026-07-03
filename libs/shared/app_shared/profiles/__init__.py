"""Framework-agnostic scrape-profile core (SPEC-06).

Per the SPEC-06 scope boundary: the ``ScrapeProfile`` validators,
DB-tunable confidence defaults, the config-resolution core, the
dual-scope repository query helpers, and the pure bulk-upsert
statement builder all live under this package. Pure Python +
SQLAlchemy Core/``redis``-free — no ``fastapi``/``scrapy``/``twisted``/
``playwright`` import anywhere under here (asserted by
``tests/unit/test_import_boundaries.py``). The Pydantic schemas,
FastAPI router, and the Redis-driving resolution orchestrator that
consume this package live only in ``apps/api``.

Package marker only in Phase 1 — individual modules (``validation``,
``confidence``, ``resolution``, ``repository``, ``upsert``) land in
later phases.
"""

from __future__ import annotations
