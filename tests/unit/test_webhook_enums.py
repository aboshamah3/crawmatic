"""Unit tests for the SPEC-16 webhook StrEnums (T006).

Pins the exact member -> value mappings for ``WebhookEventStatus`` and
``WebhookEventType`` so accidental renames/typos in the taxonomy (which
producers and readers both depend on as plain strings) are caught here
rather than at a live integration boundary.
"""

from __future__ import annotations

from app_shared.enums import StrEnum, WebhookEventStatus, WebhookEventType


def test_webhook_event_status_members_and_values() -> None:
    assert issubclass(WebhookEventStatus, StrEnum)
    assert WebhookEventStatus.PENDING == "PENDING"
    assert WebhookEventStatus.DELIVERED == "DELIVERED"
    assert WebhookEventStatus.FAILED == "FAILED"
    assert {member.value for member in WebhookEventStatus} == {
        "PENDING",
        "DELIVERED",
        "FAILED",
    }


def test_webhook_event_type_has_exactly_eleven_members_with_expected_strings() -> None:
    """The published event vocabulary, pinned member-for-member.

    Started at eight and has grown three times, each a *deliberate*
    vocabulary addition that had to move this pin before it could ship —
    which is exactly what this test is for: an accidental rename or typo
    still fails here.

    * `SCRAPE_JOB_CANCELLED` / `scrape.job.cancelled` (EPA A2,
      2026-08-25) — the durable event
      `app_shared.jobs.cancellation.cancel_and_reconcile_job` records
      when a job is administratively closed.
    * `BUDGET_THRESHOLD_WARNING` / `budget.threshold.warning` (EPA C3,
      2026-08-25) — a cost budget crossed one of its 50/75/90% marks,
      emitted only by `app_shared.costauth.service`.
    * `SCHEDULER_ITEM_DEAD_LETTERED` / `scheduler.item.dead_lettered`
      (EPA W4.2, 2026-08-25) — a `refresh_rules` row exhausted its
      bounded retries and was disabled pending replay.

    All three reuse the existing `webhook_events.create_webhook_event`
    consumer (EPA B7's ruling: no new, unregistered outbox task names).
    """
    assert issubclass(WebhookEventType, StrEnum)

    expected = {
        "PRICE_ALERT_CREATED": "price.alert.created",
        "PRICE_ALERT_UPDATED": "price.alert.updated",
        "PRICE_ALERT_RESOLVED": "price.alert.resolved",
        "PRICE_ALERT_REOPENED": "price.alert.reopened",
        "SCRAPE_JOB_COMPLETED": "scrape.job.completed",
        "SCRAPE_JOB_PARTIAL": "scrape.job.partial_failed",
        "SCRAPE_JOB_FAILED": "scrape.job.failed",
        "SCRAPE_JOB_CANCELLED": "scrape.job.cancelled",
        "DOMAIN_STRATEGY_UPDATED": "domain.strategy.updated",
        "BUDGET_THRESHOLD_WARNING": "budget.threshold.warning",
        "SCHEDULER_ITEM_DEAD_LETTERED": "scheduler.item.dead_lettered",
    }

    members = list(WebhookEventType)
    assert len(members) == 11

    actual = {member.name: member.value for member in members}
    assert actual == expected


def test_webhook_event_status_pending_is_string_equal_to_its_literal() -> None:
    # StrEnum value equality: the member compares equal to its plain str value.
    assert WebhookEventStatus.PENDING == "PENDING"
    assert str(WebhookEventStatus.PENDING) == "PENDING"
