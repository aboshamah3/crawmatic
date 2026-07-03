"""Framework-agnostic jobs & orchestration core (SPEC-08).

Pure business logic for scrape-job orchestration — HTTP batching by
competitor-domain + mode (``batching``), deterministic hash-by-domain node
selection (``nodes``), the deterministic finalized-status rule + stall-window
bucketing (``lifecycle``), the target state-transition writer + counter
aggregation (``targets``), and job-creation + dispatch orchestration
(``service``). No FastAPI, no Scrapy/Twisted/Playwright (see
``tests/unit/test_import_boundaries.py``). Empty package init for now — the
individual modules land in later phases of this spec.
"""

from __future__ import annotations

__all__: list[str] = []
