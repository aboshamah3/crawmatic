"""Regression tests for the 2026-08-15 runaway-rediscovery loop.

## What actually happened (measured in production, read-only)

`STRATEGY_LIGHT_RECHECK` is enqueued by `apps/scheduler` on the 60s
maintenance tick and evaluates every `ACTIVE` profile. Rediscovery
conditions 1 and 2 read state that the rediscovery *remedy* never
cleared:

* condition 1 reads `domain_strategy_profiles.recent_failure_count`,
  which only `flush_profile` ever resets (on a live qualifying success);
* condition 2 reads the **lifetime** `strategy_attempt_stats`
  success ratio, and `_probe_sample` writes no stats at all.

`seed_from_discovery` put the profile back to `ACTIVE` without touching
either, so the next tick re-evaluated byte-identical inputs and
re-triggered — one discovery run per profile per minute, forever.

Live evidence: `fqtoners.com` held `DIRECT_HTTP` at 3 successes / 6
attempts = 0.500 against `STRATEGY_REDISCOVERY_SUCCESS_RATE_FLOOR`
(0.80) and logged **13,151** `COMPLETED` discovery runs against the bare
`fqtoners.com` key — a flat 1,439/day (1,440 minutes in a day) for ten
consecutive days. It cost nothing only because fqtoners answers
`DIRECT_HTTP`; `extra.com`, behind a WAF, escalated every probe to the
paid proxy and burned ~87k proxied requests.

The shapes used below are the real production ones, including the
URL-encoded and raw-Arabic `fqtoners.com` path segments that appear in
`strategy_discovery_runs.url_pattern`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql

from app_shared.enums import (
    AccessMethod,
    ExtractionMethod,
    MethodType,
    StrategyStatus,
)
from app_shared.models.strategy import DomainStrategyProfile
from app_shared.strategy.promotion import PromotionThresholds
from app_shared.strategy.rediscovery import (
    CombinedStats,
    RecentSignals,
    RediscoveryDecision,
    RediscoveryThresholds,
    apply_rediscovery,
    evaluate_rediscovery,
)
from app_shared.strategy.seed import DiscoverySeedConfidences, seed_from_discovery

# The real production key: a bare apex with no path at all (domain
# profile scope) plus two of the real derived match patterns, one
# percent-encoded and one raw UTF-8 Arabic.
_FQTONERS_DOMAIN = "fqtoners.com"
_FQTONERS_PATTERNS = (
    "fqtoners.com",
    "fqtoners.com/en/toner-hp-212a-w2122a-yellow/:id",
    "fqtoners.com/%D8%AD%D8%A8%D8%B1-%D9%84%D9%8A%D8%B2%D8%B1-w2411a-216a/:id",
    "fqtoners.com/ar/حبر-ماكينة-تصوير-توشيبا-أصلي-t2505-5k-أسود/:id",
)

_THRESHOLDS = RediscoveryThresholds(
    consecutive_failures=3,
    success_rate_floor=Decimal("0.80"),
    low_confidence=Decimal("0.75"),
)
_PROMOTION = PromotionThresholds(
    confidence_threshold=Decimal("0.85"), min_successes=3, min_distinct_urls=3
)

#: What a healthy discovery run reports back: the whole 10-URL sample
#: qualified at 0.95 confidence (the exact shape every `COMPLETED`
#: fqtoners/extra run recorded — `sample_size=10`).
_FULL_SAMPLE = DiscoverySeedConfidences(
    access_confidence=Decimal("0.95"),
    access_qualifying_count=10,
    access_distinct_url_count=10,
    extraction_confidence=Decimal("0.95"),
    extraction_qualifying_count=10,
    extraction_distinct_url_count=10,
)


def _profile(
    *,
    url_pattern: str = _FQTONERS_DOMAIN,
    status: StrategyStatus = StrategyStatus.ACTIVE,
    recent_failure_count: int = 0,
    last_discovery_at: datetime | None = None,
) -> DomainStrategyProfile:
    profile = DomainStrategyProfile(
        workspace_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        domain=_FQTONERS_DOMAIN,
        url_pattern=url_pattern,
        url_pattern_version=1,
        status=status,
        preferred_access_method=AccessMethod.DIRECT_HTTP,
        preferred_extraction_method=ExtractionMethod.JSON_LD,
        confirmed_success_count=0,
        recent_failure_count=recent_failure_count,
        last_discovery_at=last_discovery_at,
    )
    profile.id = uuid.uuid4()
    return profile


class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    """Records every statement; reports a caller-chosen `rowcount`."""

    def __init__(self, rowcount: int = 1) -> None:
        self.statements: list[object] = []
        self._rowcount = rowcount

    def execute(self, statement: object) -> _FakeResult:
        self.statements.append(statement)
        return _FakeResult(self._rowcount)


def _compiled(statement: object) -> tuple[str, dict]:
    compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    return str(compiled), dict(compiled.params)


class _StubSettings:
    def __init__(
        self,
        *,
        min_interval_seconds: int = 21600,
        max_runs_per_key_per_day: int = 6,
    ) -> None:
        self.STRATEGY_REDISCOVERY_MIN_INTERVAL_SECONDS = min_interval_seconds
        self.STRATEGY_DISCOVERY_MAX_RUNS_PER_KEY_PER_DAY = max_runs_per_key_per_day


@pytest.fixture
def stub_rediscovery_settings(monkeypatch: pytest.MonkeyPatch):
    def _apply(**kwargs: int) -> _StubSettings:
        settings = _StubSettings(**kwargs)
        monkeypatch.setattr(
            "app_shared.strategy.rediscovery.get_settings", lambda: settings
        )
        return settings

    return _apply


@pytest.fixture
def captured_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def _enqueue(task_name: str, *, queue: str, kwargs: dict) -> None:
        calls.append({"task": task_name, "queue": queue, "kwargs": kwargs})

    monkeypatch.setattr("app_shared.strategy.rediscovery.enqueue", _enqueue)
    return calls


# --------------------------------------------------------------------------
# 1. Root cause: the remedy must clear the state that triggered it.
# --------------------------------------------------------------------------


def test_seed_clears_the_failure_streak_that_triggered_condition_1() -> None:
    """Condition 1's entire input is `recent_failure_count`, and discovery
    used to leave it exactly as it found it — so a profile degraded at the
    threshold came back `ACTIVE` still at the threshold and re-triggered on
    the very next 60s tick."""
    profile = _profile(status=StrategyStatus.DEGRADED, recent_failure_count=3)

    seed_from_discovery(
        profile,
        winning_access=AccessMethod.DIRECT_HTTP,
        winning_extraction=ExtractionMethod.JSON_LD,
        confidences=_FULL_SAMPLE,
        thresholds=_PROMOTION,
    )

    assert profile.status is StrategyStatus.ACTIVE
    assert profile.recent_failure_count == 0

    # The re-evaluation the next light-recheck tick performs must now be
    # a no-op instead of an immediate re-trigger.
    decision = evaluate_rediscovery(
        profile,
        CombinedStats(recent_failure_count=profile.recent_failure_count, success_rate=None),
        RecentSignals(attempts=()),
        _THRESHOLDS,
        scope="domain",
    )
    assert decision.trigger is False, decision.reason


def test_seed_leaves_a_no_winner_run_untouched() -> None:
    """`NO_WINNER` must not silently launder a failure streak — nothing was
    re-established, so nothing is stale yet."""
    profile = _profile(status=StrategyStatus.DEGRADED, recent_failure_count=3)

    seed_from_discovery(
        profile,
        winning_access=None,
        winning_extraction=None,
        confidences=None,
        thresholds=_PROMOTION,
    )

    assert profile.status is StrategyStatus.DEGRADED
    assert profile.recent_failure_count == 3


def test_discovery_rebases_the_lifetime_ratio_that_triggered_condition_2() -> None:
    """The fqtoners loop, reproduced end to end.

    `DIRECT_HTTP` at 3/6 = 0.500 trips the 0.80 floor. Discovery then
    qualifies the whole 10-URL sample. Before the fix the counters were
    untouched, so the ratio was still 0.500 and the next tick re-triggered
    — 1,439 times a day. After the fix the winning methods carry the run's
    own result and the re-evaluation is clean.
    """
    from app_shared.strategy.flush import rebase_stats_after_discovery

    profile = _profile()

    lifetime = CombinedStats(recent_failure_count=0, success_rate=Decimal("0.5"))
    assert evaluate_rediscovery(
        profile, lifetime, RecentSignals(attempts=()), _THRESHOLDS, scope="domain"
    ).trigger is True

    seed_from_discovery(
        profile,
        winning_access=AccessMethod.DIRECT_HTTP,
        winning_extraction=ExtractionMethod.JSON_LD,
        confidences=_FULL_SAMPLE,
        thresholds=_PROMOTION,
    )

    session = _FakeSession()
    rebase_stats_after_discovery(
        session,
        profile.id,
        winning_access=AccessMethod.DIRECT_HTTP,
        winning_extraction=ExtractionMethod.JSON_LD,
        confidence=Decimal("0.95"),
        qualifying_count=10,
    )

    # One absolute upsert per winning method — access and extraction.
    assert len(session.statements) == 2
    rebased: dict[str, tuple[int, int]] = {}
    for statement in session.statements:
        sql, params = _compiled(statement)
        assert "ON CONFLICT" in sql.upper()
        # Absolute SET, never `count + delta` (that is `_upsert_stats`'s
        # job) — a re-base that accumulated would never clear the ratio.
        assert "attempt_count + " not in sql
        rebased[str(params["method_name"])] = (
            int(params["attempt_count"]),
            int(params["success_count"]),
        )

    assert rebased[AccessMethod.DIRECT_HTTP.value] == (10, 10)
    assert rebased[ExtractionMethod.JSON_LD.value] == (10, 10)

    # What the next light-recheck tick would now read for the preferred
    # access method.
    attempts, successes = rebased[AccessMethod.DIRECT_HTTP.value]
    post = CombinedStats(
        recent_failure_count=profile.recent_failure_count,
        success_rate=Decimal(successes) / Decimal(attempts),
    )
    assert evaluate_rediscovery(
        profile, post, RecentSignals(attempts=()), _THRESHOLDS, scope="domain"
    ).trigger is False


def test_rebase_is_a_no_op_without_qualifying_urls() -> None:
    from app_shared.strategy.flush import rebase_stats_after_discovery

    session = _FakeSession()
    rebase_stats_after_discovery(
        session,
        uuid.uuid4(),
        winning_access=AccessMethod.DIRECT_HTTP,
        winning_extraction=None,
        confidence=None,
        qualifying_count=0,
    )
    assert session.statements == []


def test_rebase_touches_only_the_winning_methods() -> None:
    """Operator-visible history for methods discovery did NOT pick (e.g.
    `PROXY_HTTP: 2,051 attempts / 0 successes` on amazon.sa) must survive —
    those rows are not what condition 2 reads."""
    from app_shared.strategy.flush import rebase_stats_after_discovery

    session = _FakeSession()
    rebase_stats_after_discovery(
        session,
        uuid.uuid4(),
        winning_access=AccessMethod.DIRECT_HTTP,
        winning_extraction=None,
        confidence=Decimal("0.95"),
        qualifying_count=10,
    )
    assert len(session.statements) == 1
    _sql, params = _compiled(session.statements[0])
    assert params["method_name"] == AccessMethod.DIRECT_HTTP.value
    assert params["method_type"] is MethodType.ACCESS


# --------------------------------------------------------------------------
# 2. Backstop: a correctness bug must not authorise unbounded discovery.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("url_pattern", _FQTONERS_PATTERNS)
def test_cooldown_is_a_database_predicate_not_an_app_side_check(
    url_pattern: str, stub_rediscovery_settings, captured_enqueue: list[dict]
) -> None:
    """The minimum re-discovery interval lives inside the same guarded
    UPDATE as the `status='ACTIVE'` predicate, so the database decides it
    against the committed row — two concurrent evaluations (inline flush +
    periodic light recheck) cannot both pass it.

    Parametrised over the real production key shapes, bare apex included:
    the bound is structural and must not depend on what the pattern looks
    like.
    """
    stub_rediscovery_settings(min_interval_seconds=21600)
    now = datetime.now(timezone.utc)
    profile = _profile(url_pattern=url_pattern, last_discovery_at=now - timedelta(minutes=1))
    session = _FakeSession(rowcount=1)

    apply_rediscovery(session, profile, RediscoveryDecision(trigger=True, reason="test"))

    assert len(session.statements) == 1
    sql, params = _compiled(session.statements[0])
    assert "last_discovery_at" in sql
    cutoffs = [
        value
        for value in params.values()
        if isinstance(value, datetime) and value < now - timedelta(hours=5)
    ]
    assert cutoffs, f"no cooldown cutoff bound in {params!r}"
    # `apply_rediscovery` takes its own `now` a hair before this one.
    assert timedelta(seconds=21595) <= now - cutoffs[0] <= timedelta(seconds=21605)


def test_cooldown_blocks_the_enqueue_when_the_update_matches_nothing(
    stub_rediscovery_settings, captured_enqueue: list[dict]
) -> None:
    """A profile inside its cooldown window degrades nothing and enqueues
    nothing — the 1,440/day loop degrades to at most one run per window."""
    stub_rediscovery_settings(min_interval_seconds=21600)
    profile = _profile(last_discovery_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    session = _FakeSession(rowcount=0)

    triggered = apply_rediscovery(
        session, profile, RediscoveryDecision(trigger=True, reason="success_rate=0.5 < 0.80")
    )

    assert triggered is False
    assert captured_enqueue == []


def test_a_profile_that_never_ran_discovery_is_never_cooled_down(
    stub_rediscovery_settings, captured_enqueue: list[dict]
) -> None:
    """`last_discovery_at IS NULL` must still be degradable — the cooldown
    bounds repetition, it must not block the first rediscovery ever."""
    stub_rediscovery_settings(min_interval_seconds=21600)
    profile = _profile(last_discovery_at=None)
    session = _FakeSession(rowcount=1)

    assert (
        apply_rediscovery(session, profile, RediscoveryDecision(trigger=True, reason="test"))
        is True
    )
    assert len(captured_enqueue) == 1
    assert captured_enqueue[0]["queue"] == "strategy_discovery"
    assert captured_enqueue[0]["kwargs"]["triggered_by"] == "REDISCOVERY"


def test_cooldown_can_be_disabled_by_configuration(
    stub_rediscovery_settings, captured_enqueue: list[dict]
) -> None:
    """0 disables the bound (config-only rollback, no deploy)."""
    stub_rediscovery_settings(min_interval_seconds=0)
    profile = _profile(last_discovery_at=datetime.now(timezone.utc))
    session = _FakeSession(rowcount=1)

    assert (
        apply_rediscovery(session, profile, RediscoveryDecision(trigger=True, reason="test"))
        is True
    )
    sql, params = _compiled(session.statements[0])
    # Still a single guarded UPDATE; the cutoff is simply "now", which no
    # past `last_discovery_at` can fail.
    assert "last_discovery_at" in sql


def test_no_trigger_still_short_circuits_before_any_statement(
    stub_rediscovery_settings, captured_enqueue: list[dict]
) -> None:
    stub_rediscovery_settings()
    session = _FakeSession(rowcount=1)
    assert (
        apply_rediscovery(session, _profile(), RediscoveryDecision(trigger=False, reason=None))
        is False
    )
    assert session.statements == []
    assert captured_enqueue == []


# --------------------------------------------------------------------------
# 3. Backstop, second layer: the per-key daily ceiling in `run_discovery`.
# --------------------------------------------------------------------------

# Importing `app.workers.tasks_strategy` constructs the Celery app (and
# therefore `Settings`), so this runs in a subprocess with a minimal env —
# the `test_discovery_proxy_auth.py` / `test_discovery_early_exit.py`
# precedent.

_CEILING_CHECK = """
import sys
sys.path.insert(0, "apps/workers")

import uuid
from contextlib import contextmanager

from app.workers import tasks_strategy


class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass

    def commit(self):
        pass

    def execute(self, statement):
        raise AssertionError("no query should run once the ceiling refuses")


session = _FakeSession()


@contextmanager
def fake_get_session():
    yield session


tasks_strategy.get_session = fake_get_session
tasks_strategy.set_workspace_context = lambda *a, **k: None
tasks_strategy._auto_run_in_flight = lambda *a, **k: False


def _boom(*a, **k):
    raise AssertionError("discovery ran despite the per-key daily ceiling")


tasks_strategy._select_sample_urls = _boom
tasks_strategy._probe_sample = _boom

# 1. at/over the ceiling: nothing probes, nothing is written -- the ledger
#    this reads must not inflate itself, or the bound would never release.
tasks_strategy._runs_in_trailing_day = lambda *a, **k: 6
tasks_strategy.run_discovery(
    workspace_id=str(uuid.uuid4()),
    competitor_id=str(uuid.uuid4()),
    domain="fqtoners.com",
    url_pattern="fqtoners.com",
    sample_urls=[],
    triggered_by="REDISCOVERY",
)
assert session.added == [], session.added

# 2. below the ceiling: the machine path proceeds as before (proved by
#    reaching the sample-selection seam, which raises).
tasks_strategy._runs_in_trailing_day = lambda *a, **k: 5
try:
    tasks_strategy.run_discovery(
        workspace_id=str(uuid.uuid4()),
        competitor_id=str(uuid.uuid4()),
        domain="fqtoners.com",
        url_pattern="fqtoners.com",
        sample_urls=[],
        triggered_by="AUTO",
    )
except AssertionError as exc:
    assert "discovery ran despite" in str(exc), exc
else:
    raise AssertionError("expected the sample-selection seam to be reached")

print("OK")
"""

_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def test_run_discovery_refuses_a_key_over_its_daily_ceiling() -> None:
    """The bound of last resort. Even if every trigger condition is wrong,
    a single `(competitor, url_pattern)` key can only run
    `STRATEGY_DISCOVERY_MAX_RUNS_PER_KEY_PER_DAY` machine-triggered
    discoveries in 24h — 6, not 1,440."""
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", _CEILING_CHECK],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **_ENV},
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert result.stdout.strip().endswith("OK")
