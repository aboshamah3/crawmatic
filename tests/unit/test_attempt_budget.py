"""`scrape_core.attempt_budget` + the ladder's budget hook (EPA C1/F08).

Nothing bounded what ONE target may spend on PHYSICAL fetches: the job
deadline bounds the job, the defer budget bounds the defer loop, the
fleet/access limits bound the rate. So a target could escalate through
its whole ladder, retry every rung, defer, and do it all again for the
length of a job window — which is exactly where
`crawmatic_attempts_per_valid_fresh_24h` goes wrong, because every one of
those fetches costs money and none of them was ever going to produce a
price.

These tests pin the four properties that bound it, plus the fifth that
keeps the bound from becoming a trap:

1. a method that failed `EXTRACTION_FAILED` on THIS target in THIS
   refresh is never retried with the same method;
2. the 5th physical attempt is refused `ATTEMPT_BUDGET_EXHAUSTED`;
3. a target past its deadline is refused `TARGET_DEADLINE_EXCEEDED`
   WITHOUT a fetch and without charging its counter;
4. the escalation ladder consults the budget before EVERY physical
   attempt — the check lives in the resolver, not in each call site;
5. ~5% of targets whose cheap method is suppressed by a DOMAIN-level
   rule still probe it, so a standing suppression stays falsifiable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app_shared.enums import AccessMethod, ScrapeErrorCode, StrategyMethodProofState
from app_shared.strategy.methods import (
    resolve_method_candidate,
    resolve_next_physical_attempt,
)

from scrape_core.attempt_budget import (
    AttemptBudget,
    SuppressionScope,
    Verdict,
    attempt_budget_key,
    target_suppression_key,
)


class _FakeRedis:
    """Just the four operations `AttemptBudget` uses."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.sets: dict[str, set[str]] = {}
        self.expires: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def get(self, key: str):
        value = self.counters.get(key)
        return None if value is None else str(value)

    def expire(self, key: str, seconds: int) -> None:
        self.expires[key] = seconds

    def sadd(self, key: str, member: str) -> int:
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.add(member)
        return len(bucket) - before

    def sismember(self, key: str, member: str) -> bool:
        return member in self.sets.get(key, set())


class _BrokenRedis:
    def incr(self, key: str) -> int:
        raise ConnectionError("redis down")

    def get(self, key: str):
        raise ConnectionError("redis down")

    def expire(self, key: str, seconds: int) -> None:
        raise ConnectionError("redis down")

    def sadd(self, key: str, member: str) -> int:
        raise ConnectionError("redis down")

    def sismember(self, key: str, member: str) -> bool:
        raise ConnectionError("redis down")


JOB = uuid.uuid4()
MATCH = uuid.uuid4()
FUTURE = datetime.now(timezone.utc) + timedelta(minutes=15)


def _budget(
    redis: object,
    *,
    job: uuid.UUID = JOB,
    match: uuid.UUID = MATCH,
    max_physical: int = 4,
    deadline_at: datetime = FUTURE,
    domain: str | None = None,
    probe: float = 0.0,
) -> AttemptBudget:
    return AttemptBudget(
        redis,
        job_id=job,
        match_id=match,
        max_physical=max_physical,
        deadline_at=deadline_at,
        domain=domain,
        recovery_probe_fraction=probe,
    )


# --- 1. per-target method suppression ---------------------------------------


def test_extraction_failed_method_is_not_retried_on_this_target() -> None:
    redis = _FakeRedis()
    budget = _budget(redis)

    assert budget.try_consume(AccessMethod.DIRECT_HTTP) is Verdict.ALLOWED
    assert budget.suppress_on_outcome(
        AccessMethod.DIRECT_HTTP, ScrapeErrorCode.EXTRACTION_FAILED
    ), "an EXTRACTION_FAILED 200 means this method cannot read this page"

    assert budget.is_suppressed(AccessMethod.DIRECT_HTTP) is True
    assert budget.try_consume(AccessMethod.DIRECT_HTTP) is Verdict.SUPPRESSED
    # A suppressed method costs NOTHING -- it must not eat the budget.
    assert redis.counters[attempt_budget_key(JOB, MATCH)] == 1
    # ...and only that method: the ladder must still be able to escalate.
    assert budget.is_suppressed(AccessMethod.PROXY_HTTP) is False
    assert budget.try_consume(AccessMethod.PROXY_HTTP) is Verdict.ALLOWED


def test_price_not_found_does_not_suppress_the_method() -> None:
    """A listing verdict says nothing about whether the METHOD works."""
    redis = _FakeRedis()
    budget = _budget(redis)
    assert (
        budget.suppress_on_outcome(
            AccessMethod.DIRECT_HTTP, ScrapeErrorCode.PRICE_NOT_FOUND
        )
        is False
    )
    assert budget.is_suppressed(AccessMethod.DIRECT_HTTP) is False
    # Nor does a host verdict.
    assert (
        budget.suppress_on_outcome(AccessMethod.DIRECT_HTTP, ScrapeErrorCode.HTTP_429)
        is False
    )
    assert budget.is_suppressed(AccessMethod.DIRECT_HTTP) is False


def test_suppression_is_per_target() -> None:
    redis = _FakeRedis()
    mine = _budget(redis)
    other = _budget(redis, match=uuid.uuid4())
    mine.suppress(AccessMethod.DIRECT_HTTP)
    assert mine.is_suppressed(AccessMethod.DIRECT_HTTP) is True
    assert other.is_suppressed(AccessMethod.DIRECT_HTTP) is False
    assert target_suppression_key(JOB, MATCH) in redis.sets


# --- 2. the physical attempt budget -----------------------------------------


def test_fifth_physical_attempt_is_refused_budget_exhausted() -> None:
    redis = _FakeRedis()
    budget = _budget(redis, max_physical=4)

    verdicts = [budget.try_consume(AccessMethod.PROXY_HTTP) for _ in range(4)]
    assert verdicts == [Verdict.ALLOWED] * 4, verdicts

    fifth = budget.try_consume(AccessMethod.PROXY_HTTP)
    assert fifth is Verdict.BUDGET_EXHAUSTED
    assert fifth.error_code is ScrapeErrorCode.ATTEMPT_BUDGET_EXHAUSTED
    assert fifth.terminal is True
    # ...and stays refused (idempotent, so a redispatch cannot reset it).
    assert budget.try_consume(AccessMethod.PLAYWRIGHT_PROXY) is Verdict.BUDGET_EXHAUSTED


def test_budget_is_per_target_and_per_job() -> None:
    redis = _FakeRedis()
    for _ in range(5):
        _budget(redis).try_consume(AccessMethod.PROXY_HTTP)
    assert _budget(redis).try_consume(AccessMethod.PROXY_HTTP) is Verdict.BUDGET_EXHAUSTED

    # A sibling target and a later refresh both start fresh.
    assert (
        _budget(redis, match=uuid.uuid4()).try_consume(AccessMethod.PROXY_HTTP)
        is Verdict.ALLOWED
    )
    assert (
        _budget(redis, job=uuid.uuid4()).try_consume(AccessMethod.PROXY_HTTP)
        is Verdict.ALLOWED
    )


def test_redis_outage_fails_open() -> None:
    """A refusal is PERSISTED EVIDENCE; never fabricate it from an outage."""
    budget = _budget(_BrokenRedis())
    assert budget.try_consume(AccessMethod.PROXY_HTTP) is Verdict.ALLOWED
    assert budget.is_suppressed(AccessMethod.PROXY_HTTP) is False
    budget.suppress(AccessMethod.PROXY_HTTP)  # must not raise


def test_redis_outage_does_not_lift_the_deadline() -> None:
    """The bound that actually caps spend needs no Redis at all."""
    budget = _budget(
        _BrokenRedis(), deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    assert budget.try_consume(AccessMethod.PROXY_HTTP) is Verdict.DEADLINE_EXCEEDED


# --- 3. the per-target deadline ---------------------------------------------


def test_target_past_deadline_is_refused_without_a_fetch() -> None:
    redis = _FakeRedis()
    budget = _budget(
        redis, deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )

    verdict = budget.try_consume(AccessMethod.DIRECT_HTTP)
    assert verdict is Verdict.DEADLINE_EXCEEDED
    assert verdict.error_code is ScrapeErrorCode.TARGET_DEADLINE_EXCEEDED
    assert verdict.terminal is True
    # No fetch happened, so nothing may be charged: the counter is
    # evidence about attempts actually made.
    assert redis.counters == {}


def test_deadline_is_checked_before_suppression_and_budget() -> None:
    redis = _FakeRedis()
    past = _budget(redis, deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    past.suppress(AccessMethod.DIRECT_HTTP)
    # Even a suppressed method reports the DEADLINE -- the target is over,
    # and "try the next rung" would be a lie.
    assert past.try_consume(AccessMethod.DIRECT_HTTP) is Verdict.DEADLINE_EXCEEDED


# --- 5. the sampled recovery probe ------------------------------------------


def test_domain_suppression_is_probed_for_about_five_percent_of_targets() -> None:
    """A standing rule nobody re-tests can never be lifted."""
    redis = _FakeRedis()
    redis.sets["budgetsupp:domain:noon.com"] = {AccessMethod.DIRECT_HTTP.value}

    probed = 0
    total = 4000
    for _ in range(total):
        budget = _budget(
            redis, match=uuid.uuid4(), domain="noon.com", probe=0.05
        )
        if not budget.is_suppressed(AccessMethod.DIRECT_HTTP):
            probed += 1

    rate = probed / total
    assert 0.03 <= rate <= 0.07, f"expected ~5% recovery probes, got {rate:.3%}"


def test_recovery_probe_is_deterministic_per_target() -> None:
    """Two processes racing the same target must not disagree."""
    redis = _FakeRedis()
    redis.sets["budgetsupp:domain:noon.com"] = {AccessMethod.DIRECT_HTTP.value}
    match = uuid.uuid4()
    answers = {
        _budget(redis, match=match, domain="noon.com", probe=0.05).is_suppressed(
            AccessMethod.DIRECT_HTTP
        )
        for _ in range(10)
    }
    assert len(answers) == 1, answers


def test_target_suppression_is_never_probed() -> None:
    """A fact about THIS fetch is not a standing rule to re-test."""
    redis = _FakeRedis()
    budget = _budget(redis, domain="noon.com", probe=1.0)
    budget.suppress(AccessMethod.DIRECT_HTTP, scope=SuppressionScope.TARGET)
    assert budget.is_suppressed(AccessMethod.DIRECT_HTTP) is True


def test_domain_suppression_without_a_domain_is_inert() -> None:
    redis = _FakeRedis()
    budget = _budget(redis)  # no domain
    budget.suppress(AccessMethod.DIRECT_HTTP, scope=SuppressionScope.DOMAIN)
    assert redis.sets == {}, "must never guess a domain"
    assert budget.is_suppressed(AccessMethod.DIRECT_HTTP) is False


# --- 4. the escalation ladder consults the budget ---------------------------


@dataclass
class _Method:
    access_method: AccessMethod
    priority: int
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    enter_on: list = field(default_factory=lambda: ["*"])
    fallback_on: list = field(default_factory=lambda: ["*"])
    enabled: bool = True
    proof_state: StrategyMethodProofState = StrategyMethodProofState.PROVEN
    cooldown_until: datetime | None = None
    next_canary_at: datetime | None = None
    retired_at: datetime | None = None


def _ladder() -> list[_Method]:
    return [
        _Method(AccessMethod.DIRECT_HTTP, 0),
        _Method(AccessMethod.PROXY_HTTP, 1),
        _Method(AccessMethod.PLAYWRIGHT_PROXY, 2),
    ]


def test_ladder_charges_the_budget_for_the_method_it_returns() -> None:
    redis = _FakeRedis()
    budget = _budget(redis)
    ladder = _ladder()

    decision = resolve_next_physical_attempt(ladder, budget=budget)
    assert decision.selection is not None
    assert decision.selection.method.access_method is AccessMethod.DIRECT_HTTP
    assert redis.counters[attempt_budget_key(JOB, MATCH)] == 1


def test_ladder_skips_a_suppressed_method_and_escalates() -> None:
    redis = _FakeRedis()
    budget = _budget(redis)
    budget.suppress(AccessMethod.DIRECT_HTTP)

    decision = resolve_next_physical_attempt(_ladder(), budget=budget)
    assert decision.refusal is None
    assert decision.selection is not None
    assert decision.selection.method.access_method is AccessMethod.PROXY_HTTP
    # The skipped rung was free.
    assert redis.counters[attempt_budget_key(JOB, MATCH)] == 1


def test_ladder_refuses_once_the_budget_is_spent() -> None:
    redis = _FakeRedis()
    budget = _budget(redis, max_physical=1)
    assert resolve_next_physical_attempt(_ladder(), budget=budget).selection is not None

    decision = resolve_next_physical_attempt(_ladder(), budget=budget)
    assert decision.selection is None
    assert decision.refusal is ScrapeErrorCode.ATTEMPT_BUDGET_EXHAUSTED


def test_ladder_refuses_a_target_past_its_deadline_without_selecting() -> None:
    redis = _FakeRedis()
    budget = _budget(
        redis, deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    decision = resolve_next_physical_attempt(_ladder(), budget=budget)
    assert decision.selection is None
    assert decision.refusal is ScrapeErrorCode.TARGET_DEADLINE_EXCEEDED
    assert redis.counters == {}, "no fetch, so no charge"


def test_ladder_without_a_budget_is_unchanged() -> None:
    """Every pre-C1 caller keeps its exact behaviour."""
    ladder = _ladder()
    selection = resolve_method_candidate(ladder)
    assert selection is not None
    assert selection.method.access_method is AccessMethod.DIRECT_HTTP
    assert selection.attempt_ordinal == 1
