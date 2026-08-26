"""Optimizer hysteresis: minimum evidence, anti-flap band, rollback.

EPA W5.5-L2 Item B. Covers `app_shared.strategy.hysteresis` (the pure
evaluators and the capability-gated durable store) and its wiring into
`app_shared.strategy.flush.flush_profile`.

The three requirements each have a named test:

1. **no switch below minimum evidence** —
   `test_below_min_evidence_holds_current_method`,
   `test_flush_holds_the_switch_when_evidence_is_thin`.
2. **no flapping under alternating outcomes** —
   `test_alternating_outcomes_do_not_flap_*` (the wider return band).
3. **degradation triggers rollback exactly once** —
   `test_degradation_rolls_back_exactly_once`,
   `test_flush_reverts_the_profile_on_a_degraded_switch`.

Plus the negative space that matters just as much: a first-ever
assignment and a same-method re-affirmation must NOT be held (holding the
latter would re-open the fqtoners.com `DEGRADED` dead-end that
`tests/unit/test_promotion_degraded_repromotion.py` exists to keep
closed).

Pure / DB-independent: the `flush_profile` half uses the same
monkeypatched-seam style as `test_promotion_degraded_repromotion.py`, and
the durable-store half asserts the rendered SQL rather than executing it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app_shared.enums import AccessMethod, ExtractionMethod, MethodType, StrategyStatus
from app_shared.models.strategy import DomainStrategyProfile
from app_shared.strategy import flush as flush_mod
from app_shared.strategy import hysteresis
from app_shared.strategy.hysteresis import (
    RollbackEvidence,
    SwitchEvidence,
    SwitchThresholds,
    evaluate_rollback,
    evaluate_switch,
)
from app_shared.strategy.promotion import PromotionDecision
from app_shared.strategy.rediscovery import RecentSignals

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)

#: The shipped defaults, restated so a change to `config.py` that
#: loosened them would surface here as a failing expectation rather than
#: quietly re-tuning every test below.
_THRESHOLDS = SwitchThresholds(
    min_evidence_samples=6,
    min_evidence_window_seconds=1800,
    revert_evidence_multiplier=2.0,
    rollback_min_attempts=10,
    rollback_degradation_margin=Decimal("0.20"),
)

_WIDE_WINDOW = 7200.0  # 2h — comfortably clears the 30-min window bar


def test_shipped_defaults_match_the_thresholds_these_tests_assume() -> None:
    from app_shared.config import Settings

    fields = Settings.model_fields
    assert fields["STRATEGY_SWITCH_HYSTERESIS_ENABLED"].default is True
    assert fields["STRATEGY_SWITCH_MIN_EVIDENCE_SAMPLES"].default == 6
    assert fields["STRATEGY_SWITCH_MIN_EVIDENCE_WINDOW_SECONDS"].default == 1800
    assert fields["STRATEGY_SWITCH_REVERT_EVIDENCE_MULTIPLIER"].default == 2.0
    assert fields["STRATEGY_SWITCH_ROLLBACK_MIN_ATTEMPTS"].default == 10
    assert fields["STRATEGY_SWITCH_ROLLBACK_DEGRADATION_MARGIN"].default == 0.20


# ---------------------------------------------------------------------------
# 1. Minimum evidence
# ---------------------------------------------------------------------------


def test_no_incumbent_is_not_a_switch() -> None:
    """A first-ever assignment must pass straight through -- US1 AS1
    (3 qualifying successes over 3 distinct URLs) is untouched."""
    verdict = evaluate_switch(
        SwitchEvidence(
            incumbent_method=None,
            candidate_method=AccessMethod.DIRECT_HTTP.value,
            sample_count=3,
            window_seconds=1.0,
        ),
        _THRESHOLDS,
    )
    assert verdict.allow is True
    assert "first assignment" in verdict.reason


def test_same_method_reaffirmation_is_never_held() -> None:
    """Holding this would re-open the fqtoners.com DEGRADED dead-end:
    a profile whose best method on re-validation is the one it already
    has must still be able to promote back to ACTIVE."""
    verdict = evaluate_switch(
        SwitchEvidence(
            incumbent_method=AccessMethod.DIRECT_HTTP.value,
            candidate_method=AccessMethod.DIRECT_HTTP.value,
            sample_count=3,
            window_seconds=1.0,
        ),
        _THRESHOLDS,
    )
    assert verdict.allow is True
    assert "re-affirmation" in verdict.reason


def test_below_min_evidence_holds_current_method() -> None:
    """THE minimum-evidence requirement: the exact evidence that installs
    a FIRST method (3 samples) must not be enough to REPLACE one."""
    verdict = evaluate_switch(
        SwitchEvidence(
            incumbent_method=AccessMethod.DIRECT_HTTP.value,
            candidate_method=AccessMethod.PROXY_HTTP.value,
            sample_count=3,
            window_seconds=_WIDE_WINDOW,
        ),
        _THRESHOLDS,
    )
    assert verdict.allow is False
    assert verdict.required_samples == 6
    assert "sample_count=3 < required=6" in verdict.reason


def test_at_min_evidence_over_a_long_enough_window_allows_the_switch() -> None:
    verdict = evaluate_switch(
        SwitchEvidence(
            incumbent_method=AccessMethod.DIRECT_HTTP.value,
            candidate_method=AccessMethod.PROXY_HTTP.value,
            sample_count=6,
            window_seconds=_WIDE_WINDOW,
        ),
        _THRESHOLDS,
    )
    assert verdict.allow is True


def test_enough_samples_in_too_short_a_window_still_holds() -> None:
    """The window is the second half of "minimum evidence": a burst of
    successes inside one flush interval is not a track record."""
    verdict = evaluate_switch(
        SwitchEvidence(
            incumbent_method=AccessMethod.DIRECT_HTTP.value,
            candidate_method=AccessMethod.PROXY_HTTP.value,
            sample_count=99,
            window_seconds=60.0,
        ),
        _THRESHOLDS,
    )
    assert verdict.allow is False
    assert "min_evidence_window_seconds" in verdict.reason


def test_unmeasurable_window_skips_the_window_check_only() -> None:
    """`None` means "this caller cannot measure the window", which is not
    evidence of a too-short one -- the SAMPLE bar still applies."""
    allowed = evaluate_switch(
        SwitchEvidence(
            incumbent_method=AccessMethod.DIRECT_HTTP.value,
            candidate_method=AccessMethod.PROXY_HTTP.value,
            sample_count=6,
            window_seconds=None,
        ),
        _THRESHOLDS,
    )
    held = evaluate_switch(
        SwitchEvidence(
            incumbent_method=AccessMethod.DIRECT_HTTP.value,
            candidate_method=AccessMethod.PROXY_HTTP.value,
            sample_count=5,
            window_seconds=None,
        ),
        _THRESHOLDS,
    )
    assert allowed.allow is True
    assert held.allow is False


# ---------------------------------------------------------------------------
# 2. Hysteresis band: no flapping
# ---------------------------------------------------------------------------


def test_returning_to_a_rolled_back_method_needs_the_wider_band() -> None:
    """Asymmetry IS the anti-flap mechanism: leaving A cost 6 samples, so
    returning to A must cost more than 6, or the two methods can trade
    the preference back and forth forever."""
    evidence = SwitchEvidence(
        incumbent_method=AccessMethod.PROXY_HTTP.value,
        candidate_method=AccessMethod.DIRECT_HTTP.value,
        sample_count=6,
        window_seconds=_WIDE_WINDOW,
        candidate_was_rolled_back=True,
    )
    verdict = evaluate_switch(evidence, _THRESHOLDS)
    assert verdict.allow is False
    assert verdict.required_samples == 12
    assert "widened revert band" in verdict.reason


def test_the_wider_band_is_clearable_with_enough_evidence() -> None:
    """Hysteresis must not be a permanent ban -- a method that genuinely
    recovers can still win, it just has to prove more."""
    verdict = evaluate_switch(
        SwitchEvidence(
            incumbent_method=AccessMethod.PROXY_HTTP.value,
            candidate_method=AccessMethod.DIRECT_HTTP.value,
            sample_count=12,
            window_seconds=_WIDE_WINDOW,
            candidate_was_rolled_back=True,
        ),
        _THRESHOLDS,
    )
    assert verdict.allow is True


def test_alternating_outcomes_do_not_flap_between_two_methods() -> None:
    """The flapping scenario end to end, driven purely by the evaluator.

    Two methods alternately accumulate exactly the base bar's worth of
    evidence. Without hysteresis every alternation would switch. With it,
    the first switch happens, and the return -- which now faces the
    doubled band -- does not, so the pair converges on one method instead
    of oscillating.
    """
    a = AccessMethod.DIRECT_HTTP.value
    b = AccessMethod.PROXY_HTTP.value

    preferred = a
    rolled_back = {a}  # A was switched away from and rolled back before
    switches = 0

    for round_index in range(6):
        candidate = b if preferred == a else a
        verdict = evaluate_switch(
            SwitchEvidence(
                incumbent_method=preferred,
                candidate_method=candidate,
                sample_count=6,  # exactly the base bar, every round
                window_seconds=_WIDE_WINDOW,
                candidate_was_rolled_back=candidate in rolled_back,
            ),
            _THRESHOLDS,
        )
        if verdict.allow:
            preferred = candidate
            switches += 1

    assert switches == 1, "alternating equal evidence must settle, not oscillate"
    assert preferred == b


# ---------------------------------------------------------------------------
# 3. Rollback
# ---------------------------------------------------------------------------


def _rollback_evidence(**overrides) -> RollbackEvidence:
    base = dict(
        from_method=AccessMethod.DIRECT_HTTP.value,
        to_method=AccessMethod.PROXY_HTTP.value,
        baseline_success_rate=Decimal("0.9000"),
        post_switch_attempts=20,
        post_switch_successes=4,  # 0.20 — far below 0.90 - 0.20
        already_rolled_back=False,
    )
    base.update(overrides)
    return RollbackEvidence(**base)


def test_clear_degradation_triggers_rollback() -> None:
    verdict = evaluate_rollback(_rollback_evidence(), _THRESHOLDS)
    assert verdict.rollback is True
    assert verdict.post_switch_success_rate == Decimal("0.2")


def test_rollback_needs_a_minimum_number_of_post_switch_attempts() -> None:
    """Never revert on one sample -- and never on the LIFETIME counter
    either: only attempts recorded strictly after the switch count."""
    verdict = evaluate_rollback(
        _rollback_evidence(post_switch_attempts=3, post_switch_successes=0),
        _THRESHOLDS,
    )
    assert verdict.rollback is False
    assert "rollback_min_attempts" in verdict.reason


def test_a_small_dip_inside_the_margin_is_not_a_rollback() -> None:
    """A margin, not equality: ordinary noise must not revert a switch."""
    verdict = evaluate_rollback(
        _rollback_evidence(post_switch_attempts=20, post_switch_successes=15),  # 0.75
        _THRESHOLDS,
    )
    assert verdict.rollback is False
    assert "post_switch_success_rate" in verdict.reason


def test_an_improvement_is_never_a_rollback() -> None:
    verdict = evaluate_rollback(
        _rollback_evidence(post_switch_attempts=20, post_switch_successes=20),
        _THRESHOLDS,
    )
    assert verdict.rollback is False


def test_missing_baseline_is_not_a_rollback() -> None:
    """No baseline means the switch was never measured against anything,
    so 'degraded' is unprovable -- hold, do not thrash."""
    verdict = evaluate_rollback(
        _rollback_evidence(baseline_success_rate=None), _THRESHOLDS
    )
    assert verdict.rollback is False


def test_degradation_rolls_back_exactly_once() -> None:
    """Half of the exactly-once guarantee: an already-stamped switch is
    never re-rolled-back, however bad it looks. (The other half is the
    `WHERE rolled_back_at IS NULL` on the stamp itself, below.)"""
    first = evaluate_rollback(_rollback_evidence(), _THRESHOLDS)
    second = evaluate_rollback(
        _rollback_evidence(already_rolled_back=True), _THRESHOLDS
    )
    assert first.rollback is True
    assert second.rollback is False
    assert "already rolled back once" in second.reason


# ---------------------------------------------------------------------------
# Durable store: capability gate + statement shapes
# ---------------------------------------------------------------------------


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


class _NoExecuteSession:
    """A session double with no `.execute` -- the shape several existing
    flush unit tests use."""


class _RaisingSession:
    def execute(self, _stmt):
        raise RuntimeError("relation does not exist")


def test_capability_probe_uses_to_regclass_on_the_switch_table() -> None:
    stmt = hysteresis._exists_stmt()
    assert "to_regclass" in _compiled(stmt)
    assert stmt.compile().params["qualified_name"] == "public.strategy_method_switches"


def test_store_is_unavailable_without_execute_or_on_error() -> None:
    """Fails CLOSED to "no durable history": the guards that need it go
    inert, and a flush is never failed by a telemetry concern."""
    assert hysteresis.switch_store_available(_NoExecuteSession()) is False
    assert hysteresis.switch_store_available(_RaisingSession()) is False


def test_store_absent_disables_the_band_and_the_rollback_lookup() -> None:
    session = _NoExecuteSession()
    assert (
        hysteresis.candidate_was_rolled_back(
            session, uuid.uuid4(), "ACCESS", AccessMethod.DIRECT_HTTP.value
        )
        is False
    )
    assert hysteresis.latest_open_switch(session, uuid.uuid4(), "ACCESS") is None


def test_capability_answer_is_memoised_per_session() -> None:
    """A flush cycle asks this once per candidate method per profile; a
    dozen extra catalog round-trips per profile per 60s tick is not an
    acceptable cost for an answer that cannot change inside one session."""

    class _CountingSession:
        def __init__(self) -> None:
            self.probes = 0

        def execute(self, _stmt):
            self.probes += 1
            return SimpleNamespace(scalar=lambda: True)

    session = _CountingSession()
    assert hysteresis.switch_store_available(session) is True
    assert hysteresis.switch_store_available(session) is True
    assert hysteresis.switch_store_available(session) is True
    assert session.probes == 1


def test_rollback_stamp_is_guarded_by_rolled_back_at_is_null() -> None:
    """The other half of exactly-once: two workers flushing the same
    profile both decide to roll back, but only one UPDATE matches a row."""
    sql = _compiled(hysteresis._mark_rolled_back_stmt(uuid.uuid4(), "worse", _NOW))
    assert "rolled_back_at IS NULL" in sql


def test_latest_open_switch_ignores_already_rolled_back_rows() -> None:
    sql = _compiled(hysteresis._latest_open_stmt(uuid.uuid4(), "ACCESS"))
    assert "rolled_back_at IS NULL" in sql
    assert "ORDER BY switched_at DESC" in sql


def test_mark_rolled_back_reports_whether_this_call_claimed_it() -> None:
    class _RowcountSession:
        def __init__(self, rowcount: int) -> None:
            self.rowcount = rowcount

        def execute(self, _stmt):
            return SimpleNamespace(rowcount=self.rowcount)

    assert (
        hysteresis.mark_rolled_back(
            _RowcountSession(1), uuid.uuid4(), reason="worse", now=_NOW
        )
        is True
    )
    assert (
        hysteresis.mark_rolled_back(
            _RowcountSession(0), uuid.uuid4(), reason="worse", now=_NOW
        )
        is False
    )


# ---------------------------------------------------------------------------
# flush_profile wiring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeDrained:
    attempt: int
    success: int
    failure: int
    rt_ms_sum: int
    conf_sum: int
    qualifying_success: int
    distinct_urls: int
    operational_failure: int = 0


class _FakeRedis:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> None:
        self.deleted.append(key)

    def srem(self, key: str, member: str) -> None:  # noqa: D401
        pass


class _FlushSession:
    """`flush_profile` only calls `.get(...)` here; every collaborator
    that would touch SQL is monkeypatched (the
    `test_promotion_degraded_repromotion.py` seam style)."""

    def __init__(self, profile: DomainStrategyProfile) -> None:
        self._profile = profile

    def get(self, _model: object, _pid: object) -> DomainStrategyProfile:
        return self._profile


class _StubFlushSettings:
    STRATEGY_PROMOTION_MIN_SUCCESSES = 3
    STRATEGY_PROMOTION_MIN_DISTINCT_URLS = 3
    STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD = 0.85
    STRATEGY_REDISCOVERY_CONSECUTIVE_FAILURES = 3
    STRATEGY_REDISCOVERY_SUCCESS_RATE_FLOOR = 0.80
    STRATEGY_REDISCOVERY_LOW_CONFIDENCE = 0.75
    STRATEGY_PROFILE_SCOPE = "domain"
    STRATEGY_SWITCH_HYSTERESIS_ENABLED = True
    STRATEGY_SWITCH_MIN_EVIDENCE_SAMPLES = 6
    STRATEGY_SWITCH_MIN_EVIDENCE_WINDOW_SECONDS = 1800
    STRATEGY_SWITCH_REVERT_EVIDENCE_MULTIPLIER = 2.0
    STRATEGY_SWITCH_ROLLBACK_MIN_ATTEMPTS = 10
    STRATEGY_SWITCH_ROLLBACK_DEGRADATION_MARGIN = 0.20


def _degraded_profile() -> DomainStrategyProfile:
    profile = DomainStrategyProfile(
        workspace_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        domain="fqtoners.com",
        url_pattern="fqtoners.com",
        url_pattern_version=1,
        status=StrategyStatus.DEGRADED,
        preferred_access_method=AccessMethod.DIRECT_HTTP,
        preferred_extraction_method=ExtractionMethod.JSON_LD,
        confirmed_success_count=3,
        recent_failure_count=0,
    )
    profile.id = uuid.uuid4()
    return profile


@pytest.fixture()
def flush_seams(monkeypatch: pytest.MonkeyPatch):
    """Every `flush_profile` collaborator that would issue SQL, stubbed;
    `apply_promotion` records the decisions it was handed."""
    monkeypatch.setattr(flush_mod, "get_settings", lambda: _StubFlushSettings())
    monkeypatch.setattr(flush_mod, "set_workspace_context", lambda *a, **k: None)
    monkeypatch.setattr(flush_mod, "stats_for_profile", lambda *a, **k: [])
    monkeypatch.setattr(
        flush_mod, "build_recent_signals", lambda *a, **k: RecentSignals(attempts=())
    )
    monkeypatch.setattr(flush_mod, "apply_rediscovery", lambda *a, **k: False)
    monkeypatch.setattr(flush_mod, "_rebase_method_stats", lambda *a, **k: None)
    monkeypatch.setattr(
        flush_mod,
        "_upsert_stats",
        lambda *a, **k: SimpleNamespace(
            avg_confidence=Decimal("0.95"),
            attempt_count=10,
            success_count=9,
            # Two hours of observation — clears the 30-minute window bar,
            # so these tests isolate the SAMPLE half of the guard.
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        ),
    )

    seen: list[dict] = []

    def _apply(session, profile_id, *, method_type, method_name, decision):
        seen.append(
            {
                "method_type": method_type,
                "method_name": method_name,
                "promote": decision.promote,
                "reason": decision.reason,
            }
        )
        return decision.promote

    monkeypatch.setattr(flush_mod, "apply_promotion", _apply)
    return seen


def _stub_drain(monkeypatch, target_type: MethodType, target_name: str, drained):
    def _drain(_redis, **kwargs):
        if (
            kwargs.get("method_type") is target_type
            and kwargs.get("method_name") == target_name
        ):
            return drained
        return _FakeDrained(0, 0, 0, 0, 0, 0, 0)

    monkeypatch.setattr(flush_mod.stats_buffer, "drain", _drain)


def test_flush_holds_the_switch_when_evidence_is_thin(
    monkeypatch: pytest.MonkeyPatch, flush_seams
) -> None:
    """End to end: a DEGRADED profile preferring DIRECT_HTTP sees three
    qualifying PROXY_HTTP samples -- enough for the raw promotion
    evaluator, not enough to repoint the domain."""
    profile = _degraded_profile()
    _stub_drain(
        monkeypatch,
        MethodType.ACCESS,
        AccessMethod.PROXY_HTTP.value,
        _FakeDrained(3, 3, 0, 0, 28500, 3, 3),
    )

    flush_mod.flush_profile(_FlushSession(profile), _FakeRedis(), profile.id)

    proxy = [
        call
        for call in flush_seams
        if call["method_name"] == AccessMethod.PROXY_HTTP.value
    ]
    assert proxy, "PROXY_HTTP must still have been evaluated"
    assert proxy[0]["promote"] is False
    assert "held by hysteresis" in proxy[0]["reason"]
    assert profile.preferred_access_method is AccessMethod.DIRECT_HTTP


def test_flush_allows_the_switch_once_the_evidence_bar_is_cleared(
    monkeypatch: pytest.MonkeyPatch, flush_seams
) -> None:
    profile = _degraded_profile()
    _stub_drain(
        monkeypatch,
        MethodType.ACCESS,
        AccessMethod.PROXY_HTTP.value,
        _FakeDrained(6, 6, 0, 0, 57000, 6, 6),
    )

    flush_mod.flush_profile(_FlushSession(profile), _FakeRedis(), profile.id)

    proxy = [
        call
        for call in flush_seams
        if call["method_name"] == AccessMethod.PROXY_HTTP.value
    ]
    assert proxy[0]["promote"] is True


def test_flush_never_holds_a_same_method_reaffirmation(
    monkeypatch: pytest.MonkeyPatch, flush_seams
) -> None:
    """The fqtoners.com regression guard: the hysteresis gate must not
    touch a same-method re-promotion out of DEGRADED, at ANY evidence
    level."""
    profile = _degraded_profile()
    _stub_drain(
        monkeypatch,
        MethodType.ACCESS,
        AccessMethod.DIRECT_HTTP.value,
        _FakeDrained(3, 3, 0, 0, 28500, 3, 3),
    )

    flush_mod.flush_profile(_FlushSession(profile), _FakeRedis(), profile.id)

    direct = [
        call
        for call in flush_seams
        if call["method_name"] == AccessMethod.DIRECT_HTTP.value
    ]
    assert direct[0]["promote"] is True
    assert "held by hysteresis" not in direct[0]["reason"]


def test_flush_with_hysteresis_disabled_restores_the_old_behaviour(
    monkeypatch: pytest.MonkeyPatch, flush_seams
) -> None:
    """The kill switch must be a true no-op path -- three samples repoint
    the domain again, exactly as before W5.5-L2."""

    class _Off(_StubFlushSettings):
        STRATEGY_SWITCH_HYSTERESIS_ENABLED = False

    monkeypatch.setattr(flush_mod, "get_settings", lambda: _Off())
    profile = _degraded_profile()
    _stub_drain(
        monkeypatch,
        MethodType.ACCESS,
        AccessMethod.PROXY_HTTP.value,
        _FakeDrained(3, 3, 0, 0, 28500, 3, 3),
    )

    flush_mod.flush_profile(_FlushSession(profile), _FakeRedis(), profile.id)

    proxy = [
        call
        for call in flush_seams
        if call["method_name"] == AccessMethod.PROXY_HTTP.value
    ]
    assert proxy[0]["promote"] is True


def test_flush_reverts_the_profile_on_a_degraded_switch(
    monkeypatch: pytest.MonkeyPatch, flush_seams
) -> None:
    """THE rollback requirement, end to end: an open switch to PROXY_HTTP
    whose post-switch outcomes are far worse than the DIRECT_HTTP
    baseline it replaced must put the profile back on DIRECT_HTTP, and
    stamp the audit row exactly once."""
    profile = _degraded_profile()
    profile.preferred_access_method = AccessMethod.PROXY_HTTP
    switch_id = uuid.uuid4()
    stamped: list[dict] = []

    monkeypatch.setattr(
        flush_mod.hysteresis,
        "latest_open_switch",
        lambda session, pid, mtype: (
            SimpleNamespace(
                id=switch_id,
                from_method=AccessMethod.DIRECT_HTTP.value,
                to_method=AccessMethod.PROXY_HTTP.value,
                baseline_success_rate=Decimal("0.9000"),
                switch_attempt_count=0,
                switch_success_count=0,
                rolled_back_at=None,
            )
            if mtype == MethodType.ACCESS.value and not stamped
            else None
        ),
    )

    def _mark(session, sid, *, reason, now):
        if stamped:
            return False
        stamped.append({"id": sid, "reason": reason})
        return True

    monkeypatch.setattr(flush_mod.hysteresis, "mark_rolled_back", _mark)
    monkeypatch.setattr(
        flush_mod,
        "stats_for_profile",
        lambda *a, **k: [
            SimpleNamespace(
                method_type=MethodType.ACCESS,
                method_name=AccessMethod.PROXY_HTTP.value,
                attempt_count=20,
                success_count=4,
                success_rate=Decimal("0.2000"),
                avg_confidence=Decimal("0.9000"),
            )
        ],
    )
    _stub_drain(
        monkeypatch,
        MethodType.ACCESS,
        AccessMethod.PROXY_HTTP.value,
        _FakeDrained(1, 0, 1, 0, 0, 0, 0),
    )

    flush_mod.flush_profile(_FlushSession(profile), _FakeRedis(), profile.id)

    assert profile.preferred_access_method is AccessMethod.DIRECT_HTTP
    assert len(stamped) == 1
    assert stamped[0]["id"] == switch_id

    # A second flush must NOT roll back again -- the switch is closed.
    flush_mod.flush_profile(_FlushSession(profile), _FakeRedis(), profile.id)
    assert len(stamped) == 1


def test_flush_does_not_revert_when_the_stamp_was_claimed_concurrently(
    monkeypatch: pytest.MonkeyPatch, flush_seams
) -> None:
    """If another worker won the `WHERE rolled_back_at IS NULL` race, this
    worker must leave the profile alone -- exactly one rollback, not two
    reverts of one switch."""
    profile = _degraded_profile()
    profile.preferred_access_method = AccessMethod.PROXY_HTTP

    monkeypatch.setattr(
        flush_mod.hysteresis,
        "latest_open_switch",
        lambda session, pid, mtype: (
            SimpleNamespace(
                id=uuid.uuid4(),
                from_method=AccessMethod.DIRECT_HTTP.value,
                to_method=AccessMethod.PROXY_HTTP.value,
                baseline_success_rate=Decimal("0.9000"),
                switch_attempt_count=0,
                switch_success_count=0,
                rolled_back_at=None,
            )
            if mtype == MethodType.ACCESS.value
            else None
        ),
    )
    monkeypatch.setattr(
        flush_mod.hysteresis, "mark_rolled_back", lambda *a, **k: False
    )
    monkeypatch.setattr(
        flush_mod,
        "stats_for_profile",
        lambda *a, **k: [
            SimpleNamespace(
                method_type=MethodType.ACCESS,
                method_name=AccessMethod.PROXY_HTTP.value,
                attempt_count=20,
                success_count=4,
                success_rate=Decimal("0.2000"),
                avg_confidence=Decimal("0.9000"),
            )
        ],
    )
    _stub_drain(
        monkeypatch,
        MethodType.ACCESS,
        AccessMethod.PROXY_HTTP.value,
        _FakeDrained(1, 0, 1, 0, 0, 0, 0),
    )

    flush_mod.flush_profile(_FlushSession(profile), _FakeRedis(), profile.id)

    assert profile.preferred_access_method is AccessMethod.PROXY_HTTP
