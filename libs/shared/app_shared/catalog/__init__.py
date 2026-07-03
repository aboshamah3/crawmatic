"""Framework-agnostic catalog core (SPEC-04).

Pure business logic for products/variants/groups — default-variant
derivation, workspace-consistency pre-checks, and set-based bulk-upsert
statement builders. No FastAPI, no Scrapy/Twisted/Playwright (see
``tests/unit/test_import_boundaries.py``). Empty package init for now —
submodules land in later SPEC-04 phases (US1+).
"""

from __future__ import annotations

__all__: list[str] = []
