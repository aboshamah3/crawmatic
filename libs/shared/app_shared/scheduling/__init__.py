"""Scraping-free scheduling primitives (SPEC-13 Scheduler).

Cadence math shared by the ``/v1/refresh-rules`` API (first ``next_run_at``,
cron validation) and the scheduler pass (recompute per run) —
``compute_next_run_at``/``validate_cron`` in ``cadence.py``. No FastAPI, no
Scrapy/Twisted/Playwright (see ``tests/unit/test_import_boundaries.py``).
Empty package init for now — ``cadence.py`` lands in a later phase of this
spec.
"""

from __future__ import annotations

__all__: list[str] = []
