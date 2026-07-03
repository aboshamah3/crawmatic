"""Framework-agnostic match-upsert core (SPEC-05).

Pure business logic for competitor-product matches — save-time URL
safety application, versioned URL pattern derivation, variant
reference resolution, and set-based bulk-upsert statement builders.
No FastAPI, no Scrapy/Twisted/Playwright (see
``tests/unit/test_import_boundaries.py``). Empty package init for
now — the ``upsert`` submodule lands in a later SPEC-05 phase (US3).
"""

from __future__ import annotations

__all__: list[str] = []
