"""Regression tests for Task 3.2: `apply_promotion`'s `DEGRADED` dead-end
(2026-08-16 saas-core-optimization handover §6 bug 2).

## The bug

`apply_promotion`'s guarded `UPDATE` used to require
`(preferred_{type}_method IS NULL OR preferred_{type}_method <> :m)` in
its `WHERE` clause -- so a `DEGRADED` profile whose best method, on
re-validation, turned out to be the *same* one it already had could
never match the statement. Discovery/re-validation only ever runs for
profiles the resolver still serves, and nothing else flips `status` back
to `ACTIVE` -- the profile was parked `DEGRADED` forever. Measured live:
`fqtoners.com`.

## The two pieces this file proves

1. **`apply_promotion` itself** (`test_apply_promotion_*`) -- exercised
   against a real (in-memory SQLite) `domain_strategy_profiles` table so
   the `WHERE` clause is genuinely evaluated, not just returned by a fake
   session. `apply_promotion` uses a plain `sqlalchemy.update()` (no
   Postgres-only constructs), so this is a faithful, fast substitute for
   the live-Postgres integration test
   (`tests/integration/test_promotion_apply.py`) this environment cannot
   run (no reachable `DATABASE_URL`).

2. **`flush_profile`'s stats re-base wiring** (`test_flush_profile_*`) --
   the brief's critical second half: unblocking `apply_promotion` alone
   would let a `DEGRADED` profile flip back to `ACTIVE` while its
   `strategy_attempt_stats` row still carries the *lifetime* failure-
   dominated ratio that tripped rediscovery condition 2 in the first
   place -- so it would immediately re-degrade on the very next
   evaluation, the exact 1,439-runs/day bug class
   `flush.rebase_stats_after_discovery` already fixed for the discovery-
   seed path. `flush_profile` must call the same re-base machinery for a
   same-method `DEGRADED` re-promotion. Exercised via monkeypatched seams
   (mirrors `tests/unit/test_rediscovery_runaway.py`'s `_FakeSession`/
   `stub_rediscovery_settings` style), since `flush_profile`'s other
   collaborators (`_upsert_stats`, `rebase`) use Postgres-only
   `INSERT ... ON CONFLICT` and cannot run against SQLite.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app_shared.enums import AccessMethod, ExtractionMethod, MethodType, StrategyStatus
from app_shared.models.strategy import DomainStrategyProfile
from app_shared.strategy import flush as flush_mod
from app_shared.strategy.promotion import PromotionDecision, apply_promotion
from app_shared.strategy.rediscovery import RecentSignals

# ---------------------------------------------------------------------------
# 1. apply_promotion against a real (SQLite) guarded UPDATE.
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_session():
    """In-memory SQLite, `domain_strategy_profiles` table only.

    `apply_promotion`'s statement is a plain `sqlalchemy.update()` (no
    `pg_insert`/Postgres-only constructs), so SQLite genuinely evaluates
    the same `WHERE id=:pid AND status IN (...)` predicate Postgres would
    -- this is not a canned-rowcount fake. SQLite does not enforce FK
    constraints unless `PRAGMA foreign_keys=ON` is set (it isn't here),
    so the referenced `competitors`/`workspaces` tables never need to
    exist for this single-table DDL.
    """
    engine = create_engine("sqlite:///:memory:")
    DomainStrategyProfile.metadata.create_all(engine, tables=[DomainStrategyProfile.__table__])
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _seed_profile(
    session,
    *,
    status: StrategyStatus,
    preferred_access_method: AccessMethod | None,
    access_confidence: Decimal | None = None,
    confirmed_success_count: int = 0,
) -> uuid.UUID:
    profile = DomainStrategyProfile(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        domain="fqtoners.com",
        url_pattern="fqtoners.com",
        url_pattern_version=1,
        status=status,
        preferred_access_method=preferred_access_method,
        access_confidence=access_confidence,
        confirmed_success_count=confirmed_success_count,
        recent_failure_count=0,
    )
    session.add(profile)
    session.commit()
    return profile.id


_PROMOTE_DECISION = PromotionDecision(promote=True, confidence=Decimal("0.95"), reason="test")
_NO_PROMOTE_DECISION = PromotionDecision(promote=False, confidence=Decimal("0.5"), reason="test")


def test_degraded_profile_same_method_re_promotes_to_active(sqlite_session) -> None:
    """THE bug (Task 3.2 Step 1, TDD RED->GREEN): a `DEGRADED` profile
    whose best method, on re-validation, is the SAME one it already has
    (`preferred_access_method == validated_name`) must promote back to
    `ACTIVE` when validation succeeded (`decision.promote=True`) -- this
    is exactly the fqtoners.com dead-end. Before the fix, the guarded
    `UPDATE`'s `WHERE` clause required the method to *change*, so this
    returned `False` and `status` never left `DEGRADED`."""
    pid = _seed_profile(
        sqlite_session,
        status=StrategyStatus.DEGRADED,
        preferred_access_method=AccessMethod.DIRECT_HTTP,
        access_confidence=Decimal("0.5000"),
        confirmed_success_count=3,
    )

    applied = apply_promotion(
        sqlite_session,
        pid,
        method_type=MethodType.ACCESS,
        method_name=AccessMethod.DIRECT_HTTP.value,  # SAME method, unchanged
        decision=_PROMOTE_DECISION,
    )
    sqlite_session.commit()

    assert applied is True, "same-method re-promotion from DEGRADED must succeed"

    profile = sqlite_session.get(DomainStrategyProfile, pid)
    assert profile.status is StrategyStatus.ACTIVE
    assert profile.preferred_access_method == AccessMethod.DIRECT_HTTP
    assert profile.access_confidence == Decimal("0.9500")
    # confirmed_success_count is a monotonic confirmation counter, not a
    # "distinct methods confirmed" counter -- a genuine re-confirmation
    # bumps it, mirroring a changed-method promotion.
    assert profile.confirmed_success_count == 4


def test_non_qualifying_decision_leaves_degraded_profile_parked(sqlite_session) -> None:
    """Self-review: promotion must still require validation SUCCESS -- a
    `promote=False` decision must never flip `DEGRADED` -> `ACTIVE`, same
    method or not."""
    pid = _seed_profile(
        sqlite_session,
        status=StrategyStatus.DEGRADED,
        preferred_access_method=AccessMethod.DIRECT_HTTP,
    )

    applied = apply_promotion(
        sqlite_session,
        pid,
        method_type=MethodType.ACCESS,
        method_name=AccessMethod.DIRECT_HTTP.value,
        decision=_NO_PROMOTE_DECISION,
    )
    sqlite_session.commit()

    assert applied is False
    profile = sqlite_session.get(DomainStrategyProfile, pid)
    assert profile.status is StrategyStatus.DEGRADED


def test_degraded_profile_different_method_still_promotes(sqlite_session) -> None:
    """Self-review: a *different*-method promotion out of `DEGRADED`
    (already worked before this fix) must remain unaffected."""
    pid = _seed_profile(
        sqlite_session,
        status=StrategyStatus.DEGRADED,
        preferred_access_method=AccessMethod.DIRECT_HTTP,
    )

    applied = apply_promotion(
        sqlite_session,
        pid,
        method_type=MethodType.ACCESS,
        method_name=AccessMethod.PROXY_HTTP.value,  # DIFFERENT method
        decision=_PROMOTE_DECISION,
    )
    sqlite_session.commit()

    assert applied is True
    profile = sqlite_session.get(DomainStrategyProfile, pid)
    assert profile.status is StrategyStatus.ACTIVE
    assert profile.preferred_access_method == AccessMethod.PROXY_HTTP


def test_active_profile_is_never_re_promoted_same_or_different_method(sqlite_session) -> None:
    """Concurrency guard (contracts/promotion.md "Concurrent promotion")
    survives dropping the method-changed predicate: once `status` is
    `ACTIVE` (a real prior promotion committed), the `status IN (...)`
    filter alone blocks every later apply for that profile -- same
    method or not -- so `confirmed_success_count` is never double-bumped."""
    pid = _seed_profile(
        sqlite_session,
        status=StrategyStatus.ACTIVE,
        preferred_access_method=AccessMethod.DIRECT_HTTP,
        access_confidence=Decimal("0.9000"),
        confirmed_success_count=1,
    )

    same_method = apply_promotion(
        sqlite_session,
        pid,
        method_type=MethodType.ACCESS,
        method_name=AccessMethod.DIRECT_HTTP.value,
        decision=_PROMOTE_DECISION,
    )
    sqlite_session.commit()
    assert same_method is False

    different_method = apply_promotion(
        sqlite_session,
        pid,
        method_type=MethodType.ACCESS,
        method_name=AccessMethod.PROXY_HTTP.value,
        decision=_PROMOTE_DECISION,
    )
    sqlite_session.commit()
    assert different_method is False

    profile = sqlite_session.get(DomainStrategyProfile, pid)
    assert profile.confirmed_success_count == 1
    assert profile.preferred_access_method == AccessMethod.DIRECT_HTTP


def test_disabled_profile_never_auto_promotes(sqlite_session) -> None:
    """FR-014, unaffected by this fix: DISABLED stays outside
    `_PROMOTABLE_STATUSES` regardless of method-changed status."""
    pid = _seed_profile(
        sqlite_session,
        status=StrategyStatus.DISABLED,
        preferred_access_method=AccessMethod.DIRECT_HTTP,
    )

    applied = apply_promotion(
        sqlite_session,
        pid,
        method_type=MethodType.ACCESS,
        method_name=AccessMethod.DIRECT_HTTP.value,
        decision=_PROMOTE_DECISION,
    )
    sqlite_session.commit()

    assert applied is False
    profile = sqlite_session.get(DomainStrategyProfile, pid)
    assert profile.status is StrategyStatus.DISABLED


# ---------------------------------------------------------------------------
# 2. flush_profile: the stats-rebase wiring for same-method DEGRADED re-promo.
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


class _FakeRedis:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.srem_calls: list[tuple[str, str]] = []

    def delete(self, key: str) -> None:
        self.deleted.append(key)

    def srem(self, key: str, member: str) -> None:
        self.srem_calls.append((key, member))


class _FakeSession:
    """`flush_profile` only ever calls `.get(...)` on this fake -- every
    other collaborator (`_upsert_stats`, `stats_for_profile`,
    `build_recent_signals`, `apply_promotion`, `apply_rediscovery`) is
    monkeypatched at the module level below, so nothing here ever needs
    to run real SQL."""

    def __init__(self, profile: DomainStrategyProfile) -> None:
        self._profile = profile

    def get(self, _model: object, _pid: object) -> DomainStrategyProfile:
        return self._profile


def _degraded_profile(*, preferred: AccessMethod = AccessMethod.DIRECT_HTTP) -> DomainStrategyProfile:
    profile = DomainStrategyProfile(
        workspace_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        domain="fqtoners.com",
        url_pattern="fqtoners.com",
        url_pattern_version=1,
        status=StrategyStatus.DEGRADED,
        preferred_access_method=preferred,
        preferred_extraction_method=ExtractionMethod.JSON_LD,
        confirmed_success_count=3,
        recent_failure_count=0,
    )
    profile.id = uuid.uuid4()
    return profile


class _StubFlushSettings:
    STRATEGY_PROMOTION_MIN_SUCCESSES = 3
    STRATEGY_PROMOTION_MIN_DISTINCT_URLS = 3
    STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD = 0.85
    STRATEGY_REDISCOVERY_CONSECUTIVE_FAILURES = 3
    STRATEGY_REDISCOVERY_SUCCESS_RATE_FLOOR = 0.80
    STRATEGY_REDISCOVERY_LOW_CONFIDENCE = 0.75
    STRATEGY_PROFILE_SCOPE = "domain"


@pytest.fixture()
def flush_seams(monkeypatch: pytest.MonkeyPatch):
    """Monkeypatch every `flush_profile` collaborator except the one
    under test (`_rebase_method_stats`, captured) and `apply_promotion`
    (stubbed per-test) -- mirrors `test_rediscovery_runaway.py`'s
    `stub_rediscovery_settings`/`captured_enqueue` seam style."""
    monkeypatch.setattr(flush_mod, "get_settings", lambda: _StubFlushSettings())
    monkeypatch.setattr(flush_mod, "set_workspace_context", lambda *a, **k: None)
    monkeypatch.setattr(flush_mod, "stats_for_profile", lambda *a, **k: [])
    monkeypatch.setattr(flush_mod, "build_recent_signals", lambda *a, **k: RecentSignals(attempts=()))
    monkeypatch.setattr(flush_mod, "apply_rediscovery", lambda *a, **k: False)
    monkeypatch.setattr(
        flush_mod,
        "_upsert_stats",
        lambda *a, **k: SimpleNamespace(avg_confidence=Decimal("0.5")),
    )

    rebase_calls: list[dict] = []
    monkeypatch.setattr(
        flush_mod,
        "_rebase_method_stats",
        lambda session, profile_id, method_type, method_name, *, qualifying_count, confidence, now: (
            rebase_calls.append(
                {
                    "profile_id": profile_id,
                    "method_type": method_type,
                    "method_name": method_name,
                    "qualifying_count": qualifying_count,
                    "confidence": confidence,
                }
            )
        ),
    )
    return rebase_calls


def _drain_only_for(
    target_method_type: MethodType, target_method_name: str, *, drained: _FakeDrained
):
    def _drain(_redis, *, profile_id, method_type, method_name):  # noqa: ANN001
        if method_type is target_method_type and method_name == target_method_name:
            return drained
        return _FakeDrained(0, 0, 0, 0, 0, 0, 0)

    return _drain


def test_flush_profile_rebases_stats_on_same_method_degraded_repromotion(
    monkeypatch: pytest.MonkeyPatch, flush_seams: list[dict]
) -> None:
    """The wiring half of Task 3.2: when `apply_promotion` actually
    re-promotes the profile's *already-preferred* method while it was
    `DEGRADED`, `flush_profile` must re-base that method's
    `strategy_attempt_stats` row -- otherwise the stale lifetime ratio
    that tripped rediscovery survives the promotion and re-degrades the
    profile on the very next evaluation (same flush cycle)."""
    profile = _degraded_profile(preferred=AccessMethod.DIRECT_HTTP)
    monkeypatch.setattr(
        flush_mod.stats_buffer,
        "drain",
        _drain_only_for(
            MethodType.ACCESS,
            AccessMethod.DIRECT_HTTP.value,
            drained=_FakeDrained(
                attempt=3, success=3, failure=0, rt_ms_sum=300, conf_sum=27000,
                qualifying_success=3, distinct_urls=3,
            ),
        ),
    )
    monkeypatch.setattr(flush_mod, "apply_promotion", lambda *a, **k: True)

    result = flush_mod.flush_profile(_FakeSession(profile), _FakeRedis(), profile.id)

    assert len(result.transitions) == 1
    assert result.transitions[0].change == "PROMOTED"
    assert len(flush_seams) == 1, "same-method DEGRADED re-promotion must trigger exactly one rebase"
    call = flush_seams[0]
    assert call["method_type"] is MethodType.ACCESS
    assert call["method_name"] == AccessMethod.DIRECT_HTTP.value
    # The honest fresh evidence this promotion rests on: the cumulative
    # distinct-URL count (survives across drains until promotion), not
    # the per-cycle qualifying_success delta.
    assert call["qualifying_count"] == 3


def test_flush_profile_does_not_rebase_a_different_method_promotion(
    monkeypatch: pytest.MonkeyPatch, flush_seams: list[dict]
) -> None:
    """Self-review: a *different*-method promotion while DEGRADED (e.g. a
    fresh candidate method winning, not the already-preferred one that
    tripped rediscovery) must NOT be rebased -- its stats row has no
    stale-failure baggage from a prior degradation to clear."""
    profile = _degraded_profile(preferred=AccessMethod.DIRECT_HTTP)
    monkeypatch.setattr(
        flush_mod.stats_buffer,
        "drain",
        _drain_only_for(
            MethodType.ACCESS,
            AccessMethod.PROXY_HTTP.value,  # NOT the preferred method
            drained=_FakeDrained(
                attempt=3, success=3, failure=0, rt_ms_sum=300, conf_sum=27000,
                qualifying_success=3, distinct_urls=3,
            ),
        ),
    )
    monkeypatch.setattr(flush_mod, "apply_promotion", lambda *a, **k: True)

    result = flush_mod.flush_profile(_FakeSession(profile), _FakeRedis(), profile.id)

    assert len(result.transitions) == 1
    assert flush_seams == [], "a different-method promotion must not be rebased"


def test_flush_profile_does_not_rebase_when_not_previously_degraded(
    monkeypatch: pytest.MonkeyPatch, flush_seams: list[dict]
) -> None:
    """Self-review: a same-method promotion from `LEARNING`/
    `DISCOVERY_REQUIRED` (not `DEGRADED`) is a first-time confirmation,
    not a remedy for a stale-failure trip -- must not be rebased either."""
    profile = _degraded_profile(preferred=AccessMethod.DIRECT_HTTP)
    profile.status = StrategyStatus.LEARNING
    monkeypatch.setattr(
        flush_mod.stats_buffer,
        "drain",
        _drain_only_for(
            MethodType.ACCESS,
            AccessMethod.DIRECT_HTTP.value,
            drained=_FakeDrained(
                attempt=3, success=3, failure=0, rt_ms_sum=300, conf_sum=27000,
                qualifying_success=3, distinct_urls=3,
            ),
        ),
    )
    monkeypatch.setattr(flush_mod, "apply_promotion", lambda *a, **k: True)

    result = flush_mod.flush_profile(_FakeSession(profile), _FakeRedis(), profile.id)

    assert len(result.transitions) == 1
    assert flush_seams == []


def test_flush_profile_does_not_rebase_when_promotion_does_not_apply(
    monkeypatch: pytest.MonkeyPatch, flush_seams: list[dict]
) -> None:
    """Self-review: `apply_promotion` returning `False` (e.g. lost a
    concurrent race, or the decision didn't qualify) must never trigger a
    rebase -- only a genuine, applied promotion does."""
    profile = _degraded_profile(preferred=AccessMethod.DIRECT_HTTP)
    monkeypatch.setattr(
        flush_mod.stats_buffer,
        "drain",
        _drain_only_for(
            MethodType.ACCESS,
            AccessMethod.DIRECT_HTTP.value,
            drained=_FakeDrained(
                attempt=3, success=3, failure=0, rt_ms_sum=300, conf_sum=27000,
                qualifying_success=3, distinct_urls=3,
            ),
        ),
    )
    monkeypatch.setattr(flush_mod, "apply_promotion", lambda *a, **k: False)

    result = flush_mod.flush_profile(_FakeSession(profile), _FakeRedis(), profile.id)

    assert result.transitions == ()
    assert flush_seams == []
