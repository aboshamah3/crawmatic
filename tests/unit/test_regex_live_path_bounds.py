"""Customer-supplied regex CPU/memory bounds on the LIVE extraction path.

EPA W5.5-L1 item 3. `extraction/pipeline.py` has applied the W3.2 ReDoS
budget on the *ranked* path since W3; the live path
(`extract_regex` -> `_first_regex_match`) compiled and ran DB-supplied
patterns unbounded. These tests pin both halves of the new, flag-gated
bound and — first — that the flag being OFF changes nothing at all.

The flag is a module constant (see the module's `TODO(config)`), so every
test that needs it on sets it with `monkeypatch.setattr`, which restores it
even on failure. No test leaves it on.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from scrape_core.extraction import regex as regex_module
from scrape_core.extraction.regex import _first_regex_match, extract_regex


# A textbook catastrophic pattern: `(a+)+$` against a long run of 'a' with no
# trailing match backtracks exponentially. The pre-flight refuses it on SHAPE
# ("nested unbounded repeat") without ever running it, which is the only
# workable defence — CPython's `re` cannot be interrupted mid-match.
REDOS_PATTERN = r"(a+)+$"
REDOS_SUBJECT = "a" * 40 + "!"


def _profile(**kwargs):
    base = {
        "price_regex": None,
        "old_price_regex": None,
        "currency_regex": None,
        "stock_regex": None,
        "confidence_rules": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


# --- the flag is OFF by default, and OFF means unchanged --------------------


def test_the_bound_ships_disabled() -> None:
    """A live-path behaviour change must be opt-in, not a surprise on deploy."""
    assert regex_module.REGEX_BOUNDS_ENABLED is False


def test_flag_off_still_runs_a_redos_shaped_pattern_unchanged() -> None:
    """Flag off = the original function. Pinning this is what makes the flag safe.

    A short subject keeps the unbounded backtrack cheap enough to run in a
    test; the point is only that the pattern is NOT refused when off.
    """
    assert regex_module.REGEX_BOUNDS_ENABLED is False
    assert _first_regex_match(["aaaa"], r"(a+)+") == ("aaaa", "aaaa")


def test_flag_off_does_not_import_the_bounded_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag-off path must not even reach `_pattern_refused`."""
    calls: list[str] = []
    monkeypatch.setattr(
        regex_module, "_pattern_refused", lambda p: calls.append(p) or False
    )
    _first_regex_match(["SAR 199.00"], r"SAR ([0-9.]+)")
    assert calls == []


# --- flag ON: pattern pre-flight --------------------------------------------


def test_flag_on_refuses_a_redos_shaped_pattern_and_finds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_ENABLED", True)
    assert _first_regex_match([REDOS_SUBJECT], REDOS_PATTERN) is None


def test_flag_on_refuses_the_redos_pattern_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is a parse-tree decision, so it is instant, not a timeout.

    Generous ceiling on purpose — this asserts "did not backtrack", not a
    performance budget, so it cannot go flaky on a loaded machine.
    """
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_ENABLED", True)
    started = time.monotonic()
    assert _first_regex_match(["a" * 60 + "!"], REDOS_PATTERN) is None
    assert time.monotonic() - started < 1.0


def test_flag_on_refuses_an_oversized_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`search_bounded`'s 512-char pattern budget applies here too."""
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_ENABLED", True)
    oversized = "a" * 600
    assert _first_regex_match([oversized], oversized) is None


def test_flag_on_still_matches_an_ordinary_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed must not mean fail-always."""
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_ENABLED", True)
    assert _first_regex_match(['"priceAmount":199.00'], r'"priceAmount":([0-9.]+)') == (
        "199.00",
        '"priceAmount":199.00',
    )


def test_flag_on_pre_flights_each_pattern_once_not_once_per_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real page has thousands of text nodes; a per-node parse is the bug."""
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_ENABLED", True)
    calls: list[str] = []
    real = regex_module._pattern_refused
    monkeypatch.setattr(
        regex_module,
        "_pattern_refused",
        lambda p: (calls.append(p), real(p))[1],
    )
    _first_regex_match([f"node {i}" for i in range(500)], r"nothing-matches-this")
    assert calls == [r"nothing-matches-this"]


# --- flag ON: input truncation ----------------------------------------------


def test_flag_on_truncates_an_oversized_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A match past the per-node cap is not seen — that IS the memory bound."""
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_ENABLED", True)
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_MAX_NODE_CHARS", 32)
    node = "." * 64 + "SAR 199.00"
    assert _first_regex_match([node], r"SAR ([0-9.]+)") is None
    # ...and the same node under the same cap DOES match when the hit is
    # inside the budget, so the previous assertion is about the cap and not
    # about a broken pattern.
    inside = "SAR 199.00" + "z" * 64
    assert _first_regex_match([inside], r"SAR ([0-9.]+)") == ("199.00", inside)


def test_flag_on_caps_the_total_scanned_across_all_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-node cap alone is not a budget when a page has 6,932 nodes."""
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_ENABLED", True)
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_MAX_NODE_CHARS", 10)
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_MAX_TOTAL_CHARS", 30)
    nodes = ["xxxxxxxxxx"] * 10 + ["SAR 199.00"]
    assert _first_regex_match(nodes, r"SAR ([0-9.]+)") is None


def test_flag_on_returns_the_full_node_as_evidence_not_the_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`matched_text` feeds `reject_if_text_contains`; truncating it would
    change what a validation rule sees."""
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_ENABLED", True)
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_MAX_NODE_CHARS", 16)
    node = "SAR 199.00 was 250.00 tail" + "z" * 100
    value, matched_text = _first_regex_match([node], r"SAR ([0-9.]+)")
    assert value == "199.00"
    assert matched_text == node


# --- end-to-end through the public entry point ------------------------------


def test_extract_regex_fails_closed_on_a_refused_price_regex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused rule yields no REGEX candidate — and never raises."""
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_ENABLED", True)
    html = f"<html><body><span>{REDOS_SUBJECT}</span><span>x</span></body></html>"
    result = extract_regex(html, profile=_profile(price_regex=REDOS_PATTERN))
    # Falls through to the single-number heuristic (which finds nothing here),
    # exactly as an unmatched price_regex always has.
    assert result is None


def test_extract_regex_unchanged_for_the_stored_production_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The four shapes actually stored in production (W5.5-L1 item 3 scan)
    must behave identically with the bound on."""
    monkeypatch.setattr(regex_module, "REGEX_BOUNDS_ENABLED", True)
    html = (
        '<html><body><script>{"priceAmount":199.00,"currencyIso":"SAR",'
        '"stock":{"stockLevelStatus":{"code":"inStock"}}}</script></body></html>'
    )
    profile = _profile(
        price_regex=r'"priceAmount":([0-9.]+)',
        currency_regex=r'"currencyIso":"(SAR)"',
        stock_regex=r'"stock":\{"stockLevelStatus":\{"code":"(\w+)"',
    )
    candidate = extract_regex(html, profile=profile)
    assert candidate is not None
    assert candidate.raw_price_text == "199.00"
    assert candidate.currency == "SAR"
