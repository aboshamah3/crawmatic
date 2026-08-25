"""Unit tests for `scripts/classify_match_set.py` (EPA A6, 2026-08-25).

Covers only the PURE classification core (`classify_match`) — no network,
no database — with synthetic `MatchFacts` fixtures. The rule set:

1. INVALID_IDENTITY: an `INVALID_IDENTITY_DOMAINS` match (S-Tech /
   `stech.ink`) with a `competitor_variant_identifier` AND at least one
   `NOT_LISTED` observation — the 2026-08-24 canary's false-negative
   identity bug, not a real absence.
2. CONFIRMED_DELISTED: >= 2 validated-absence (`NOT_LISTED`) fetches
   spanning >= 24h. Never from a single fetch (validated absence OR a
   raw 404/410 alone) — one fetch always yields UNKNOWN + second_pass.
3. ACTIVE: a recent (<= `recent_days`), successful, comparable
   observation — recorded provisional.
4. UNKNOWN: everything else (never observed, stale, or only
   inconclusive/non-validated errors).

Also covers the live-fetch enrichment helper (`enrich_never_observed_via_
fetch`) and `LiveFetcher`'s request cap / domain-precedent gating, using
an injected fake opener — no real network call.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# `scripts/` has no __init__.py / installed entry point -- match the
# sys.path convention `tests/unit/test_backfill_daily_rollups.py` /
# `tests/unit/test_seed_bootstrap.py` use to import `scripts.<module>`.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.classify_match_set import (  # noqa: E402
    CLASSIFIER_VERSION,
    SECOND_PASS_AFTER,
    LiveFetcher,
    MatchFacts,
    classify_match,
    domain_has_direct_precedent,
    enrich_never_observed_via_fetch,
)

NOW = datetime(2026, 8, 25, 16, 0, 0, tzinfo=timezone.utc)


def _facts(**overrides) -> MatchFacts:
    defaults = dict(
        match_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        competitor_domain="example.com",
        competitor_url="https://example.com/product/1",
        competitor_variant_identifier=None,
        match_status="ACTIVE",
        latest_observation_at=None,
        latest_observation_success=None,
        latest_observation_comparable=None,
        latest_observation_error_code=None,
        not_listed_at=(),
    )
    defaults.update(overrides)
    return MatchFacts(**defaults)


# --- ACTIVE --------------------------------------------------------------


def test_recent_successful_comparable_observation_is_active():
    facts = _facts(
        latest_observation_at=NOW - timedelta(days=2),
        latest_observation_success=True,
        latest_observation_comparable=True,
    )
    result = classify_match(facts, now=NOW)
    assert result.state == "ACTIVE"
    assert result.evidence["provisional"] is True
    assert result.evidence["classifier_version"] == CLASSIFIER_VERSION
    assert result.second_pass is False


def test_active_requires_comparable_not_just_success():
    facts = _facts(
        latest_observation_at=NOW - timedelta(days=1),
        latest_observation_success=True,
        latest_observation_comparable=False,  # e.g. CURRENCY_MISMATCH
    )
    result = classify_match(facts, now=NOW)
    assert result.state == "UNKNOWN"


def test_stale_success_outside_recent_window_is_unknown():
    facts = _facts(
        latest_observation_at=NOW - timedelta(days=30),
        latest_observation_success=True,
        latest_observation_comparable=True,
    )
    result = classify_match(facts, now=NOW, recent_days=14)
    assert result.state == "UNKNOWN"
    assert result.evidence["reason"] == "no_decisive_recent_signal"


def test_never_observed_is_unknown_with_reason():
    facts = _facts(latest_observation_at=None)
    result = classify_match(facts, now=NOW)
    assert result.state == "UNKNOWN"
    assert result.evidence["reason"] == "never_observed"


def test_recent_failed_observation_is_unknown_not_active():
    facts = _facts(
        latest_observation_at=NOW - timedelta(hours=1),
        latest_observation_success=False,
        latest_observation_comparable=True,
        latest_observation_error_code="TIMEOUT",
    )
    result = classify_match(facts, now=NOW)
    assert result.state == "UNKNOWN"
    assert result.evidence["latest_observation_error_code"] == "TIMEOUT"


# --- CONFIRMED_DELISTED / single-fetch UNKNOWN ----------------------------


def test_single_validated_absence_fetch_is_unknown_never_confirmed():
    """The task brief is explicit: never emit CONFIRMED_DELISTED from one fetch."""
    facts = _facts(not_listed_at=(NOW - timedelta(hours=2),))
    result = classify_match(facts, now=NOW)
    assert result.state == "UNKNOWN"
    assert result.second_pass is True
    assert result.evidence["second_pass_after"] == SECOND_PASS_AFTER
    assert result.evidence["validated_absence_fetch_count"] == 1


def test_two_validated_absence_fetches_under_24h_apart_still_unknown():
    facts = _facts(
        not_listed_at=(
            NOW - timedelta(hours=20),
            NOW - timedelta(hours=1),
        )
    )
    result = classify_match(facts, now=NOW)
    assert result.state == "UNKNOWN"
    assert result.second_pass is True


def test_two_validated_absence_fetches_24h_plus_apart_confirms_delisted():
    facts = _facts(
        not_listed_at=(
            NOW - timedelta(hours=30),
            NOW - timedelta(hours=1),
        )
    )
    result = classify_match(facts, now=NOW)
    assert result.state == "CONFIRMED_DELISTED"
    assert result.second_pass is False
    assert result.evidence["validated_absence_fetch_count"] == 2
    assert result.evidence["span_hours"] == pytest.approx(29.0, abs=0.01)


def test_exactly_24h_apart_confirms_delisted_boundary_inclusive():
    facts = _facts(
        not_listed_at=(
            NOW - timedelta(hours=24),
            NOW,
        )
    )
    result = classify_match(facts, now=NOW)
    assert result.state == "CONFIRMED_DELISTED"


def test_a_single_raw_404_with_no_not_listed_history_is_never_delisted():
    """A raw HTTP_404 is deliberately NOT validated absence in this
    classifier — only NOT_LISTED (recorded via not_listed_at) counts."""
    facts = _facts(
        latest_observation_at=NOW - timedelta(hours=1),
        latest_observation_success=False,
        latest_observation_comparable=True,
        latest_observation_error_code="HTTP_404",
    )
    result = classify_match(facts, now=NOW)
    assert result.state == "UNKNOWN"
    assert result.second_pass is False


# --- INVALID_IDENTITY (S-Tech) --------------------------------------------


def test_stech_with_identifier_and_not_listed_is_invalid_identity():
    facts = _facts(
        competitor_domain="stech.ink",
        competitor_variant_identifier="pump-ink-500ml-black",
        not_listed_at=(NOW - timedelta(hours=3),),
    )
    result = classify_match(facts, now=NOW)
    assert result.state == "INVALID_IDENTITY"
    assert result.second_pass is False
    assert result.evidence["competitor_variant_identifier"] == "pump-ink-500ml-black"
    assert result.evidence["not_listed_observation_count"] == 1


def test_stech_without_identifier_falls_through_to_normal_rules():
    """No competitor_variant_identifier -> the false-negative bug doesn't
    apply (per the 2026-08-24 finding: the 4 stech rows without an
    identifier succeeded) -- ordinary single-fetch UNKNOWN applies."""
    facts = _facts(
        competitor_domain="stech.ink",
        competitor_variant_identifier=None,
        not_listed_at=(NOW - timedelta(hours=3),),
    )
    result = classify_match(facts, now=NOW)
    assert result.state == "UNKNOWN"
    assert result.second_pass is True


def test_stech_with_identifier_but_no_not_listed_history_not_invalid_identity():
    facts = _facts(
        competitor_domain="stech.ink",
        competitor_variant_identifier="pump-ink-500ml-black",
        latest_observation_at=NOW - timedelta(days=1),
        latest_observation_success=True,
        latest_observation_comparable=True,
    )
    result = classify_match(facts, now=NOW)
    assert result.state == "ACTIVE"


def test_non_stech_domain_with_identifier_and_not_listed_is_not_invalid_identity():
    """The INVALID_IDENTITY rule is scoped to registered domains only —
    a lookalike identifier shape on a different competitor never
    triggers it."""
    facts = _facts(
        competitor_domain="extra.com",
        competitor_variant_identifier="some-handle-slug",
        not_listed_at=(NOW - timedelta(hours=1),),
    )
    result = classify_match(facts, now=NOW)
    assert result.state == "UNKNOWN"
    assert result.second_pass is True


def test_invalid_identity_takes_priority_over_delisted_even_with_two_fetches():
    """Even two 24h-apart NOT_LISTED fetches on an INVALID_IDENTITY match
    must never be read as CONFIRMED_DELISTED -- the identity bug
    explanation always wins."""
    facts = _facts(
        competitor_domain="stech.ink",
        competitor_variant_identifier="pump-ink-500ml-black",
        not_listed_at=(NOW - timedelta(hours=48), NOW - timedelta(hours=1)),
    )
    result = classify_match(facts, now=NOW)
    assert result.state == "INVALID_IDENTITY"


# --- LiveFetcher / enrichment (no real network) ---------------------------


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self, *_args):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Injected in place of `LiveFetcher._opener` — no real network I/O."""

    def __init__(self, responses: dict[str, tuple[int, bytes]]):
        self._responses = responses
        self.calls: list[str] = []

    def open(self, request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.calls.append(url)
        if url not in self._responses:
            raise LookupError(f"no fake response registered for {url}")
        status, body = self._responses[url]
        if status >= 400:
            import urllib.error

            raise urllib.error.HTTPError(url, status, "err", {}, None)
        return _FakeResponse(status, body)


def test_live_fetcher_budget_hard_cap_never_exceeded():
    fetcher = LiveFetcher(budget=2)
    fetcher._opener = _FakeOpener(
        {
            "https://example.com/robots.txt": (404, b""),
            "https://example.com/p1": (200, b"x" * 300),
            "https://example.com/p2": (200, b"x" * 300),
        }
    )
    status1, _, outcome1 = fetcher.fetch("https://example.com/p1")
    assert outcome1 == "fetched"
    assert status1 == 200
    # Budget is now exhausted (1 robots.txt request + 1 target request == 2).
    status2, _, outcome2 = fetcher.fetch("https://example.com/p2")
    assert outcome2 == "budget_exhausted"
    assert fetcher.requests_made == 2


def test_domain_has_direct_precedent_gating():
    known = {"extra.com", "noon.com"}
    assert domain_has_direct_precedent(known, "extra.com") is True
    assert domain_has_direct_precedent(known, "proxy-only.example") is False


def test_enrich_skips_domains_with_no_direct_precedent():
    facts = _facts(competitor_domain="proxy-only.example", competitor_url="https://proxy-only.example/p")
    fetcher = LiveFetcher(budget=10)
    fetcher._opener = _FakeOpener({})
    updated = enrich_never_observed_via_fetch(
        [facts], fetcher=fetcher, domains_with_direct_success=set(), now=NOW
    )
    assert updated == {}
    assert fetcher.requests_made == 0


def test_enrich_success_fetch_yields_active_via_reclassification():
    facts = _facts(competitor_domain="extra.com", competitor_url="https://extra.com/p1")
    fetcher = LiveFetcher(budget=10)
    fetcher._opener = _FakeOpener(
        {
            "https://extra.com/robots.txt": (404, b""),
            "https://extra.com/p1": (200, b"x" * 300),
        }
    )
    updated = enrich_never_observed_via_fetch(
        [facts], fetcher=fetcher, domains_with_direct_success={"extra.com"}, now=NOW
    )
    assert facts.match_id in updated
    result = classify_match(updated[facts.match_id], now=NOW)
    assert result.state == "ACTIVE"


def test_enrich_404_fetch_yields_single_validated_absence_still_unknown():
    facts = _facts(competitor_domain="extra.com", competitor_url="https://extra.com/gone")
    fetcher = LiveFetcher(budget=10)
    fetcher._opener = _FakeOpener(
        {
            "https://extra.com/robots.txt": (404, b""),
            "https://extra.com/gone": (404, b""),
        }
    )
    updated = enrich_never_observed_via_fetch(
        [facts], fetcher=fetcher, domains_with_direct_success={"extra.com"}, now=NOW
    )
    assert facts.match_id in updated
    result = classify_match(updated[facts.match_id], now=NOW)
    assert result.state == "UNKNOWN"
    assert result.second_pass is True
