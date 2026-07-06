"""Pure webhook-event payload builders (SPEC-16 US3, contracts/events.md).

One builder per producer seam — maps an existing source-domain enum
transition to ``(event_type, payload, dedup_key)`` per the taxonomy in
``contracts/events.md``:

* :func:`build_alert_event` — SPEC-09 ``AlertEventType`` transitions
  (``recompute_variant``). ``AlertEventType.UNCHANGED`` never persists a
  ``price_alert_events`` row upstream and produces **no event** here either
  (returns ``None``).
* :func:`build_job_event` — SPEC-08 ``ScrapeJobStatus`` terminal statuses
  (``finalize_jobs``). ``ScrapeJobStatus.CANCELLED`` (and any non-terminal
  status) produces **no event** (returns ``None``).
* :func:`build_strategy_event` — SPEC-12 promotion/rediscovery transitions
  (``flush_stats`` / ``light_recheck``). Only ever called by its seams for a
  genuine transition, so it always returns a triple.

Every payload is built from a small fixed set of ids + change descriptors
and is guarded by :func:`_guard_payload_size` (< 8 KiB serialized, the
"very large payload" edge case) — a builder that would exceed the bound
raises :class:`PayloadTooLargeError` rather than ever silently truncating
or storing an unbounded blob.

Framework-agnostic (stdlib ``json`` + ``app_shared.enums`` only) — imported
by both worker seams (``apps/workers/app/workers/tasks_*.py``) and the
Celery task itself; never imports sqlalchemy/celery/fastapi/scrapy (see
``tests/unit/test_import_boundaries.py``).
"""

from __future__ import annotations

import json
import uuid

from app_shared.enums import (
    AlertEventType,
    AlertSeverity,
    AlertType,
    ScrapeJobStatus,
    StrategyStatus,
    WebhookEventType,
)

__all__ = [
    "WEBHOOK_PAYLOAD_MAX_BYTES",
    "PayloadTooLargeError",
    "build_alert_event",
    "build_job_event",
    "build_strategy_event",
]

#: FR "very large payload" edge case — a payload builder must never let a
#: source store an unbounded blob (contracts/events.md, plan.md decision 4).
WEBHOOK_PAYLOAD_MAX_BYTES = 8 * 1024

# SPEC-09 AlertEventType -> webhook event_type (contracts/events.md #1).
# UNCHANGED is deliberately absent -- not a KeyError bug, the lookup below
# treats "absent" as "no event" (mirrors the upstream `if event_type is not
# None` guard: UNCHANGED never persists a `price_alert_events` row either).
_ALERT_EVENT_TYPES: dict[AlertEventType, WebhookEventType] = {
    AlertEventType.CREATED: WebhookEventType.PRICE_ALERT_CREATED,
    AlertEventType.UPDATED: WebhookEventType.PRICE_ALERT_UPDATED,
    AlertEventType.RESOLVED: WebhookEventType.PRICE_ALERT_RESOLVED,
    AlertEventType.REOPENED: WebhookEventType.PRICE_ALERT_REOPENED,
}

# SPEC-08 ScrapeJobStatus (terminal only) -> webhook event_type
# (contracts/events.md #2). CANCELLED is deliberately absent -- it is a
# vocabulary member but is never produced by SPEC-08 `finalize_jobs` in v1.
_JOB_EVENT_TYPES: dict[ScrapeJobStatus, WebhookEventType] = {
    ScrapeJobStatus.COMPLETED: WebhookEventType.SCRAPE_JOB_COMPLETED,
    ScrapeJobStatus.PARTIAL_FAILED: WebhookEventType.SCRAPE_JOB_PARTIAL,
    ScrapeJobStatus.FAILED: WebhookEventType.SCRAPE_JOB_FAILED,
}


class PayloadTooLargeError(ValueError):
    """Raised when a builder's serialized payload exceeds ``WEBHOOK_PAYLOAD_MAX_BYTES``."""


def _guard_payload_size(payload: dict) -> dict:
    """Assert ``json.dumps(payload)`` serializes under the 8 KiB bound.

    Returns ``payload`` unchanged (so callers can wrap the construction
    expression, e.g. ``return event_type, _guard_payload_size({...}), key``).
    """
    size = len(json.dumps(payload).encode("utf-8"))
    if size >= WEBHOOK_PAYLOAD_MAX_BYTES:
        raise PayloadTooLargeError(
            f"webhook payload is {size} bytes, exceeding the "
            f"{WEBHOOK_PAYLOAD_MAX_BYTES}-byte guard"
        )
    return payload


def build_alert_event(
    *,
    product_variant_id: uuid.UUID | str,
    product_id: uuid.UUID | str,
    alert_state_id: uuid.UUID | str,
    transition: AlertEventType,
    previous_type: AlertType | None,
    new_type: AlertType,
    previous_severity: AlertSeverity | None,
    new_severity: AlertSeverity,
    scrape_job_id: uuid.UUID | str | None = None,
) -> tuple[str, dict, str] | None:
    """Build the ``price.alert.*`` webhook event for one alert-state transition.

    ``None`` when ``transition`` is :attr:`AlertEventType.UNCHANGED` (or any
    other value outside the taxonomy) — mirrors the seam's own
    ``if event_type is not None`` guard (contracts/events.md #1).
    """
    webhook_type = _ALERT_EVENT_TYPES.get(transition)
    if webhook_type is None:
        return None

    payload = _guard_payload_size(
        {
            "product_variant_id": str(product_variant_id),
            "product_id": str(product_id),
            "alert_state_id": str(alert_state_id),
            "previous_type": previous_type.value if previous_type is not None else None,
            "new_type": new_type.value,
            "previous_severity": (
                previous_severity.value if previous_severity is not None else None
            ),
            "new_severity": new_severity.value,
            "transition": transition.value,
        }
    )
    dedup_key = f"alert:{alert_state_id}:{transition.value}:{scrape_job_id or 'api'}"
    return webhook_type.value, payload, dedup_key


def build_job_event(
    *,
    scrape_job_id: uuid.UUID | str,
    status: ScrapeJobStatus,
    success_count: int,
    failure_count: int,
    skipped_count: int,
    total: int,
) -> tuple[str, dict, str] | None:
    """Build the ``scrape.job.*`` webhook event for one finalized job.

    ``None`` when ``status`` is :attr:`ScrapeJobStatus.CANCELLED` (or any
    other non-terminal-for-webhooks status) — that status is never produced
    by ``finalize_jobs`` (contracts/events.md #2).
    """
    webhook_type = _JOB_EVENT_TYPES.get(status)
    if webhook_type is None:
        return None

    payload = _guard_payload_size(
        {
            "scrape_job_id": str(scrape_job_id),
            "status": status.value,
            "success_count": success_count,
            "failure_count": failure_count,
            "skipped_count": skipped_count,
            "total": total,
        }
    )
    dedup_key = f"job:{scrape_job_id}:{status.value}"
    return webhook_type.value, payload, dedup_key


def build_strategy_event(
    *,
    strategy_profile_id: uuid.UUID | str,
    domain: str,
    new_status: StrategyStatus,
    change: str,
    method: str | None = None,
) -> tuple[str, dict, str]:
    """Build the ``domain.strategy.updated`` webhook event for one genuine
    promotion/rediscovery transition (contracts/events.md #3).

    Only ever called by its seams (``flush_stats``/``light_recheck``) for a
    transition an ``apply_*`` helper already confirmed genuine (returned
    ``True``) — unlike the other two builders there is no "no event" case.
    ``change`` is one of ``"PROMOTED"`` / ``"REDISCOVERY_TRIGGERED"``.
    """
    payload = _guard_payload_size(
        {
            "strategy_profile_id": str(strategy_profile_id),
            "domain": domain,
            "new_status": new_status.value,
            "change": change,
            "method": method,
        }
    )
    dedup_key = f"strategy:{strategy_profile_id}:{new_status.value}:{change}"
    return WebhookEventType.DOMAIN_STRATEGY_UPDATED.value, payload, dedup_key
