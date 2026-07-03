"""Deterministic finalization + stall-window unit tests (SPEC-08 T041, US3, FR-019/020, SC-007).

`app_shared.jobs.lifecycle.resolve_finalized_status` — pure, no
DB/Redis/network. Per `contracts/lifecycle-counters.md`: the single
ordered, failure-centric rule — `total == 0` -> COMPLETED; `failure == 0`
-> COMPLETED (covers all-success, success+skipped, and skipped-only,
since skips are non-fatal); `failure > 0` and `success > 0` ->
PARTIAL_FAILED; `failure > 0` and `success == 0` -> FAILED.

`stall_window` — stable within one `timeout_seconds`-wide window,
increments across windows.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app_shared.enums import ScrapeJobStatus
from app_shared.jobs.lifecycle import resolve_finalized_status, stall_window


def test_zero_targets_resolves_completed() -> None:
    assert resolve_finalized_status(success=0, failure=0, skipped=0, total=0) == (
        ScrapeJobStatus.COMPLETED
    )


def test_all_success_resolves_completed() -> None:
    assert resolve_finalized_status(success=5, failure=0, skipped=0, total=5) == (
        ScrapeJobStatus.COMPLETED
    )


def test_success_and_skipped_no_failures_resolves_completed() -> None:
    assert resolve_finalized_status(success=3, failure=0, skipped=2, total=5) == (
        ScrapeJobStatus.COMPLETED
    )


def test_skipped_only_no_failures_resolves_completed() -> None:
    """Skips are non-fatal (analyze A1 remediation) — a job whose every
    target resolved to SKIPPED (and nothing failed) still finalizes
    COMPLETED, never PARTIAL_FAILED/FAILED."""
    assert resolve_finalized_status(success=0, failure=0, skipped=5, total=5) == (
        ScrapeJobStatus.COMPLETED
    )


def test_mixed_success_and_failure_resolves_partial_failed() -> None:
    assert resolve_finalized_status(success=3, failure=2, skipped=0, total=5) == (
        ScrapeJobStatus.PARTIAL_FAILED
    )


def test_failure_and_skipped_no_success_resolves_failed() -> None:
    assert resolve_finalized_status(success=0, failure=2, skipped=3, total=5) == (
        ScrapeJobStatus.FAILED
    )


def test_failure_only_resolves_failed() -> None:
    assert resolve_finalized_status(success=0, failure=5, skipped=0, total=5) == (
        ScrapeJobStatus.FAILED
    )


def test_failure_and_success_and_skipped_resolves_partial_failed() -> None:
    """At least one real failure alongside at least one success ->
    PARTIAL_FAILED, regardless of any skips mixed in."""
    assert resolve_finalized_status(success=1, failure=1, skipped=1, total=3) == (
        ScrapeJobStatus.PARTIAL_FAILED
    )


# --- stall_window ------------------------------------------------------------


def test_stall_window_stable_within_one_window() -> None:
    timeout = 900
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later_same_window = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)

    assert stall_window(base, timeout) == stall_window(later_same_window, timeout)


def test_stall_window_increments_across_windows() -> None:
    timeout = 900
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    next_window = datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)

    assert stall_window(next_window, timeout) == stall_window(base, timeout) + 1


def test_stall_window_matches_floor_epoch_over_timeout() -> None:
    timeout = 900
    now = datetime(2026, 7, 3, 12, 34, 56, tzinfo=timezone.utc)

    assert stall_window(now, timeout) == int(now.timestamp() // timeout)
