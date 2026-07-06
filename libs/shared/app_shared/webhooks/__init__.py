"""Webhook event payload builders (SPEC-16 US3).

Scraping-free, framework-agnostic package (stdlib ``json`` + ``app_shared.enums``
only — no sqlalchemy/celery/fastapi/scrapy, see ``tests/unit/test_import_boundaries.py``).
"""

from __future__ import annotations

from app_shared.webhooks.payloads import (
    WEBHOOK_PAYLOAD_MAX_BYTES,
    PayloadTooLargeError,
    build_alert_event,
    build_job_event,
    build_strategy_event,
)

__all__ = [
    "WEBHOOK_PAYLOAD_MAX_BYTES",
    "PayloadTooLargeError",
    "build_alert_event",
    "build_job_event",
    "build_strategy_event",
]
