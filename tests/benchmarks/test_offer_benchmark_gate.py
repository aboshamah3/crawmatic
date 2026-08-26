"""Release gate for offer data (Task W3.3, READY-012, plan §15.3).

Pure/offline — no DB, no network, no ``.env`` (same discipline as
``tests/unit/strategy/test_candidate_ranking.py``). Two independent
things are tested here:

1. **Gate mechanics** (``TestGateThresholds``, ``TestEvidenceFreshness``)
   — :func:`~scripts.run_offer_benchmark.decide_repricing_gate` and
   :func:`~scripts.run_offer_benchmark.check_evidence_freshness` against
   hand-built inputs at each boundary. These must always be green: they
   prove the gate LOGIC is correct, independent of today's corpus score.

2. **The real corpus** (``TestCorpusGate``) — runs the actual
   ``tests/benchmarks/offer_truth/`` corpus through
   ``scripts.run_offer_benchmark.run_benchmark`` and asserts the
   **monitoring**-tier bar, which this system should already clear.

This file deliberately does **not** assert
``decision.allowed_for_auto_reprice is True`` against the real corpus.
Per the task: "The EXACT thresholds are an OWNER decision (§15.3)" —
and per ``tests/benchmarks/offer_truth/CORPUS.md``'s documented coverage
gaps (old_price/seller/shipping/coupon/landed_total are 0%-or-near-0%
recall almost everywhere today), the auto-reprice bar is not expected to
pass yet. That is a correct, honest signal this test surfaces (printed,
not silently hidden) rather than a threshold this suite should fight to
force green — hard-asserting it True would defeat the gate's entire
purpose, and hard-asserting it False would break the moment coverage
genuinely improves. ``test_auto_reprice_gate_is_a_real_computed_decision``
asserts only that the decision is well-formed and consistent with the
report it was computed from.

CI hook-in (not wired here — Task W3.3 explicitly does not touch
``.github/``, that lane belongs to another task): this module is
pytest-collectible as-is (``uv run pytest tests/benchmarks -q``), and
``scripts/run_offer_benchmark.py`` is directly CI-invocable
(``uv run python scripts/run_offer_benchmark.py --json-out report.json``,
exit 0 unless the scorer itself errored). A CI release-gate step should
run both: this test file to enforce the monitoring bar as a hard check,
and the script to publish the scored report (and, once §15.3 is
ratified, to fail the pipeline on ``allowed_for_auto_reprice`` for any
change that flips the flag from a prior green run).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_offer_benchmark import (  # noqa: E402
    BenchmarkReport,
    CaseResult,
    FieldTally,
    GateThresholds,
    check_evidence_freshness,
    decide_repricing_gate,
    run_benchmark,
)
from tests.benchmarks.offer_truth.schema import SCORED_FIELDS  # noqa: E402


def _fake_report(
    *,
    financial_correct: int,
    financial_total: int,
    unresolved_ambiguity: int = 0,
) -> BenchmarkReport:
    """A minimal, hand-built report hitting an exact financial-agreement
    ratio and ambiguity count — independent of the real corpus, so gate
    boundary tests never drift when the corpus changes."""
    tallies = {name: FieldTally() for name in SCORED_FIELDS}
    # financial_agreement_rate() pools current_price/old_price/currency/
    # landed_total — split the requested correct/total evenly across
    # current_price (correct) and old_price (incorrect, i.e. "total but
    # not correct") so the ratio comes out exactly as requested.
    tallies["current_price"].correct = financial_correct
    tallies["old_price"].incorrect = financial_total - financial_correct
    results = [
        CaseResult(
            case_id=f"silent-conflict-{i}",
            domain="amazon",
            category="marketplace",
            synthetic=True,
            outcome_correct=False,
            silently_wrong_conflict=True,
            actual=None,  # not read by the gate
        )
        for i in range(unresolved_ambiguity)
    ]
    return BenchmarkReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        policy_version="test",
        field_tallies=tallies,
        case_results=results,
        domains=("amazon",),
    )


class TestGateThresholds:
    """Boundary tests for decide_repricing_gate — always green, corpus-independent."""

    def test_below_monitoring_bar_blocks_both_tiers(self) -> None:
        report = _fake_report(financial_correct=50, financial_total=100)  # 50%
        decision = decide_repricing_gate(
            report, rollback_wired=True, human_approval_wired=True, evidence_fresh=True
        )
        assert decision.allowed_for_monitoring is False
        assert decision.allowed_for_auto_reprice is False

    def test_between_monitoring_and_auto_reprice_bar(self) -> None:
        report = _fake_report(financial_correct=95, financial_total=100)  # 95%
        thresholds = GateThresholds()
        decision = decide_repricing_gate(
            report, thresholds, rollback_wired=True, human_approval_wired=True, evidence_fresh=True
        )
        assert 95 / 100 >= thresholds.monitoring_min_financial_agreement
        assert 95 / 100 < thresholds.auto_reprice_min_financial_agreement
        assert decision.allowed_for_monitoring is True
        assert decision.allowed_for_auto_reprice is False
        assert any("auto-reprice gate" in r for r in decision.reasons)

    def test_above_auto_reprice_bar_with_all_policies_wired_passes_both(self) -> None:
        report = _fake_report(financial_correct=999, financial_total=1000, unresolved_ambiguity=0)  # 99.9%
        decision = decide_repricing_gate(
            report, rollback_wired=True, human_approval_wired=True, evidence_fresh=True
        )
        assert decision.allowed_for_monitoring is True
        assert decision.allowed_for_auto_reprice is True
        assert "all gates satisfied" in decision.reasons

    def test_high_agreement_but_unresolved_ambiguity_blocks_auto_reprice_only(self) -> None:
        report = _fake_report(financial_correct=999, financial_total=1000, unresolved_ambiguity=1)
        decision = decide_repricing_gate(
            report, rollback_wired=True, human_approval_wired=True, evidence_fresh=True
        )
        assert decision.allowed_for_monitoring is True
        assert decision.allowed_for_auto_reprice is False
        assert decision.unresolved_ambiguity_count == 1
        assert any("unresolved" in r for r in decision.reasons)

    def test_high_agreement_but_no_rollback_policy_blocks_auto_reprice_only(self) -> None:
        report = _fake_report(financial_correct=999, financial_total=1000)
        decision = decide_repricing_gate(
            report, rollback_wired=False, human_approval_wired=True, evidence_fresh=True
        )
        assert decision.allowed_for_monitoring is True
        assert decision.allowed_for_auto_reprice is False
        assert any("rollback" in r for r in decision.reasons)

    def test_high_agreement_but_no_human_approval_policy_blocks_auto_reprice_only(self) -> None:
        report = _fake_report(financial_correct=999, financial_total=1000)
        decision = decide_repricing_gate(
            report, rollback_wired=True, human_approval_wired=False, evidence_fresh=True
        )
        assert decision.allowed_for_monitoring is True
        assert decision.allowed_for_auto_reprice is False
        assert any("human-approval" in r for r in decision.reasons)

    def test_high_agreement_but_stale_evidence_blocks_auto_reprice_only(self) -> None:
        report = _fake_report(financial_correct=999, financial_total=1000)
        decision = decide_repricing_gate(
            report, rollback_wired=True, human_approval_wired=True, evidence_fresh=False
        )
        assert decision.allowed_for_monitoring is True
        assert decision.allowed_for_auto_reprice is False
        assert any("not fresh" in r for r in decision.reasons)

    def test_no_financial_claims_at_all_is_not_silently_passing(self) -> None:
        """An empty/degenerate report (no financial-field claims anywhere)
        must not pass either gate by vacuous division — `financial_agreement_rate()`
        returns ``None`` in that case, and the gate must treat ``None`` as
        "not proven", never as 100%."""
        report = _fake_report(financial_correct=0, financial_total=0)
        decision = decide_repricing_gate(
            report, rollback_wired=True, human_approval_wired=True, evidence_fresh=True
        )
        assert decision.financial_agreement_rate is None
        assert decision.allowed_for_monitoring is False
        assert decision.allowed_for_auto_reprice is False

    def test_thresholds_are_configurable_not_hardcoded(self) -> None:
        """§15.3 is PENDING OWNER REVIEW — the owner must be able to swap
        in different numbers without touching decide_repricing_gate itself."""
        report = _fake_report(financial_correct=80, financial_total=100)  # 80%
        lenient = GateThresholds(monitoring_min_financial_agreement=0.5, auto_reprice_min_financial_agreement=0.75)
        decision = decide_repricing_gate(
            report, lenient, rollback_wired=True, human_approval_wired=True, evidence_fresh=True
        )
        assert decision.allowed_for_monitoring is True
        assert decision.allowed_for_auto_reprice is True


class TestEvidenceFreshness:
    def test_fresh_evidence_within_bound_passes(self) -> None:
        now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        observed = now - timedelta(hours=1)
        assert check_evidence_freshness(observed, max_age_hours=24.0, now=now) is True

    def test_stale_evidence_beyond_bound_fails(self) -> None:
        now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        observed = now - timedelta(hours=48)
        assert check_evidence_freshness(observed, max_age_hours=24.0, now=now) is False

    def test_exactly_at_bound_passes(self) -> None:
        now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        observed = now - timedelta(hours=24)
        assert check_evidence_freshness(observed, max_age_hours=24.0, now=now) is True

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            check_evidence_freshness(datetime(2026, 8, 26, 12, 0), max_age_hours=24.0)


class TestCorpusGate:
    """The real ``tests/benchmarks/offer_truth/`` corpus through the real scorer."""

    @pytest.fixture(scope="class")
    def report(self) -> BenchmarkReport:
        return run_benchmark()

    def test_corpus_loads_and_scores_without_scorer_errors(self, report: BenchmarkReport) -> None:
        summary = report.summary()
        errored = [r for r in report.case_results if r.error]
        assert not errored, f"scorer errors: {[(r.case_id, r.error) for r in errored]}"
        assert summary["total_cases"] >= 60, "corpus must stay >= 20 cases/domain across 3 domains"

    def test_every_domain_has_at_least_20_cases(self, report: BenchmarkReport) -> None:
        counts: dict[str, int] = {}
        for r in report.case_results:
            counts[r.domain] = counts.get(r.domain, 0) + 1
        for domain in ("amazon", "noon", "stech"):
            assert counts.get(domain, 0) >= 20, f"{domain}: only {counts.get(domain, 0)} cases"

    def test_monitoring_gate_passes_on_the_real_corpus(self, report: BenchmarkReport) -> None:
        """The lower, display-only bar. This is a real requirement: if the
        system cannot even clear the monitoring bar against its own
        labeled corpus, monitoring displays should not be trusted either."""
        decision = decide_repricing_gate(
            report, rollback_wired=True, human_approval_wired=True, evidence_fresh=True
        )
        assert decision.allowed_for_monitoring is True, (
            f"monitoring gate failed against the real corpus: {decision.reasons} "
            f"(financial_field_agreement_rate={decision.financial_agreement_rate})"
        )

    def test_auto_reprice_gate_is_a_real_computed_decision(self, report: BenchmarkReport) -> None:
        """Not a pass/fail assertion on the auto-reprice tier itself (see
        module docstring) — just proves the decision is well-formed and
        traceable back to the report, so a human reading CI output gets a
        real number and real reasons, never a silently-skipped check."""
        decision = decide_repricing_gate(
            report, rollback_wired=True, human_approval_wired=True, evidence_fresh=True
        )
        assert isinstance(decision.allowed_for_auto_reprice, bool)
        assert decision.reasons, "gate must always explain itself"
        assert decision.unresolved_ambiguity_count == report.unresolved_ambiguity_count()
        assert decision.financial_agreement_rate == report.financial_agreement_rate()
        print(
            f"\n[W3.3 gate, PENDING OWNER REVIEW §15.3] auto_reprice="
            f"{decision.allowed_for_auto_reprice} agreement="
            f"{decision.financial_agreement_rate} ambiguity="
            f"{decision.unresolved_ambiguity_count} reasons={decision.reasons}"
        )

    def test_missing_deployment_policies_always_block_auto_reprice_on_real_corpus(
        self, report: BenchmarkReport
    ) -> None:
        """Even a hypothetically perfect corpus score must not slip past
        the rollback/human-approval/freshness requirements — this exercises
        that against the REAL report's actual agreement rate, whatever it
        is, by asserting the deployment-policy reasons specifically."""
        decision = decide_repricing_gate(
            report, rollback_wired=False, human_approval_wired=False, evidence_fresh=False
        )
        assert decision.allowed_for_auto_reprice is False
        joined = " ".join(decision.reasons)
        assert "rollback" in joined
        assert "human-approval" in joined
        assert "not fresh" in joined
