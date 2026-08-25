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


def test_webhook_event_type_has_exactly_nine_members_with_expected_strings() -> None:
    """The published event vocabulary, pinned member-for-member.

    Grew from eight to nine on 2026-08-25 (EPA A2): `SCRAPE_JOB_CANCELLED`
    /`scrape.job.cancelled` is the durable event
    `app_shared.jobs.cancellation.cancel_and_reconcile_job` records when a
    job is administratively closed. It is a *deliberate* vocabulary
    addition, so the pin moves with it — which is exactly what this test
    is for: an accidental rename or typo still fails here, while a real
    addition has to be stated in one obvious place before it can ship.
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
    }

    members = list(WebhookEventType)
    assert len(members) == 9

    actual = {member.name: member.value for member in members}
    assert actual == expected


def test_webhook_event_status_pending_is_string_equal_to_its_literal() -> None:
    # StrEnum value equality: the member compares equal to its plain str value.
    assert WebhookEventStatus.PENDING == "PENDING"
    assert str(WebhookEventStatus.PENDING) == "PENDING"
