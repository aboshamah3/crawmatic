"""Unit tests for the SPEC-16 webhook payload builders (T030,
contracts/events.md).

Covers, per builder: the correct `event_type` string + payload keys/values
for every relevant source enum member, that the "never persisted" source
members (`AlertEventType.UNCHANGED`, `ScrapeJobStatus.CANCELLED`) produce NO
event, the `dedup_key` format, and the < 8 KiB size guard.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app_shared.enums import (
    AlertEventType,
    AlertSeverity,
    AlertType,
    ScrapeJobStatus,
    StrategyStatus,
)
from app_shared.webhooks.payloads import (
    WEBHOOK_PAYLOAD_MAX_BYTES,
    PayloadTooLargeError,
    build_alert_event,
    build_job_event,
    build_strategy_event,
)


# --- build_alert_event ------------------------------------------------------


@pytest.mark.parametrize(
    "transition,expected_event_type",
    [
        (AlertEventType.CREATED, "price.alert.created"),
        (AlertEventType.UPDATED, "price.alert.updated"),
        (AlertEventType.RESOLVED, "price.alert.resolved"),
        (AlertEventType.REOPENED, "price.alert.reopened"),
    ],
)
def test_build_alert_event_maps_every_persisted_transition(
    transition: AlertEventType, expected_event_type: str
) -> None:
    variant_id = uuid.uuid4()
    product_id = uuid.uuid4()
    alert_state_id = uuid.uuid4()
    scrape_job_id = uuid.uuid4()

    result = build_alert_event(
        product_variant_id=variant_id,
        product_id=product_id,
        alert_state_id=alert_state_id,
        transition=transition,
        previous_type=AlertType.NORMAL,
        new_type=AlertType.RISK,
        previous_severity=AlertSeverity.LOW,
        new_severity=AlertSeverity.HIGH,
        scrape_job_id=scrape_job_id,
    )

    assert result is not None
    event_type, payload, dedup_key = result

    assert event_type == expected_event_type
    assert payload == {
        "product_variant_id": str(variant_id),
        "product_id": str(product_id),
        "alert_state_id": str(alert_state_id),
        "previous_type": "NORMAL",
        "new_type": "RISK",
        "previous_severity": "LOW",
        "new_severity": "HIGH",
        "transition": transition.value,
    }
    assert dedup_key == f"alert:{alert_state_id}:{transition.value}:{scrape_job_id}"


def test_build_alert_event_unchanged_produces_no_event() -> None:
    result = build_alert_event(
        product_variant_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        alert_state_id=uuid.uuid4(),
        transition=AlertEventType.UNCHANGED,
        previous_type=AlertType.NORMAL,
        new_type=AlertType.NORMAL,
        previous_severity=AlertSeverity.NONE,
        new_severity=AlertSeverity.NONE,
    )
    assert result is None


def test_build_alert_event_null_previous_and_default_scrape_job_id() -> None:
    alert_state_id = uuid.uuid4()
    result = build_alert_event(
        product_variant_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        alert_state_id=alert_state_id,
        transition=AlertEventType.CREATED,
        previous_type=None,
        new_type=AlertType.RISK,
        previous_severity=None,
        new_severity=AlertSeverity.HIGH,
    )
    assert result is not None
    _, payload, dedup_key = result
    assert payload["previous_type"] is None
    assert payload["previous_severity"] is None
    assert dedup_key == f"alert:{alert_state_id}:CREATED:api"


# --- build_job_event ---------------------------------------------------


@pytest.mark.parametrize(
    "status,expected_event_type",
    [
        (ScrapeJobStatus.COMPLETED, "scrape.job.completed"),
        (ScrapeJobStatus.PARTIAL_FAILED, "scrape.job.partial_failed"),
        (ScrapeJobStatus.FAILED, "scrape.job.failed"),
    ],
)
def test_build_job_event_maps_every_terminal_status(
    status: ScrapeJobStatus, expected_event_type: str
) -> None:
    job_id = uuid.uuid4()
    result = build_job_event(
        scrape_job_id=job_id,
        status=status,
        success_count=120,
        failure_count=3,
        skipped_count=1,
        total=124,
    )

    assert result is not None
    event_type, payload, dedup_key = result

    assert event_type == expected_event_type
    assert payload == {
        "scrape_job_id": str(job_id),
        "status": status.value,
        "success_count": 120,
        "failure_count": 3,
        "skipped_count": 1,
        "total": 124,
    }
    assert dedup_key == f"job:{job_id}:{status.value}"


def test_build_job_event_cancelled_produces_no_event() -> None:
    result = build_job_event(
        scrape_job_id=uuid.uuid4(),
        status=ScrapeJobStatus.CANCELLED,
        success_count=0,
        failure_count=0,
        skipped_count=0,
        total=0,
    )
    assert result is None


def test_build_job_event_non_terminal_status_produces_no_event() -> None:
    result = build_job_event(
        scrape_job_id=uuid.uuid4(),
        status=ScrapeJobStatus.PENDING,
        success_count=0,
        failure_count=0,
        skipped_count=0,
        total=0,
    )
    assert result is None


# --- build_strategy_event ------------------------------------------------


def test_build_strategy_event_promotion() -> None:
    profile_id = uuid.uuid4()
    event_type, payload, dedup_key = build_strategy_event(
        strategy_profile_id=profile_id,
        domain="example.com",
        new_status=StrategyStatus.ACTIVE,
        change="PROMOTED",
        method="DIRECT_HTTP",
    )

    assert event_type == "domain.strategy.updated"
    assert payload == {
        "strategy_profile_id": str(profile_id),
        "domain": "example.com",
        "new_status": "ACTIVE",
        "change": "PROMOTED",
        "method": "DIRECT_HTTP",
    }
    assert dedup_key == f"strategy:{profile_id}:ACTIVE:PROMOTED"


def test_build_strategy_event_rediscovery_has_no_method() -> None:
    profile_id = uuid.uuid4()
    event_type, payload, dedup_key = build_strategy_event(
        strategy_profile_id=profile_id,
        domain="example.com",
        new_status=StrategyStatus.DEGRADED,
        change="REDISCOVERY_TRIGGERED",
    )

    assert event_type == "domain.strategy.updated"
    assert payload["method"] is None
    assert payload["new_status"] == "DEGRADED"
    assert dedup_key == f"strategy:{profile_id}:DEGRADED:REDISCOVERY_TRIGGERED"


# --- size guard ----------------------------------------------------------


def test_size_guard_raises_on_oversized_payload() -> None:
    with pytest.raises(PayloadTooLargeError):
        build_strategy_event(
            strategy_profile_id=uuid.uuid4(),
            domain="x" * (WEBHOOK_PAYLOAD_MAX_BYTES + 100),
            new_status=StrategyStatus.ACTIVE,
            change="PROMOTED",
        )


def test_size_guard_boundary_is_under_max_bytes_for_typical_payload() -> None:
    _, payload, _ = build_alert_event(
        product_variant_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        alert_state_id=uuid.uuid4(),
        transition=AlertEventType.CREATED,
        previous_type=AlertType.NORMAL,
        new_type=AlertType.RISK,
        previous_severity=AlertSeverity.LOW,
        new_severity=AlertSeverity.HIGH,
    )
    assert len(json.dumps(payload).encode("utf-8")) < WEBHOOK_PAYLOAD_MAX_BYTES
