"""The versioned domain strategy (EPA C4, F08 / plan §11 item 2).

`domain_playbooks` gained five columns: `strategy_version`, `cheap_path`,
`fallback_path`, `fallback_cap_per_refresh`, `recovery_probe_fraction`.
This file pins what the ladder does with them, plus the two artifacts
whose absence would make the canary gate unrunnable (the labeled
fixtures, and the canary script's refusal to run without a spend
ceiling).

Five properties:

1. **Every attempt is stamped.** Selections, refusals and dead ends all
   carry the `strategy_version` they were decided under, so "which
   strategy produced this price" survives an unrelated approval bumping
   `profile_version`.
2. **`fallback_cap_per_refresh=1` allows exactly one browser fallback per
   target per refresh** — the plan's own acceptance test. The count lives
   on the per-target budget, because that is the only object that is both
   per-target and per-refresh.
3. **`cheap_path` is a hint, never an override.** It orders the ladder
   only when nothing else pins the start.
4. **A bad curated value degrades to "no hint"**, loudly, instead of
   failing a seed or silently matching nothing.
5. **The gate's artifacts exist and refuse correctly**: >= 30 labeled
   offers per domain, and `run_domain_canary.py` will not run without
   `--max-usd`.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from app_shared.enums import AccessMethod, ScrapeErrorCode, StrategyMethodProofState
from app_shared.strategy.methods import (
    DEFAULT_STRATEGY_VERSION,
    PlaybookStrategy,
    resolve_next_physical_attempt,
)

from scrape_core.attempt_budget import AttemptBudget, fallback_attempts_key

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
LABELED_OFFERS = REPO_ROOT / "tests" / "fixtures" / "labeled_offers"
CANARY_SCRIPT = REPO_ROOT / "scripts" / "run_domain_canary.py"


# --- fakes -------------------------------------------------------------------


@dataclass
class _Method:
    """A `StrategyMethodLike` with no ORM behind it."""

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


@dataclass
class _PlaybookRow:
    """A `domain_playbooks` row's C4 columns, without SQLAlchemy."""

    domain: str = "noon.com"
    strategy_version: int = 7
    cheap_path: str | None = "PROXY_HTTP"
    fallback_path: str | None = "PLAYWRIGHT_PROXY"
    fallback_cap_per_refresh: int | None = 1
    recovery_probe_fraction: object | None = None


class _FakeRedis:
    """The five operations `AttemptBudget` uses."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.sets: dict[str, set[str]] = {}

    def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def get(self, key: str):
        value = self.counters.get(key)
        return None if value is None else str(value)

    def expire(self, key: str, seconds: int) -> None:
        return None

    def sadd(self, key: str, member: str) -> int:
        self.sets.setdefault(key, set()).add(member)
        return 1

    def sismember(self, key: str, member: str) -> bool:
        return member in self.sets.get(key, set())


JOB = uuid.uuid4()


def _ladder() -> list[_Method]:
    return [
        _Method(AccessMethod.DIRECT_HTTP, 0),
        _Method(AccessMethod.PROXY_HTTP, 1),
        _Method(AccessMethod.PLAYWRIGHT_PROXY, 2),
    ]


def _budget(redis: object, *, match: uuid.UUID) -> AttemptBudget:
    return AttemptBudget(
        redis,
        job_id=JOB,
        match_id=match,
        max_physical=99,  # not the bound under test here
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=1),
        domain="noon.com",
    )


# --- 1. every decision is stamped with its strategy version ------------------


def test_selection_carries_the_playbook_strategy_version() -> None:
    playbook = PlaybookStrategy.from_row(_PlaybookRow())
    decision = resolve_next_physical_attempt(_ladder(), playbook=playbook)

    assert decision.selection is not None
    assert decision.selection.strategy_version == 7
    assert decision.strategy_version == 7


def test_no_playbook_stamps_version_one_not_none() -> None:
    """"No playbook" is version 1, not an unknown.

    Every attempt ran under *some* strategy; "the ladder's own default
    order" is honestly named version 1. A `None` would force every reader
    of `request_attempts` to handle a third case meaning the same thing.
    """
    decision = resolve_next_physical_attempt(_ladder())
    assert decision.strategy_version == DEFAULT_STRATEGY_VERSION == 1
    assert decision.selection is not None
    assert decision.selection.strategy_version == 1


def test_refusals_and_dead_ends_are_stamped_too() -> None:
    """"Version 7 refused every target" must be as answerable as a price."""
    playbook = PlaybookStrategy.from_row(_PlaybookRow())

    # Dead end: an empty ladder.
    assert resolve_next_physical_attempt([], playbook=playbook).strategy_version == 7

    # Terminal refusal: a budget with nothing left.
    exhausted = AttemptBudget(
        _FakeRedis(),
        job_id=JOB,
        match_id=uuid.uuid4(),
        max_physical=0,
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    decision = resolve_next_physical_attempt(
        _ladder(), budget=exhausted, playbook=playbook
    )
    assert decision.refusal is ScrapeErrorCode.ATTEMPT_BUDGET_EXHAUSTED
    assert decision.strategy_version == 7
    assert decision.selection is None


# --- 2. fallback_cap_per_refresh = 1 -----------------------------------------


def test_cap_of_one_allows_exactly_one_browser_fallback_per_target() -> None:
    """The plan's acceptance test, stated literally.

    One target, one refresh, `fallback_cap_per_refresh=1`: the browser
    rung may be reached ONCE. The second escalation that would reach it
    skips past instead — capped is *not* terminal, a cheaper rung further
    down may still run.
    """
    redis = _FakeRedis()
    match = uuid.uuid4()
    budget = _budget(redis, match=match)
    playbook = PlaybookStrategy.from_row(_PlaybookRow(fallback_cap_per_refresh=1))
    ladder = _ladder()
    browser = ladder[-1]

    # Escalate straight onto the browser rung (cursor on PROXY_HTTP).
    first = resolve_next_physical_attempt(
        ladder,
        current_method_id=ladder[1].id,
        outcome=ScrapeErrorCode.BLOCKED,
        budget=budget,
        playbook=playbook,
    )
    assert first.selection is not None
    assert first.selection.method.id == browser.id
    assert first.selection.is_fallback_path is True
    assert redis.counters[fallback_attempts_key(JOB, match)] == 1

    # The SAME target, same refresh, escalating again: the cap is spent.
    second = resolve_next_physical_attempt(
        ladder,
        current_method_id=ladder[1].id,
        outcome=ScrapeErrorCode.BLOCKED,
        budget=budget,
        playbook=playbook,
    )
    assert second.selection is None, "exactly one browser fallback per refresh"
    assert second.refusal is None, "a spent cap is not terminal for the target"
    assert redis.counters[fallback_attempts_key(JOB, match)] == 1


def test_the_cap_is_per_target_not_per_domain() -> None:
    """A second target on the same domain gets its own allowance."""
    redis = _FakeRedis()
    playbook = PlaybookStrategy.from_row(_PlaybookRow(fallback_cap_per_refresh=1))
    ladder = _ladder()

    for _ in range(2):
        match = uuid.uuid4()
        decision = resolve_next_physical_attempt(
            ladder,
            current_method_id=ladder[1].id,
            outcome=ScrapeErrorCode.BLOCKED,
            budget=_budget(redis, match=match),
            playbook=playbook,
        )
        assert decision.selection is not None
        assert decision.selection.method.access_method is AccessMethod.PLAYWRIGHT_PROXY


def test_cap_of_zero_never_takes_the_fallback_path() -> None:
    redis = _FakeRedis()
    match = uuid.uuid4()
    playbook = PlaybookStrategy.from_row(_PlaybookRow(fallback_cap_per_refresh=0))
    ladder = _ladder()

    decision = resolve_next_physical_attempt(
        ladder,
        current_method_id=ladder[1].id,
        outcome=ScrapeErrorCode.BLOCKED,
        budget=_budget(redis, match=match),
        playbook=playbook,
    )
    assert decision.selection is None
    assert fallback_attempts_key(JOB, match) not in redis.counters


def test_null_cap_is_uncapped_the_pre_c4_behaviour() -> None:
    """An unseeded row changes nothing — that is what makes it safe to ship."""
    redis = _FakeRedis()
    match = uuid.uuid4()
    playbook = PlaybookStrategy.from_row(
        _PlaybookRow(fallback_cap_per_refresh=None)
    )
    ladder = _ladder()

    for _ in range(3):
        decision = resolve_next_physical_attempt(
            ladder,
            current_method_id=ladder[1].id,
            outcome=ScrapeErrorCode.BLOCKED,
            budget=_budget(redis, match=match),
            playbook=playbook,
        )
        assert decision.selection is not None
    assert fallback_attempts_key(JOB, match) not in redis.counters


def test_a_capped_fallback_is_not_charged_when_the_budget_refuses() -> None:
    """Order matters: the physical-attempt gate runs first.

    A target refused on its deadline must not have burned the one browser
    attempt it never got to make.
    """
    redis = _FakeRedis()
    match = uuid.uuid4()
    past_deadline = AttemptBudget(
        redis,
        job_id=JOB,
        match_id=match,
        max_physical=4,
        deadline_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    ladder = _ladder()
    decision = resolve_next_physical_attempt(
        ladder,
        current_method_id=ladder[1].id,
        outcome=ScrapeErrorCode.BLOCKED,
        budget=past_deadline,
        playbook=PlaybookStrategy.from_row(_PlaybookRow(fallback_cap_per_refresh=1)),
    )
    assert decision.refusal is ScrapeErrorCode.TARGET_DEADLINE_EXCEEDED
    assert fallback_attempts_key(JOB, match) not in redis.counters


def test_a_gate_without_the_counter_makes_the_cap_inert_not_wrong() -> None:
    """A pre-C4 gate reports zero used and cannot be charged.

    Refusing on an unknown count would silently disable the expensive rung
    for every caller that has not adopted the counter — a far worse
    failure than an uncounted attempt.
    """

    class _OldGate:
        def is_suppressed(self, method) -> bool:
            return False

        def try_consume(self, method):
            return type("V", (), {"error_code": None, "allowed": True})()

    ladder = _ladder()
    for _ in range(3):
        decision = resolve_next_physical_attempt(
            ladder,
            current_method_id=ladder[1].id,
            outcome=ScrapeErrorCode.BLOCKED,
            budget=_OldGate(),
            playbook=PlaybookStrategy.from_row(_PlaybookRow(fallback_cap_per_refresh=1)),
        )
        assert decision.selection is not None


# --- 3. cheap_path is a hint, never an override -----------------------------


def test_cheap_path_is_tried_first_when_nothing_else_pins_the_start() -> None:
    playbook = PlaybookStrategy.from_row(_PlaybookRow(cheap_path="PROXY_HTTP"))
    decision = resolve_next_physical_attempt(_ladder(), playbook=playbook)

    assert decision.selection is not None
    assert decision.selection.method.access_method is AccessMethod.PROXY_HTTP


def test_an_explicit_preferred_method_beats_the_cheap_path() -> None:
    """The playbook has always been a starting hint, never an override."""
    ladder = _ladder()
    playbook = PlaybookStrategy.from_row(_PlaybookRow(cheap_path="PROXY_HTTP"))
    decision = resolve_next_physical_attempt(
        ladder, preferred_method_id=ladder[2].id, playbook=playbook
    )
    assert decision.selection is not None
    assert decision.selection.method.id == ladder[2].id


def test_a_durable_cursor_beats_the_cheap_path() -> None:
    """Escalation continues from the cursor; it never restarts at cheap."""
    ladder = _ladder()
    playbook = PlaybookStrategy.from_row(_PlaybookRow(cheap_path="PLAYWRIGHT_PROXY"))
    decision = resolve_next_physical_attempt(
        ladder,
        current_method_id=ladder[0].id,
        outcome=ScrapeErrorCode.BLOCKED,
        playbook=playbook,
    )
    assert decision.selection is not None
    assert decision.selection.method.id == ladder[1].id


def test_no_cheap_path_keeps_plain_priority_order() -> None:
    playbook = PlaybookStrategy.from_row(_PlaybookRow(cheap_path=None))
    decision = resolve_next_physical_attempt(_ladder(), playbook=playbook)
    assert decision.selection is not None
    assert decision.selection.method.access_method is AccessMethod.DIRECT_HTTP


# --- 4. reading the row ------------------------------------------------------


def test_from_row_of_none_is_none() -> None:
    assert PlaybookStrategy.from_row(None) is None


def test_a_row_predating_the_migration_yields_defaults() -> None:
    """`from_row` reads structurally, so an old row is not a crash."""

    class _OldRow:
        domain = "noon.com"

    strategy = PlaybookStrategy.from_row(_OldRow())
    assert strategy is not None
    assert strategy.strategy_version == DEFAULT_STRATEGY_VERSION
    assert strategy.cheap_path is None
    assert strategy.fallback_path is None
    assert strategy.fallback_cap_per_refresh is None
    assert strategy.caps_fallback() is False


def test_an_unknown_access_method_degrades_to_no_hint(caplog) -> None:
    """Curated operator data must degrade, not fail an INSERT or match nothing."""
    strategy = PlaybookStrategy.from_row(
        _PlaybookRow(cheap_path="TELEPATHY", fallback_path="TELEPATHY")
    )
    assert strategy is not None
    assert strategy.cheap_path is None
    assert strategy.fallback_path is None
    assert strategy.caps_fallback() is False


def test_a_negative_cap_is_clamped_to_never() -> None:
    strategy = PlaybookStrategy.from_row(_PlaybookRow(fallback_cap_per_refresh=-3))
    assert strategy is not None
    assert strategy.fallback_cap_per_refresh == 0


def test_recovery_probe_fraction_is_coerced_from_numeric() -> None:
    """The column is `NUMERIC`; the budget takes a `float`."""
    from decimal import Decimal

    strategy = PlaybookStrategy.from_row(
        _PlaybookRow(recovery_probe_fraction=Decimal("0.05"))
    )
    assert strategy is not None
    assert strategy.recovery_probe_fraction == pytest.approx(0.05)


# --- 5. the gate's artifacts -------------------------------------------------


@pytest.mark.parametrize("name", ["noon", "stech", "amazon"])
def test_labeled_offer_fixture_has_at_least_thirty_labels(name: str) -> None:
    """The canary is only as good as its label set."""
    path = LABELED_OFFERS / f"{name}.jsonl"
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(rows) >= 30, f"{path} holds only {len(rows)} labels"

    required = {"price", "currency", "seller", "variant", "availability"}
    for row in rows:
        assert required <= set(row), f"{path}: a label is missing {required - set(row)}"
        assert row["match_id"], "every label must be joinable to a result"

    match_ids = [row["match_id"] for row in rows]
    assert len(set(match_ids)) == len(match_ids), "duplicate match_id in labels"


def test_canary_refuses_to_run_without_max_usd() -> None:
    """A money-capable script with an implicit budget can cost anything."""
    completed = subprocess.run(
        [
            sys.executable,
            str(CANARY_SCRIPT),
            "--domain",
            "noon.com",
            "--labels",
            str(LABELED_OFFERS / "noon.jsonl"),
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "--max-usd" in completed.stderr


def test_canary_refuses_a_live_run_without_the_owner_gate() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(CANARY_SCRIPT),
            "--domain",
            "noon.com",
            "--max-usd",
            "1.00",
            "--labels",
            str(LABELED_OFFERS / "noon.jsonl"),
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 3
    assert "OWNER-GO:canary:noon.com" in completed.stderr


def test_canary_scoring_is_offline_and_agrees_with_its_own_labels() -> None:
    """A self-comparison must score 100 % — the scorer's own sanity check."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import run_domain_canary as canary
    finally:
        sys.path.pop(0)

    labels = canary.read_jsonl(LABELED_OFFERS / "stech.jsonl")
    score = canary.score_results(labels, labels, domain="stech.ink")

    assert score.passed is True
    assert score.missing_results == ()
    for name in canary.SCORED_FIELDS:
        assert score.fields[name].agreement == 1.0


def test_canary_fails_when_a_label_got_no_result() -> None:
    """20 of 30 answered is not a pass; it is two thirds of a canary."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import run_domain_canary as canary
    finally:
        sys.path.pop(0)

    labels = canary.read_jsonl(LABELED_OFFERS / "stech.jsonl")
    score = canary.score_results(labels, labels[:5], domain="stech.ink")

    assert score.passed is False
    assert len(score.missing_results) == len(labels) - 5


def test_canary_fails_below_the_ninety_five_percent_bar() -> None:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import run_domain_canary as canary
    finally:
        sys.path.pop(0)

    labels = canary.read_jsonl(LABELED_OFFERS / "stech.jsonl")
    results = [dict(row) for row in labels]
    # Break 3 of 30 prices -> 90 % agreement, under the bar.
    for row in results[:3]:
        row["price"] = "0.0001"
    score = canary.score_results(labels, results, domain="stech.ink")

    assert score.fields["price"].agreement == pytest.approx(27 / 30)
    assert score.fields["price"].meets_bar is False
    assert score.passed is False
    # ...and the currency/availability columns are untouched by it.
    assert score.fields["currency"].meets_bar is True


def test_canary_price_comparison_is_numeric_not_textual() -> None:
    """`"349.2500"` and `349.25` are the same price."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import run_domain_canary as canary
    finally:
        sys.path.pop(0)

    label = {
        "match_id": "m1",
        "price": "349.2500",
        "currency": "SAR",
        "availability": "IN_STOCK",
    }
    result = {
        "match_id": "m1",
        "price": 349.25,
        "currency": "sar",
        "availability": "in_stock",
    }
    score = canary.score_results([label], [result], domain="noon.com")
    assert score.passed is True
