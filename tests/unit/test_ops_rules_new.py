"""EPA B9 (F22, audit §13 Operations) — the new `app_shared.opsmetrics.rules`
alert rules.

Every rule under test here reads a gauge that is not yet a declared
`OpsSnapshot` field — wiring the real DB/Redis collection into
`OpsSnapshot`/`collect_snapshot` is a follow-up outside this task's file
scope (see the "EPA B9 new alert rules" comment in `rules.py`). Each rule
is exercised here the same way the codebase already tests
`ATTEMPTS_PER_VALID_FRESH_MAX`-style gauges: attach the attribute to a
real `OpsSnapshot` via `object.__setattr__` (it is `frozen=True` but not
`slots=True`), fire/no-fire at the threshold ± a small delta.

`queue_oldest_pending_seconds > 3600` (also in the plan's rule list) is
NOT a new rule — `_r_queue`'s existing `queue.pending_target_age` already
fires at exactly this condition against the already-wired
`snapshot.queue.oldest_pending_target_age_seconds`. Its own ±1 coverage
is included below for completeness.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app_shared.opsmetrics.rules import (
    DEFAULT_THRESHOLDS,
    RULES,
    Severity,
    evaluate,
)
from app_shared.opsmetrics.snapshot import BreakerHealth, OpsSnapshot, QueueHealth

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _snapshot(**extra: object) -> OpsSnapshot:
    """A minimal, otherwise-silent `OpsSnapshot` with `extra` attached as
    ad-hoc attributes (the not-yet-declared B9 gauges)."""
    snap = OpsSnapshot(collected_at=NOW)
    for name, value in extra.items():
        object.__setattr__(snap, name, value)
    return snap


def by_id(alerts, rule_id: str):
    return [a for a in alerts if a.rule_id == rule_id]


def test_every_new_rule_is_registered() -> None:
    registered = {rule.rule_id for rule in RULES}
    for rule_id in (
        "freshness.fraction_24h",
        "persistence.*",
        "breaker.evaluation_lag",
        "costauth.budget_denials_1h",
        "infra.disk_free",
        "backup.restore_verify_failed",
        "cost.ledger_linked_attempt_fraction_24h",
        "dispatch.ambiguous_intents_sustained",
        "heartbeat.missing",
    ):
        assert rule_id in registered, rule_id


_NEW_RULE_IDS = (
    "freshness.fraction_24h",
    "persistence.pending_batches",
    "persistence.quarantined_batches",
    "breaker.evaluation_lag",
    "costauth.budget_denials_1h",
    "infra.disk_free",
    "backup.restore_verify_failed",
    "cost.ledger_linked_attempt_fraction_24h",
    "dispatch.ambiguous_intents_sustained",
    "heartbeat.missing",
)


def test_a_bare_snapshot_with_none_of_the_new_attributes_fires_none_of_the_new_rules() -> None:
    """Inert until wired: a snapshot that doesn't carry any of these
    gauges yet must not fire — that is the "not yet collected" state
    every other rule in this module treats as absence, never failure.
    (A bare `OpsSnapshot` DOES fire plenty of PRE-EXISTING `*.unavailable`
    rules for its other, unrelated default-`available=False` sections —
    this only asserts none of the NEW B9 rules are among them.)"""
    fired = {a.rule_id for a in evaluate(_snapshot())}
    assert fired.isdisjoint(_NEW_RULE_IDS)


# --------------------------------------------------------------------------
# freshness_fraction_24h < 0.95
# --------------------------------------------------------------------------


def test_freshness_fraction_just_above_threshold_does_not_fire() -> None:
    snap = _snapshot(freshness_fraction_24h=0.96)
    assert by_id(evaluate(snap), "freshness.fraction_24h") == []


def test_freshness_fraction_at_threshold_does_not_fire() -> None:
    snap = _snapshot(freshness_fraction_24h=DEFAULT_THRESHOLDS.freshness_fraction_24h_min)
    assert by_id(evaluate(snap), "freshness.fraction_24h") == []


def test_freshness_fraction_just_below_threshold_fires() -> None:
    snap = _snapshot(freshness_fraction_24h=0.94)
    alerts = by_id(evaluate(snap), "freshness.fraction_24h")
    assert alerts and alerts[0].severity is Severity.HIGH


# --------------------------------------------------------------------------
# queue_oldest_pending_seconds > 3600 — already covered by `_r_queue`
# --------------------------------------------------------------------------


def test_queue_oldest_pending_at_threshold_does_not_fire() -> None:
    snap = _snapshot()
    object.__setattr__(
        snap,
        "queue",
        QueueHealth(
            available=True,
            targets_by_status={"PENDING": 1},
            oldest_pending_target_age_seconds=DEFAULT_THRESHOLDS.target_pending_age_seconds,
        ),
    )
    assert by_id(evaluate(snap), "queue.pending_target_age") == []


def test_queue_oldest_pending_just_above_threshold_fires() -> None:
    snap = _snapshot()
    object.__setattr__(
        snap,
        "queue",
        QueueHealth(
            available=True,
            targets_by_status={"PENDING": 1},
            oldest_pending_target_age_seconds=DEFAULT_THRESHOLDS.target_pending_age_seconds + 1,
        ),
    )
    assert by_id(evaluate(snap), "queue.pending_target_age")


# --------------------------------------------------------------------------
# persistence_pending_batches > 16 or persistence_quarantined_batches > 0
# --------------------------------------------------------------------------


def test_persistence_pending_at_threshold_does_not_fire() -> None:
    snap = _snapshot(persistence_pending_batches=16, persistence_quarantined_batches=0)
    assert by_id(evaluate(snap), "persistence.pending_batches") == []


def test_persistence_pending_just_above_threshold_fires() -> None:
    snap = _snapshot(persistence_pending_batches=17, persistence_quarantined_batches=0)
    alerts = by_id(evaluate(snap), "persistence.pending_batches")
    assert alerts and alerts[0].severity is Severity.HIGH


def test_any_quarantined_batch_fires_critical() -> None:
    snap = _snapshot(persistence_pending_batches=0, persistence_quarantined_batches=1)
    alerts = by_id(evaluate(snap), "persistence.quarantined_batches")
    assert alerts and alerts[0].severity is Severity.CRITICAL


def test_zero_quarantined_batches_does_not_fire() -> None:
    snap = _snapshot(persistence_pending_batches=0, persistence_quarantined_batches=0)
    assert by_id(evaluate(snap), "persistence.quarantined_batches") == []


# --------------------------------------------------------------------------
# breaker_seconds_since_evaluation > 900 (early warning, existing field)
# --------------------------------------------------------------------------


def test_breaker_evaluation_lag_at_threshold_does_not_fire() -> None:
    snap = _snapshot()
    object.__setattr__(
        snap,
        "breaker",
        BreakerHealth(
            available=True,
            state="CLOSED",
            seconds_since_evaluation=DEFAULT_THRESHOLDS.breaker_evaluation_lag_warning_seconds,
        ),
    )
    assert by_id(evaluate(snap), "breaker.evaluation_lag") == []


def test_breaker_evaluation_lag_just_above_threshold_fires() -> None:
    snap = _snapshot()
    object.__setattr__(
        snap,
        "breaker",
        BreakerHealth(
            available=True,
            state="CLOSED",
            seconds_since_evaluation=DEFAULT_THRESHOLDS.breaker_evaluation_lag_warning_seconds
            + 1,
        ),
    )
    alerts = by_id(evaluate(snap), "breaker.evaluation_lag")
    assert alerts and alerts[0].severity is Severity.WARNING


# --------------------------------------------------------------------------
# costauth_denials_1h{reason=BUDGET} > 0
# --------------------------------------------------------------------------


def test_zero_budget_denials_does_not_fire() -> None:
    snap = _snapshot(costauth_denials_1h_by_reason={"BUDGET": 0})
    assert by_id(evaluate(snap), "costauth.budget_denials_1h") == []


def test_one_budget_denial_fires() -> None:
    snap = _snapshot(costauth_denials_1h_by_reason={"BUDGET": 1})
    alerts = by_id(evaluate(snap), "costauth.budget_denials_1h")
    assert alerts and alerts[0].severity is Severity.HIGH


def test_denials_for_another_reason_do_not_fire_the_budget_rule() -> None:
    snap = _snapshot(costauth_denials_1h_by_reason={"RATE_LIMIT": 5})
    assert by_id(evaluate(snap), "costauth.budget_denials_1h") == []


# --------------------------------------------------------------------------
# disk_free_fraction < 0.15 (host and volumes)
# --------------------------------------------------------------------------


def test_disk_free_at_threshold_does_not_fire() -> None:
    snap = _snapshot(
        disk_free_fraction_by_mount={"host": DEFAULT_THRESHOLDS.disk_free_fraction_critical}
    )
    assert by_id(evaluate(snap), "infra.disk_free") == []


def test_disk_free_just_below_threshold_fires_for_the_named_mount() -> None:
    snap = _snapshot(
        disk_free_fraction_by_mount={"host": DEFAULT_THRESHOLDS.disk_free_fraction_critical - 0.01}
    )
    alerts = by_id(evaluate(snap), "infra.disk_free")
    assert alerts and alerts[0].severity is Severity.CRITICAL
    assert alerts[0].subject == "host"


def test_disk_free_checks_every_mount_independently() -> None:
    snap = _snapshot(
        disk_free_fraction_by_mount={
            "host": 0.5,
            "data-volume": DEFAULT_THRESHOLDS.disk_free_fraction_critical - 0.01,
        }
    )
    alerts = by_id(evaluate(snap), "infra.disk_free")
    assert {a.subject for a in alerts} == {"data-volume"}


# --------------------------------------------------------------------------
# restore_verify_failed
# --------------------------------------------------------------------------


def test_restore_verify_not_failed_does_not_fire() -> None:
    snap = _snapshot(restore_verify_failed=False)
    assert by_id(evaluate(snap), "backup.restore_verify_failed") == []


def test_restore_verify_failed_fires_critical() -> None:
    snap = _snapshot(restore_verify_failed=True)
    alerts = by_id(evaluate(snap), "backup.restore_verify_failed")
    assert alerts and alerts[0].severity is Severity.CRITICAL


# --------------------------------------------------------------------------
# ledger_linked_attempt_fraction_24h < 0.95
# --------------------------------------------------------------------------


def test_ledger_linked_fraction_matches_emit_module_threshold() -> None:
    from app_shared.opsmetrics.emit import LEDGER_LINKED_ATTEMPT_FRACTION_MIN

    assert (
        DEFAULT_THRESHOLDS.ledger_linked_attempt_fraction_24h_min
        == LEDGER_LINKED_ATTEMPT_FRACTION_MIN
    )


def test_ledger_linked_fraction_at_threshold_does_not_fire() -> None:
    snap = _snapshot(
        ledger_linked_attempt_fraction_24h=DEFAULT_THRESHOLDS.ledger_linked_attempt_fraction_24h_min
    )
    assert by_id(evaluate(snap), "cost.ledger_linked_attempt_fraction_24h") == []


def test_ledger_linked_fraction_just_below_threshold_fires() -> None:
    snap = _snapshot(
        ledger_linked_attempt_fraction_24h=DEFAULT_THRESHOLDS.ledger_linked_attempt_fraction_24h_min
        - 0.01
    )
    alerts = by_id(evaluate(snap), "cost.ledger_linked_attempt_fraction_24h")
    assert alerts and alerts[0].severity is Severity.WARNING


# --------------------------------------------------------------------------
# dispatch_ambiguous_intents > 0 for 10 min
# --------------------------------------------------------------------------


def test_ambiguous_intents_present_but_not_yet_sustained_does_not_fire() -> None:
    snap = _snapshot(
        dispatch_ambiguous_intents=2,
        dispatch_ambiguous_intents_oldest_seconds=DEFAULT_THRESHOLDS.dispatch_ambiguous_intents_sustained_seconds,
    )
    assert by_id(evaluate(snap), "dispatch.ambiguous_intents_sustained") == []


def test_ambiguous_intents_sustained_just_past_10_minutes_fires() -> None:
    snap = _snapshot(
        dispatch_ambiguous_intents=2,
        dispatch_ambiguous_intents_oldest_seconds=DEFAULT_THRESHOLDS.dispatch_ambiguous_intents_sustained_seconds
        + 1,
    )
    alerts = by_id(evaluate(snap), "dispatch.ambiguous_intents_sustained")
    assert alerts and alerts[0].severity is Severity.HIGH


def test_zero_ambiguous_intents_never_fires_regardless_of_age() -> None:
    snap = _snapshot(dispatch_ambiguous_intents=0, dispatch_ambiguous_intents_oldest_seconds=9_999)
    assert by_id(evaluate(snap), "dispatch.ambiguous_intents_sustained") == []


# --------------------------------------------------------------------------
# heartbeat_missing{service}
# --------------------------------------------------------------------------


def test_no_missing_services_does_not_fire() -> None:
    snap = _snapshot(heartbeat_missing_services=())
    assert by_id(evaluate(snap), "heartbeat.missing") == []


def test_one_missing_service_fires_named_in_the_subject() -> None:
    snap = _snapshot(heartbeat_missing_services=("scheduler",))
    alerts = by_id(evaluate(snap), "heartbeat.missing")
    assert len(alerts) == 1
    assert alerts[0].severity is Severity.HIGH
    assert alerts[0].subject == "scheduler"


def test_every_missing_service_gets_its_own_alert() -> None:
    snap = _snapshot(heartbeat_missing_services=("worker", "scheduler"))
    alerts = by_id(evaluate(snap), "heartbeat.missing")
    assert {a.subject for a in alerts} == {"worker", "scheduler"}
