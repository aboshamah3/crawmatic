"""A2/F02 — the profile-regex hard execution deadline.

The property under test is not "catastrophic patterns are detected" (the
W3.2 heuristic already claims that, statically). It is stronger and
runtime-only: *whatever* a stored pattern turns out to do, the extraction
chain gives it a bounded slice of wall clock and then takes the page back.
CPython's stdlib ``re`` cannot make that promise at all — no timeout, no
signal, no thread interrupts a backtracking match — which is why the live
path runs on the ``regex`` engine through
:func:`~scrape_core.extraction.regex.search_with_deadline`.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app_shared.enums import ExtractionMethod
from scrape_core.extraction.regex import (
    RegexDeadlineExceeded,
    extract_regex,
    regex_deadline_tripped,
    regex_deadline_watch,
    search_with_deadline,
)

#: The canonical exponential-backtracking pattern. 40 "a"s plus a sentinel
#: that cannot match is ~2^40 paths for a backtracking engine.
CATASTROPHIC = r"(a|aa)+$"
CATASTROPHIC_SUBJECT = "a" * 40 + "!"


# --- plan Step 1 test, verbatim in intent ------------------------------------


def test_catastrophic_pattern_raises_within_budget() -> None:
    t0 = time.monotonic()
    with pytest.raises(RegexDeadlineExceeded):
        search_with_deadline(CATASTROPHIC, CATASTROPHIC_SUBJECT, timeout=0.2)
    assert time.monotonic() - t0 < 1.0


# --- search_with_deadline ----------------------------------------------------


def test_search_with_deadline_returns_the_match_for_a_sane_pattern() -> None:
    match = search_with_deadline(r"SAR\s*([0-9.,]+)", "Price: SAR 1,299.00", timeout=0.25)
    assert match is not None
    assert match.group(1) == "1,299.00"


def test_search_with_deadline_returns_none_when_nothing_matches() -> None:
    assert search_with_deadline(r"USD", "Price: SAR 10", timeout=0.25) is None


def test_deadline_exception_names_the_offending_pattern_and_budget() -> None:
    with pytest.raises(RegexDeadlineExceeded) as excinfo:
        search_with_deadline(CATASTROPHIC, CATASTROPHIC_SUBJECT, timeout=0.15)
    assert excinfo.value.pattern == CATASTROPHIC
    assert excinfo.value.timeout == 0.15


# --- extract_regex: a blown deadline costs the page its REGEX reading only ---


def test_extract_regex_returns_none_on_deadline_instead_of_raising() -> None:
    """The page's other four strategies must still get to run.

    A raised exception here would escape `extract()` and kill the whole
    item — the exact failure mode that lost 73 fetched amazon pages to a
    parsel ValueError once already.
    """
    profile = SimpleNamespace(price_regex=CATASTROPHIC, confidence_rules=None)
    html = f"<html><body><p>{CATASTROPHIC_SUBJECT}</p></body></html>"

    t0 = time.monotonic()
    assert extract_regex(html, profile=profile) is None
    # 4x the 0.25s default per-page budget, plus generous slack for a
    # loaded CI box. The point is bounded, not fast.
    assert time.monotonic() - t0 < 5.0


def test_extract_regex_records_the_deadline_for_the_caller() -> None:
    """The spider reads this to stamp REGEX_TIMEOUT instead of PRICE_NOT_FOUND."""
    profile = SimpleNamespace(price_regex=CATASTROPHIC, confidence_rules=None)
    html = f"<html><body><p>{CATASTROPHIC_SUBJECT}</p></body></html>"

    with regex_deadline_watch():
        assert regex_deadline_tripped() is None
        extract_regex(html, profile=profile)
        tripped = regex_deadline_tripped()

    assert tripped is not None
    assert tripped["pattern"] == CATASTROPHIC


def test_no_deadline_recorded_on_a_healthy_page() -> None:
    profile = SimpleNamespace(price_regex=r"SAR\s*([0-9.,]+)", confidence_rules=None)
    with regex_deadline_watch():
        candidate = extract_regex("<html><body><p>SAR 42.00</p></body></html>", profile=profile)
        assert regex_deadline_tripped() is None
    assert candidate is not None
    assert candidate.method is ExtractionMethod.REGEX
    assert candidate.raw_price_text == "42.00"


def test_deadline_watch_resets_between_pages() -> None:
    profile = SimpleNamespace(price_regex=CATASTROPHIC, confidence_rules=None)
    html = f"<html><body><p>{CATASTROPHIC_SUBJECT}</p></body></html>"
    with regex_deadline_watch():
        extract_regex(html, profile=profile)
        assert regex_deadline_tripped() is not None
    with regex_deadline_watch():
        assert regex_deadline_tripped() is None


# --- cumulative per-page budget ----------------------------------------------


def test_per_page_budget_stops_after_four_timeouts_worth_of_nodes(monkeypatch) -> None:
    """A per-node cap alone is not a budget.

    A live amazon.sa product page has 6,932 text nodes. At 0.25 s each that
    is 28 minutes of one core for a single pattern, so the page carries its
    own cumulative ceiling of ``4 x timeout``.
    """
    from scrape_core.extraction import regex as regex_module

    monkeypatch.setattr(regex_module, "_regex_timeout_seconds", lambda: 0.05)
    nodes = [CATASTROPHIC_SUBJECT] * 50

    t0 = time.monotonic()
    with pytest.raises(RegexDeadlineExceeded):
        regex_module._first_regex_match(nodes, CATASTROPHIC)
    elapsed = time.monotonic() - t0

    # 4 x 0.05 s = 0.2 s of budget; without it this would be 50 x 0.05 s.
    assert elapsed < 1.0


# --- quarantine -------------------------------------------------------------


def test_quarantined_profile_skips_the_regex_rules_entirely() -> None:
    from datetime import UTC, datetime

    profile = SimpleNamespace(
        price_regex=r"SAR\s*([0-9.,]+)",
        confidence_rules=None,
        regex_quarantined_at=datetime.now(UTC),
    )
    candidate = extract_regex("<html><body><p>SAR 42.00</p></body></html>", profile=profile)
    # The single-number heuristic still runs — quarantine removes the REGEX
    # strategy, it does not blind the page.
    assert candidate is not None
    assert candidate.method is ExtractionMethod.SINGLE_NUMBER


def test_unquarantined_profile_still_uses_its_regex_rules() -> None:
    profile = SimpleNamespace(
        price_regex=r"SAR\s*([0-9.,]+)",
        confidence_rules=None,
        regex_quarantined_at=None,
    )
    candidate = extract_regex("<html><body><p>SAR 42.00</p></body></html>", profile=profile)
    assert candidate is not None
    assert candidate.method is ExtractionMethod.REGEX


def test_profile_without_the_column_is_not_treated_as_quarantined() -> None:
    """A pre-migration row (or a dict-shaped test stub) must fail OPEN here."""
    profile = SimpleNamespace(price_regex=r"SAR\s*([0-9.,]+)", confidence_rules=None)
    candidate = extract_regex("<html><body><p>SAR 42.00</p></body></html>", profile=profile)
    assert candidate is not None
    assert candidate.method is ExtractionMethod.REGEX
