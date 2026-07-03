"""Celery task-name string constants.

Task names live here as plain strings so any process — a Scrapyd
spider, the scheduler, the API — can enqueue work via Celery's
``send_task(name, ...)`` without importing ``apps/workers`` itself.
That indirection is the dependency boundary that keeps
scrapy/twisted/playwright out of the worker/API import closures
(Constitution V: Disciplined Scraping Runtime).

This module intentionally imports nothing from ``celery``.
"""

from __future__ import annotations

# --- Jobs & orchestration (SPEC-08 FR-011, FR-015, D8) ---
# Enqueued via ``app_shared.messaging.enqueue`` by the API/pipeline; consumed
# by ``apps/workers/app/workers/tasks_jobs.py``.
SCRAPE_DISPATCH_JOB = "scrape_dispatch.dispatch_job"
SCRAPE_RECOVER_STALLED = "maintenance.recover_stalled_batches"
SCRAPE_FINALIZE_JOBS = "maintenance.finalize_jobs"
