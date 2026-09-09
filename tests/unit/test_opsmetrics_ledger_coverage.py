"""Tests for EPA A7's two ledger-coverage metrics in `opsmetrics.emit`.

These are pure fraction functions plus a standalone Prometheus renderer
(`app_shared.opsmetrics.emit.render_ledger_coverage_prometheus`) — see
that module's own comment for why they are not folded into
`OpsSnapshot`/`render_prometheus` yet (B9 wires the alert thresholds
these name; a later task composes them into the collector).
"""

from __future__ import annotations

from app_shared.opsmetrics.emit import (
    LEDGER_BYTES_MISSING_FRACTION_MAX,
    LEDGER_LINKED_ATTEMPT_FRACTION_MIN,
    LedgerCoverage,
    ledger_bytes_missing_fraction,
    ledger_linked_attempt_fraction,
    render_ledger_coverage_prometheus,
)


class TestLedgerLinkedAttemptFraction:
    def test_ordinary_fraction(self) -> None:
        assert ledger_linked_attempt_fraction(total_attempts=200, linked_attempts=190) == 0.95

    def test_full_coverage(self) -> None:
        assert ledger_linked_attempt_fraction(total_attempts=50, linked_attempts=50) == 1.0

    def test_zero_attempts_is_no_signal_not_zero(self) -> None:
        assert ledger_linked_attempt_fraction(total_attempts=0, linked_attempts=0) is None


class TestLedgerBytesMissingFraction:
    def test_ordinary_fraction(self) -> None:
        assert ledger_bytes_missing_fraction(total_proxied=100, missing_bytes=3) == 0.03

    def test_zero_proxied_is_no_signal_not_zero(self) -> None:
        assert ledger_bytes_missing_fraction(total_proxied=0, missing_bytes=0) is None


class TestThresholdsAreDocumentedForB9:
    def test_thresholds_match_the_packet(self) -> None:
        assert LEDGER_LINKED_ATTEMPT_FRACTION_MIN == 0.95
        assert LEDGER_BYTES_MISSING_FRACTION_MAX == 0.05


class TestRenderLedgerCoveragePrometheus:
    def test_renders_both_gauges_with_help_and_type(self) -> None:
        coverage = LedgerCoverage(
            linked_attempt_fraction_24h=0.97, bytes_missing_fraction_24h=0.02
        )
        text = render_ledger_coverage_prometheus(coverage)

        assert "# TYPE crawmatic_ledger_linked_attempt_fraction_24h gauge" in text
        assert "crawmatic_ledger_linked_attempt_fraction_24h 0.97" in text
        assert "# TYPE crawmatic_ledger_bytes_missing_fraction_24h gauge" in text
        assert "crawmatic_ledger_bytes_missing_fraction_24h 0.02" in text

    def test_no_signal_omits_both_lines_rather_than_fabricating_zero(self) -> None:
        assert render_ledger_coverage_prometheus(LedgerCoverage()) == ""

    def test_one_available_one_not_renders_only_the_available_one(self) -> None:
        coverage = LedgerCoverage(linked_attempt_fraction_24h=0.9)
        text = render_ledger_coverage_prometheus(coverage)
        assert "crawmatic_ledger_linked_attempt_fraction_24h" in text
        assert "crawmatic_ledger_bytes_missing_fraction_24h" not in text
