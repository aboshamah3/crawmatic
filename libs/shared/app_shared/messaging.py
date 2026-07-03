"""Enqueue-by-name Celery producer seam (SPEC-08, contracts/messaging.md, D8).

Lets the API (and later the scheduler / SPEC-07 pipeline) enqueue Celery
work by **task name** — via ``app_shared.task_names`` constants — without
importing ``apps/workers``. That indirection is the dependency boundary
that keeps the worker's (and its future scrapy-adjacent) import closure out
of the API (Constitution I).

Mirrors the lazy-singleton pattern in ``app_shared.redis_client`` /
``app_shared.database``: the producer is built on first use from
``Settings.REDIS_URL`` and cached per-process — never at import time (would
defeat fail-fast config validation) and never per-call.

Import boundary: this module may import ``celery`` (the ban is
scrapy/twisted/playwright/fastapi); ``task_names.py`` itself stays
celery-free.
"""

from __future__ import annotations

from typing import Any

from celery import Celery

from app_shared.config import get_settings

_producer: Celery | None = None


def _get_producer() -> Celery:
    """Return the per-process Celery producer, creating it on first use."""
    global _producer
    if _producer is None:
        settings = get_settings()
        _producer = Celery(broker=settings.REDIS_URL, backend=None)
    return _producer


def enqueue(name: str, *, queue: str, kwargs: dict[str, Any] | None = None) -> None:
    """Send a task by ``name`` (from ``app_shared.task_names``) to ``queue``.

    ``kwargs`` are passed straight through to ``send_task`` — no result is
    awaited (fire-and-forget producer seam).
    """
    producer = _get_producer()
    producer.send_task(name, kwargs=kwargs, queue=queue)
