"""Unit tests for `scripts/run_gate_d_canary.py` (EPA D1, 2026-08-26).

Four things are worth testing here, and they are exactly the four things that
would silently ruin Run Gate D if they were wrong:

1. **Stratification validity** — a sample that misses a stratum makes a §12.6
   rate meaningless, and a sample that is not reproducible makes a no-go
   repeat run a different experiment.
2. **SLA computation** — the precomputed SLA is the gate value when 10 minutes
   is infeasible, so an arithmetic error here moves the bar the canary is
   judged against.
3. **Evaluation** — every §12.6 condition must be able to FAIL. A check that
   cannot fail is worse than a missing check, because it reads as coverage.
4. **The owner gates** — every live step must hard-refuse without explicit
   acknowledgment AND closed deploy gates. This is the property that lets D1
   be prepared autonomously at all.

Everything is driven from literals: no database, no network, no clock, no
production anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pytest

# `scripts/` has no __init__.py — same sys.path convention as
# tests/unit/test_classify_match_set.py.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_gate_d_canary import (  # noqa: E402
    DEPLOY_GATES,
    FAIL,
    GATE_NAMES,
    LIVE_STEPS,
    PASS,
    PENDING,
    RESTORE_PROVENANCE_BANNER,
    SECTION_12_6_TARGET_SECONDS,
    CanaryFacts,
    DomainRateLimit,
    DomainWorkload,
    OwnerGateRefusal,
    Stratum,
    TargetCandidate,
    build_stratified_sample,
    cmd_enqueue,
    cmd_sign,
    compute_completion_sla,
    default_strata,
    evaluate_canary,
    require_owner_go,
    target_set_hash,
    validate_sample,
    verdict,
)

# --------------------------------------------------------------------------
# Synthetic pool
# --------------------------------------------------------------------------


def _candidate(index: int, domain: str, **overrides) -> TargetCandidate:
    """One synthetic candidate with plausible defaults.

    `match_id` is a stable function of (domain, index) so a test can rebuild
    the same pool twice and compare hashes — which is how determinism is
    tested at all.
    """
    base = dict(
        match_id=f"{domain}-{index:05d}",
        competitor_id=f"competitor-{domain}",
        domain=domain,
        competitor_url=f"https://{domain}/p/{index}",
        classification="ACTIVE",
        variant_identifier=None,
        previously_successful=True,
        previously_failed=False,
        attempts_p95=2,
        used_direct=True,
        used_proxy=False,
        used_browser=False,
        labeled_stech=False,
    )
    base.update(overrides)
    return TargetCandidate(**base)


def synthetic_pool() -> list[TargetCandidate]:
    """A pool shaped like the real one: scarce certified/browser targets, a
    labeled S-Tech corpus, and a long tail of ordinary direct targets."""
    pool: list[TargetCandidate] = []
    # amazon.sa — the only browser+proxy domain, deliberately scarce (19).
    pool += [
        _candidate(
            i,
            "amazon.sa",
            used_browser=True,
            used_proxy=True,
            used_direct=False,
            previously_failed=True,
            attempts_p95=6,
        )
        for i in range(19)
    ]
    # noon.com — scarce, proxy-only (23).
    pool += [
        _candidate(i, "noon.com", used_proxy=True, used_direct=False, previously_failed=True, attempts_p95=6)
        for i in range(23)
    ]
    # stech.ink — 30 labeled (the B4 corpus) + 400 ordinary ACTIVE.
    pool += [
        _candidate(
            i,
            "stech.ink",
            classification="INVALID_IDENTITY",
            labeled_stech=True,
            variant_identifier=f"legacy-{i}",
            used_proxy=True,
            attempts_p95=10,
        )
        for i in range(30)
    ]
    pool += [
        _candidate(
            1000 + i,
            "stech.ink",
            variant_identifier=f"gid-{i}",
            used_proxy=True,
            attempts_p95=10,
        )
        for i in range(400)
    ]
    # The ordinary tail.
    for domain, count in (("pcpalace.com.sa", 490), ("jarir.com", 350), ("extra.com", 348)):
        pool += [_candidate(i, domain, attempts_p95=5) for i in range(count)]
    return pool


# --------------------------------------------------------------------------
# 1. Stratification validity
# --------------------------------------------------------------------------


def test_sample_is_exactly_the_requested_size_and_valid():
    result = build_stratified_sample(synthetic_pool(), size=199)
    assert len(result.targets) == 199
    assert validate_sample(result, size=199) == []
    assert len({t.match_id for t in result.targets}) == 199


def test_every_stratum_meets_its_floor():
    result = build_stratified_sample(synthetic_pool(), size=199)
    for stratum in default_strata():
        assert result.coverage[stratum.name] >= stratum.floor, (
            f"{stratum.name}: {result.coverage[stratum.name]} < {stratum.floor}"
        )


def test_labeled_stech_subset_is_included_whole_not_sampled():
    """The regression corpus for the incident that motivated the gate is
    mandatory — sampling 12 of the 30 would leave the bug reintroducible."""
    result = build_stratified_sample(synthetic_pool(), size=199)
    labeled = [t for t in result.targets if t.labeled_stech]
    assert len(labeled) == 30


def test_selection_is_deterministic_for_a_fixed_seed():
    first = build_stratified_sample(synthetic_pool(), size=199, seed="fixed")
    second = build_stratified_sample(synthetic_pool(), size=199, seed="fixed")
    assert first.target_set_hash == second.target_set_hash
    assert [t.match_id for t in first.targets] == [t.match_id for t in second.targets]


def test_a_different_seed_draws_a_different_sample():
    a = build_stratified_sample(synthetic_pool(), size=199, seed="seed-a")
    b = build_stratified_sample(synthetic_pool(), size=199, seed="seed-b")
    assert a.target_set_hash != b.target_set_hash


def test_target_set_hash_is_order_invariant():
    """The hash must answer 'is this the same SET', so shuffling the list
    cannot change it — otherwise two honest rebuilds disagree."""
    result = build_stratified_sample(synthetic_pool(), size=199)
    forwards = target_set_hash(result.targets)
    backwards = target_set_hash(list(reversed(result.targets)))
    assert forwards == backwards


def test_target_set_hash_ignores_volatile_history_but_not_identity():
    pool = synthetic_pool()
    original = build_stratified_sample(pool, size=199)
    # Same targets, different measured history: still the same target set.
    mutated = [
        TargetCandidate(**{**t.__dict__, "attempts_p95": t.attempts_p95 + 7})
        for t in original.targets
    ]
    assert target_set_hash(mutated) == original.target_set_hash
    # A different URL is a different target.
    relocated = [
        TargetCandidate(**{**t.__dict__, "competitor_url": t.competitor_url + "?x=1"})
        for t in original.targets
    ]
    assert target_set_hash(relocated) != original.target_set_hash


def test_pool_outside_the_authorized_set_is_refused():
    """D1 authorizes ACTIVE-classified matches plus the labeled S-Tech subset
    and nothing else — a widened pool would invalidate every §12.6 rate."""
    pool = synthetic_pool()
    pool.append(_candidate(9999, "extra.com", classification="UNKNOWN"))
    with pytest.raises(ValueError, match="forbids widening the pool"):
        build_stratified_sample(pool, size=199)


def test_a_pool_smaller_than_the_sample_is_refused_not_padded():
    with pytest.raises(ValueError, match="never pad a canary sample"):
        build_stratified_sample(synthetic_pool()[:50], size=199)


def test_short_supply_caps_a_floor_instead_of_failing_the_build():
    """A stratum the pool cannot fill is reported as capped, not raised: a
    sample that refuses to exist teaches the owner nothing."""
    pool = [_candidate(i, "extra.com") for i in range(300)]
    strata = (
        Stratum("impossible", 50, lambda c: c.used_browser, "no browser targets exist"),
        Stratum("everything", 10, lambda c: True, "trivially satisfiable"),
    )
    result = build_stratified_sample(pool, size=199, strata=strata)
    assert result.capped_strata == ("impossible",)
    assert result.coverage["impossible"] == 0
    # validate_sample still flags the empty stratum — capping is recorded, not excused.
    assert any("impossible" in problem for problem in validate_sample(result, size=199, strata=strata))


def test_remainder_fill_keeps_the_domain_mix_broad():
    """Floors pull toward scarce domains; the proportional fill must still let
    the ordinary tail dominate, or the canary measures the wrong fleet."""
    result = build_stratified_sample(synthetic_pool(), size=199)
    mix: dict[str, int] = {}
    for target in result.targets:
        mix[target.domain] = mix.get(target.domain, 0) + 1
    assert len(mix) >= 5
    assert mix["amazon.sa"] + mix["noon.com"] < len(result.targets) / 2


# --------------------------------------------------------------------------
# 2. SLA computation
# --------------------------------------------------------------------------


def test_rate_limit_binds_when_the_site_is_fast():
    """A fast site can saturate its certified ceiling, so the ceiling governs."""
    sla = compute_completion_sla(
        [DomainWorkload("amazon.sa", targets=100, attempts_p95_per_target=1.0, p95_latency_seconds=0.5)],
        per_domain_concurrency=4,
    )
    entry = sla["per_domain"][0]
    assert entry["binding_constraint"] == "rate_limit"
    assert entry["effective_rps"] == pytest.approx(1.667, abs=1e-3)


def test_concurrency_binds_when_the_site_is_slow():
    """noon.com's certified 1.667/s is unreachable at a 30s p95: four slots
    deliver 0.13/s. Promising the certified rate would promise an SLA the
    fleet cannot meet."""
    sla = compute_completion_sla(
        [DomainWorkload("noon.com", targets=18, attempts_p95_per_target=6.0, p95_latency_seconds=30.0)],
        per_domain_concurrency=4,
    )
    entry = sla["per_domain"][0]
    assert entry["binding_constraint"] == "concurrency"
    assert entry["effective_rps"] == pytest.approx(4 / 30.0, abs=1e-4)


def test_uncertified_domains_are_flagged_with_the_assumed_rate():
    sla = compute_completion_sla(
        [DomainWorkload("madeup.example", targets=10, attempts_p95_per_target=2.0, p95_latency_seconds=1.0)]
    )
    assert sla["uncertified_rate_limit_domains"] == ["madeup.example"]
    assert sla["per_domain"][0]["rate_limit_certified"] is False
    assert sla["per_domain"][0]["certified_rps"] == 2.0


def test_ten_minute_target_governs_when_it_is_feasible():
    sla = compute_completion_sla(
        [DomainWorkload("extra.com", targets=20, attempts_p95_per_target=1.0, p95_latency_seconds=0.5)]
    )
    assert sla["ten_minute_target_feasible"] is True
    assert sla["governing_sla_seconds"] == SECTION_12_6_TARGET_SECONDS
    assert sla["governing_source"] == "section-12.6-target"


def test_precomputed_sla_governs_when_ten_minutes_is_infeasible():
    """§12.6's 10 minutes is a target validated against the computation, not a
    promise: when certified limits make it infeasible the computed value is
    the gate."""
    sla = compute_completion_sla(
        [DomainWorkload("noon.com", targets=100, attempts_p95_per_target=6.0, p95_latency_seconds=30.0)],
        per_domain_concurrency=4,
    )
    assert sla["ten_minute_target_feasible"] is False
    assert sla["governing_sla_seconds"] == sla["computed_sla_seconds"]
    assert sla["governing_sla_seconds"] > SECTION_12_6_TARGET_SECONDS
    assert sla["governing_source"] == "precomputed-sla"


def test_domains_are_parallel_not_serial():
    """Two identical domains must not double the SLA — the fleet runs them
    side by side, and summing would inflate the gate value."""
    one = compute_completion_sla(
        [DomainWorkload("a.example", 50, 2.0, 1.0)], per_domain_concurrency=4, fleet_concurrency=1024
    )
    two = compute_completion_sla(
        [DomainWorkload("a.example", 50, 2.0, 1.0), DomainWorkload("b.example", 50, 2.0, 1.0)],
        per_domain_concurrency=4,
        fleet_concurrency=1024,
    )
    assert two["totals"]["critical_path_seconds"] == pytest.approx(
        one["totals"]["critical_path_seconds"]
    )


def test_fleet_concurrency_can_be_the_binding_constraint():
    """With enough domains the fleet, not any one site, is the bottleneck."""
    workloads = [DomainWorkload(f"d{i}.example", 50, 2.0, 1.0) for i in range(40)]
    tight = compute_completion_sla(workloads, fleet_concurrency=4)
    loose = compute_completion_sla(workloads, fleet_concurrency=1024)
    assert tight["computed_sla_seconds"] > loose["computed_sla_seconds"]


def test_custom_rate_limits_are_honoured():
    sla = compute_completion_sla(
        [DomainWorkload("slowsite.example", 10, 1.0, 0.1)],
        rate_limits={"slowsite.example": DomainRateLimit("slowsite.example", 0.1, True, "test")},
    )
    assert sla["per_domain"][0]["effective_rps"] == pytest.approx(0.1)
    assert sla["per_domain"][0]["binding_constraint"] == "rate_limit"


def test_empty_or_invalid_workloads_are_refused():
    with pytest.raises(ValueError, match="empty sample"):
        compute_completion_sla([])
    with pytest.raises(ValueError, match="latency must be positive"):
        compute_completion_sla([DomainWorkload("a.example", 1, 1.0, 0.0)])


# --------------------------------------------------------------------------
# 3. Evaluation against synthetic pass/fail fixtures
# --------------------------------------------------------------------------


def passing_facts(**overrides) -> CanaryFacts:
    """A canary that satisfies every §12.6 condition.

    Written as the baseline so each failure test flips exactly one fact — if a
    check were wired to the wrong field, its test would pass while another
    test unexpectedly failed, which is the point.
    """
    base = dict(
        job_terminal=True,
        elapsed_seconds=900,
        governing_sla_seconds=1729,
        targets_total=199,
        targets_pending=0,
        targets_started=0,
        targets_deferred=0,
        targets_unaccounted=0,
        targets_with_one_terminal_outcome=199,
        successful_observations_labeled_failed=0,
        eligible_active_targets=173,
        fresh_comparable_offers=170,
        per_domain_fresh_rate={"amazon.sa": 1.0, "noon.com": 0.95, "stech.ink": 0.99},
        technical_failures=2,
        confirmed_delisted=4,
        stech_labeled_targets=30,
        stech_false_not_listed=0,
        duplicate_physical_dispatches=0,
        shared_guard_identities=0,
        paid_operations=520,
        operations_missing_authorization=0,
        operations_missing_reservation=0,
        operations_missing_settlement=0,
        operations_missing_allocation=0,
        grants_issued=520,
        grants_with_exactly_one_operation=520,
        grants_still_reserved_after_drain=0,
        budget_counter_total=0.0412,
        ledger_settled_total=0.0412,
        app_bytes=16_094_690,
        provider_bytes=16_200_000,
        proxy_cost_usd=0.041,
        combined_cost_usd=0.069,
        railway_billing_confirmed=True,
        railway_confirmed_combined_cost_usd=0.071,
        canary_attributable_alerts=(),
        deliberate_test_alert_fired=True,
        evidence_bundle_signed=True,
    )
    base.update(overrides)
    return CanaryFacts(**base)


def _status(checks, name: str) -> str:
    return next(check.status for check in checks if check.name == name)


def test_a_fully_passing_canary_is_a_go():
    checks = evaluate_canary(passing_facts())
    assert all(check.status == PASS for check in checks), [
        c.name for c in checks if c.status != PASS
    ]
    assert verdict(checks) == "GO"


@pytest.mark.parametrize(
    ("check_name", "overrides"),
    [
        ("terminal_within_sla", {"job_terminal": False}),
        ("terminal_within_sla", {"elapsed_seconds": 1730}),
        ("zero_unaccounted_targets", {"targets_pending": 1}),
        ("zero_unaccounted_targets", {"targets_started": 1}),
        ("zero_unaccounted_targets", {"targets_deferred": 1}),
        ("zero_unaccounted_targets", {"targets_unaccounted": 1}),
        ("one_explainable_terminal_outcome", {"targets_with_one_terminal_outcome": 198}),
        ("no_success_labeled_failed", {"successful_observations_labeled_failed": 1}),
        ("fresh_comparable_offer_rate", {"fresh_comparable_offers": 160}),
        ("certified_domain_floor", {"per_domain_fresh_rate": {"amazon.sa": 0.89, "noon.com": 1.0}}),
        ("technical_failure_rate", {"technical_failures": 5}),
        ("stech_zero_false_not_listed", {"stech_false_not_listed": 1}),
        ("no_duplicate_physical_dispatch", {"duplicate_physical_dispatches": 1}),
        ("no_shared_guard_identity", {"shared_guard_identities": 1}),
        ("no_unledgered_paid_operations", {"operations_missing_authorization": 1}),
        ("no_unledgered_paid_operations", {"operations_missing_reservation": 1}),
        ("no_unledgered_paid_operations", {"operations_missing_settlement": 1}),
        ("no_unledgered_paid_operations", {"operations_missing_allocation": 1}),
        ("provider_reconciliation_within_2pct", {"provider_bytes": 20_000_000}),
        ("grant_operation_cardinality", {"grants_with_exactly_one_operation": 519}),
        ("no_grant_left_reserved_after_drain", {"grants_still_reserved_after_drain": 1}),
        ("budget_vs_ledger_reconciliation", {"ledger_settled_total": 0.03}),
        ("proxy_cost_ceiling", {"proxy_cost_usd": 0.051}),
        ("combined_cost_ceiling_provisional", {"combined_cost_usd": 0.081}),
        ("combined_cost_railway_confirmation", {"railway_confirmed_combined_cost_usd": 0.09}),
        ("no_canary_attributable_invariant_alert", {"canary_attributable_alerts": ("rls_denied",)}),
        ("deliberate_test_alert_fired", {"deliberate_test_alert_fired": False}),
        ("evidence_bundle_signed", {"evidence_bundle_signed": False}),
    ],
)
def test_each_condition_can_fail_and_makes_the_run_a_no_go(check_name, overrides):
    """Every §12.6 condition must be individually falsifiable. A check that
    cannot fail reads as coverage while providing none."""
    checks = evaluate_canary(passing_facts(**overrides))
    assert _status(checks, check_name) == FAIL
    assert verdict(checks) == "NO-GO"


def test_railway_confirmation_is_pending_not_passing_while_billing_lags():
    """Cost gates split by measurement latency: an unreported Railway window
    is PENDING, and PENDING can never read as GO."""
    checks = evaluate_canary(
        passing_facts(railway_billing_confirmed=None, railway_confirmed_combined_cost_usd=None)
    )
    assert _status(checks, "combined_cost_railway_confirmation") == PENDING
    assert verdict(checks) == "GO-PENDING-CONFIRMATION"


def test_a_single_failure_outweighs_a_pending():
    checks = evaluate_canary(
        passing_facts(railway_billing_confirmed=None, proxy_cost_usd=0.10)
    )
    assert verdict(checks) == "NO-GO"


def test_confirmed_delisted_is_reported_separately_not_counted_as_failure():
    """§12.6 asks for delisted URLs to be reported separately — raising the
    delisted count alone must not move the technical-failure gate."""
    checks = evaluate_canary(passing_facts(confirmed_delisted=40))
    assert _status(checks, "technical_failure_rate") == PASS
    assert "40" in next(c.detail for c in checks if c.name == "technical_failure_rate")


def test_unknown_fact_keys_are_refused():
    with pytest.raises(ValueError, match="unknown fact keys"):
        CanaryFacts.from_mapping({**passing_facts().__dict__, "invented_metric": 1})


def test_missing_required_fact_keys_are_refused():
    payload = dict(passing_facts().__dict__)
    del payload["targets_total"]
    with pytest.raises(ValueError, match="missing required fact keys"):
        CanaryFacts.from_mapping(payload)


def test_from_mapping_round_trips_a_passing_bundle():
    facts = CanaryFacts.from_mapping(dict(passing_facts().__dict__))
    assert verdict(evaluate_canary(facts)) == "GO"


# --------------------------------------------------------------------------
# 4. The owner gates — every live step hard-refuses
# --------------------------------------------------------------------------


@pytest.mark.parametrize("step", sorted(LIVE_STEPS))
def test_every_live_step_refuses_without_any_acknowledgment(step):
    with pytest.raises(OwnerGateRefusal, match="requires explicit owner acknowledgment"):
        require_owner_go(step, owner_go=None, gates_met=GATE_NAMES)


@pytest.mark.parametrize("step", sorted(LIVE_STEPS))
def test_every_live_step_refuses_another_steps_token(step):
    """Tokens are step-specific so authorization for one decision can never be
    copy-pasted into a different one."""
    other = next(token for name, token in LIVE_STEPS.items() if name != step)
    with pytest.raises(OwnerGateRefusal, match="requires explicit owner acknowledgment"):
        require_owner_go(step, owner_go=other, gates_met=GATE_NAMES)


@pytest.mark.parametrize("step", sorted(LIVE_STEPS))
def test_every_live_step_refuses_with_no_gates_closed(step):
    with pytest.raises(OwnerGateRefusal, match="unclosed deploy gates"):
        require_owner_go(step, owner_go=LIVE_STEPS[step], gates_met=())


@pytest.mark.parametrize("missing", GATE_NAMES)
def test_a_single_unclosed_gate_still_refuses(missing):
    remaining = [name for name in GATE_NAMES if name != missing]
    with pytest.raises(OwnerGateRefusal) as excinfo:
        require_owner_go("enqueue", owner_go=LIVE_STEPS["enqueue"], gates_met=remaining)
    assert missing in str(excinfo.value)


@pytest.mark.parametrize("step", sorted(LIVE_STEPS))
def test_full_authorization_opens_the_gate(step):
    require_owner_go(step, owner_go=LIVE_STEPS[step], gates_met=GATE_NAMES)


def test_an_unknown_step_is_refused_rather_than_defaulting_to_allowed():
    """A typo must never fall through to 'no gate configured, therefore fine'."""
    with pytest.raises(OwnerGateRefusal, match="unknown live step"):
        require_owner_go("deploy_manifest", owner_go="anything", gates_met=GATE_NAMES)


def test_sign_writes_a_sha256sum_verifiable_checksum_file(tmp_path):
    """A1's pattern is a plain `sha256sum -c`-verifiable file. `sha256_digest`
    prefixes "sha256:" and would silently produce a file the standard tool
    rejects — this test is the reason the sign path does not use it."""
    (tmp_path / "artifact.json").write_text('{"a":1}\n', encoding="utf-8")
    args = argparse.Namespace(
        out_dir=tmp_path,
        owner_go=LIVE_STEPS["sign"],
        gate_met=[],
        approver="Test Owner",
        decision="GO",
        sign_no_go=False,
    )
    assert cmd_sign(args) == 0

    line = (tmp_path / "SHA256SUMS").read_text(encoding="utf-8").strip()
    digest, _, name = line.partition("  ")
    assert name == "artifact.json"
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    assert digest == hashlib.sha256(b'{"a":1}\n').hexdigest()
    assert "Test Owner" in (tmp_path / "APPROVAL.txt").read_text(encoding="utf-8")


def test_sign_refuses_to_approve_a_recorded_no_go(tmp_path):
    (tmp_path / "evaluation.json").write_text(
        json.dumps({"verdict": "NO-GO", "checks": []}), encoding="utf-8"
    )
    args = argparse.Namespace(
        out_dir=tmp_path,
        owner_go=LIVE_STEPS["sign"],
        gate_met=[],
        approver="Test Owner",
        decision="GO",
        sign_no_go=False,
    )
    assert cmd_sign(args) == 3
    assert not (tmp_path / "SHA256SUMS").exists()
    # Preserving a failed run's evidence is still possible, explicitly.
    assert cmd_sign(argparse.Namespace(**{**vars(args), "sign_no_go": True})) == 0


def test_enqueue_refuses_a_restore_derived_sla_even_when_fully_authorized(tmp_path):
    """A second, independent refusal: full owner authorization must still not
    enqueue the preparation sample, which was drawn from a 24-hour-old dump."""
    (tmp_path / "TARGET_SET_HASH.txt").write_text("sha256:abc\n", encoding="utf-8")
    (tmp_path / "completion_sla.json").write_text(
        json.dumps(
            {
                "provenance_banner": RESTORE_PROVENANCE_BANNER,
                "target_set_hash": "sha256:abc",
                "governing_sla_seconds": 1729,
                "governing_source": "precomputed-sla",
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        out_dir=tmp_path, owner_go=LIVE_STEPS["enqueue"], gate_met=list(GATE_NAMES)
    )
    assert cmd_enqueue(args) == 3

    # The same bundle, rebuilt against production, is accepted.
    payload = json.loads((tmp_path / "completion_sla.json").read_text(encoding="utf-8"))
    payload["provenance_banner"] = "LIVE PRODUCTION 2026-08-27"
    (tmp_path / "completion_sla.json").write_text(json.dumps(payload), encoding="utf-8")
    assert cmd_enqueue(args) == 0


def test_every_deploy_gate_names_a_recorded_blocker():
    """The gates are recorded blockers, not invented checklist items — each
    must cite where it came from and state a checkable fact."""
    for gate in DEPLOY_GATES:
        assert gate.blocker_ref.startswith("2026-")
        assert len(gate.assertion) > 40
    assert {"entitlement-writer", "budget-seeding", "role-ordering"} <= set(GATE_NAMES)
