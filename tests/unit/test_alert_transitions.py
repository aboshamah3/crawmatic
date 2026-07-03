"""`transition()` truth table (SPEC-09 T016, D5, FR-013, SC-004).

All six real cases + both `None`-previous cases + the defensive
same-type/different-severity -> UPDATED branch (hand-constructed input,
I1) — a same-type severity change cannot arise from the real engine
(severity is a pure function of type), so it is exercised here only via
a hand-constructed input.
"""

from __future__ import annotations

from app_shared.alerts.engine import transition
from app_shared.enums import AlertEventType, AlertSeverity, AlertType


def test_prev_none_new_normal_is_no_event() -> None:
    result = transition(None, None, AlertType.NORMAL, AlertSeverity.NONE, had_history=False)
    assert result is None


def test_prev_none_new_non_normal_is_created() -> None:
    result = transition(
        None, None, AlertType.HIGH_PRICE, AlertSeverity.HIGH, had_history=False
    )
    assert result is AlertEventType.CREATED


def test_prev_equals_new_is_unchanged_not_persisted() -> None:
    result = transition(
        AlertType.HIGH_PRICE,
        AlertSeverity.HIGH,
        AlertType.HIGH_PRICE,
        AlertSeverity.HIGH,
        had_history=True,
    )
    assert result is None


def test_prev_non_normal_new_normal_is_resolved() -> None:
    result = transition(
        AlertType.HIGH_PRICE,
        AlertSeverity.HIGH,
        AlertType.NORMAL,
        AlertSeverity.NONE,
        had_history=True,
    )
    assert result is AlertEventType.RESOLVED


def test_prev_normal_new_non_normal_with_history_is_reopened() -> None:
    result = transition(
        AlertType.NORMAL,
        AlertSeverity.NONE,
        AlertType.HIGH_PRICE,
        AlertSeverity.HIGH,
        had_history=True,
    )
    assert result is AlertEventType.REOPENED


def test_prev_non_normal_new_different_non_normal_type_change_is_updated() -> None:
    result = transition(
        AlertType.HIGH_PRICE,
        AlertSeverity.HIGH,
        AlertType.RISK,
        AlertSeverity.CRITICAL,
        had_history=True,
    )
    assert result is AlertEventType.UPDATED


def test_defensive_same_type_different_severity_is_updated_hand_constructed() -> None:
    """I1: a same-type severity change cannot arise from the real engine
    (severity_for is a pure function of type), so this branch is exercised
    only via a hand-constructed input."""
    result = transition(
        AlertType.HIGH_PRICE,
        AlertSeverity.HIGH,
        AlertType.HIGH_PRICE,
        AlertSeverity.CRITICAL,
        had_history=True,
    )
    assert result is AlertEventType.UPDATED
