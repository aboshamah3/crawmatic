"""amazon.sa / noon.com alerts that say what is actually failing (2026-09-29, plan E9.1).

The domain alerts were computed over request_attempts only, all origins,
with fixed thresholds, and said "46% success" without saying why. And the
~940 targets the job deadline failed every night wrote no attempt row at
all (reaper.py), so the worst failure of the night was invisible to them.

* every domain alert carries its per-error-code breakdown;
* link-level outcomes (the FINAL result per target, per domain) are their
  own snapshot section, and deadline-failed targets their own HIGH alert;
* the thresholds come from settings (env), with the old constants as
  defaults -- configurable, not quieter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app_shared.opsmetrics.rules import (
    DEFAULT_THRESHOLDS,
    Severity,
    evaluate,
    thresholds_from_settings,
)
from app_shared.opsmetrics.snapshot import DomainStats, LinkOutcomes, OpsSnapshot

NOW = datetime(2026, 9, 29, 6, tzinfo=UTC)

AMAZON = DomainStats(
    domain="amazon.sa",
    attempts=1_702,
    successes=369,
    distinct_urls=900,
    proxied=1_133,
    failed_paid=882,
    error_codes={"BLOCKED": 1_010, "TIMEOUT": 192, "PRICE_NOT_FOUND": 118, "PROXY_FAILED": 13},
)


def _by_id(alerts, rule_id):  # noqa: ANN001, ANN202
    return [a for a in alerts if a.rule_id == rule_id]


def test_the_domain_success_alert_names_the_failure_codes() -> None:
    (alert,) = _by_id(evaluate(OpsSnapshot(collected_at=NOW, domains_24h=(AMAZON,))),
                      "reliability.domain_success_rate")
    assert alert.severity is Severity.HIGH
    assert alert.observed["error_codes"]["BLOCKED"] == 1_010
    assert list(alert.observed["error_codes"])[0] == "BLOCKED"  # most frequent first


def test_the_wasted_spend_alert_names_the_failure_codes() -> None:
    (alert,) = _by_id(evaluate(OpsSnapshot(collected_at=NOW, domains_24h=(AMAZON,))),
                      "cost.wasted_paid_rate")
    assert alert.observed["error_codes"]["BLOCKED"] == 1_010


def test_deadline_failed_targets_are_their_own_high_alert() -> None:
    snap = OpsSnapshot(
        collected_at=NOW,
        link_outcomes_24h=(
            LinkOutcomes(domain="amazon.sa", completed=2_700, failed=900, skipped=5, deadline_failed=629),
            LinkOutcomes(domain="noon.com", completed=600, failed=300, skipped=0, deadline_failed=297),
            LinkOutcomes(domain="pcpalace.com", completed=400, failed=2, skipped=0, deadline_failed=0),
        ),
    )
    (alert,) = _by_id(evaluate(snap), "jobs.deadline_failed_targets")
    assert alert.severity is Severity.HIGH
    assert alert.observed["deadline_failed"] == 926
    assert alert.observed["by_domain"] == {"amazon.sa": 629, "noon.com": 297}


def test_no_deadline_failures_no_alert() -> None:
    snap = OpsSnapshot(
        collected_at=NOW,
        link_outcomes_24h=(LinkOutcomes(domain="x.com", completed=10, failed=0, skipped=0, deadline_failed=0),),
    )
    assert not _by_id(evaluate(snap), "jobs.deadline_failed_targets")


def test_link_success_rate_is_final_outcome_over_links() -> None:
    lo = LinkOutcomes(domain="amazon.sa", completed=2_700, failed=900, skipped=5, deadline_failed=629)
    assert round(lo.link_success_rate, 4) == round(2_700 / (2_700 + 900), 4)


def test_thresholds_come_from_settings_with_the_constants_as_defaults() -> None:
    assert thresholds_from_settings(SimpleNamespace()) == DEFAULT_THRESHOLDS
    tuned = thresholds_from_settings(
        SimpleNamespace(
            OPS_DOMAIN_SUCCESS_RATE_HIGH=0.6,
            OPS_DOMAIN_SUCCESS_RATE_WARNING=0.85,
            OPS_DOMAIN_SUCCESS_MIN_ATTEMPTS=100,
            OPS_WASTED_PAID_RATE_HIGH=0.5,
            OPS_WASTED_PAID_MIN_ATTEMPTS=300,
        )
    )
    assert tuned.domain_success_rate_high == 0.6
    assert tuned.domain_success_rate_warning == 0.85
    assert tuned.domain_success_min_attempts == 100
    assert tuned.wasted_paid_rate_high == 0.5
    assert tuned.wasted_paid_min_attempts == 300


def test_deadline_failed_counts_never_fetched_target_deadline_code() -> None:
    """E1 (2026-10-07): TARGET_DEADLINE_EXCEEDED with attempt_count = 0 is folded
    into the same `deadline_failed` figure the alert reads."""
    from app_shared.opsmetrics.snapshot import _LINK_OUTCOMES_SQL

    assert "JOB_DEADLINE_EXCEEDED" in _LINK_OUTCOMES_SQL
    assert "TARGET_DEADLINE_EXCEEDED" in _LINK_OUTCOMES_SQL
    # attempt_count = 0 is expressed as "no request_attempts row for the target".
    assert "NOT EXISTS" in _LINK_OUTCOMES_SQL
    assert "ra.match_id = t.match_id" in _LINK_OUTCOMES_SQL
    assert "ra.scrape_job_id = t.scrape_job_id" in _LINK_OUTCOMES_SQL


def test_deadline_failed_alert_message_names_the_per_target_code() -> None:
    snap = OpsSnapshot(
        collected_at=NOW,
        link_outcomes_24h=(
            LinkOutcomes(domain="noon.com", completed=0, failed=40, skipped=0, deadline_failed=40),
        ),
    )
    (alert,) = _by_id(evaluate(snap), "jobs.deadline_failed_targets")
    assert "attempt_count = 0" in alert.message
    assert alert.observed["deadline_failed"] == 40
