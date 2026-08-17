"""Golden-fixture regression net for the competitor extraction chain
(Task 3.4, 2026-08-16 saas-core-optimization brief, Step 2/3).

One parametrized test runs the real ``scrape_core.extraction.pipeline.extract``
chain (JSON-LD -> EMBEDDED_JSON -> CSS -> regex, the same order the
production spider uses) over one frozen product-page HTML fixture per
competitor and asserts the extracted ``(price, currency, availability)``
against a hand-checked ``expected.json`` sitting next to it. This is the
permanent guard against a chain change silently breaking a competitor
that used to extract cleanly (contracts/extraction.md).

The competitor cohort (``_COMPETITOR_DOMAINS``) is pinned to the 12-row
table in ``matching/REPORT_COST_PER_COMPETITOR_2026-08-12.md`` -- the
authoritative "current competitors" list as of this task. A domain with
no fixture directory yet is **not silently absent** from this suite: it
gets an explicit ``pytest.mark.skip`` case with the reason a human needs
(what was tried, why it failed) instead of just not appearing, so
"we have no regression coverage for X" is a visible skip in the test
report forever, not a gap someone has to notice on their own.

Fixture layout: ``tests/fixtures/competitors/<domain>/``
    * ``product.html`` -- one real captured product page (either fetched
      live this task, 2026-08-17 direct HTTP, or sourced from an
      HTML sample already on disk from a prior task -- see each
      ``expected.json``'s ``provenance`` field for exactly which and why).
    * ``expected.json`` -- ``{"price", "currency", "availability",
      "extraction_method", "provenance"}``, frozen by re-running the real
      extraction chain once at fixture-authoring time (never hand-typed).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app_shared.enums import ExtractionMethod, StockStatus
from scrape_core.extraction.pipeline import extract

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "competitors"

# The 12-competitor cohort, pinned to
# matching/REPORT_COST_PER_COMPETITOR_2026-08-12.md section 2's
# per-competitor table (the "billing table" -- the authoritative list of
# currently-onboarded competitors as of this task). Domains with no
# `<domain>/` fixture directory below get a visible skip, not omission.
_COMPETITOR_DOMAINS: tuple[str, ...] = (
    "amazon.sa",
    "stech.ink",
    "noon.com",
    "pcpalace.com.sa",
    "jarir.com",
    "extra.com",
    "fqtoners.com",
    "rowadalahbar.com",
    "rawand.com.sa",
    "amwajest.com",
    "alshamel.sa",
    "afaqalhasoob.com",
)

# Domains with no fixture yet, and why -- keeps the gap visible in every
# test run instead of a fixture directory silently not existing.
# amazon.sa: both direct-HTTP (Cloudflare "Just a moment..." challenge,
# same block the 2026-08-12 cost report's §4.1 measured live) and the
# r.jina.ai Reader fallback (also challenged) failed on 2026-08-17;
# a paid-proxy capture is out of this task's scope (see task-3.4-report.md
# "Scope adjustments"). Owner runs `onboard_competitor.py --apply` once a
# genuine capture exists.
_MISSING_FIXTURE_REASONS: dict[str, str] = {
    "amazon.sa": (
        "TODO: no fixture yet. 2026-08-17 capture attempt: direct HTTP and the "
        "r.jina.ai Reader fallback both hit a Cloudflare/Akamai challenge page "
        "(matches REPORT_COST_PER_COMPETITOR_2026-08-12.md §4.1's live amazon "
        "block). Needs a proxied capture, out of this task's no-prod-apply scope "
        "-- see task-3.4-report.md."
    ),
}


def _fixture_params() -> list[Any]:
    params = []
    for domain in _COMPETITOR_DOMAINS:
        fixture_dir = _FIXTURES_DIR / domain
        html_path = fixture_dir / "product.html"
        if html_path.is_file():
            params.append(pytest.param(domain, id=domain))
            continue
        reason = _MISSING_FIXTURE_REASONS.get(
            domain, f"TODO: no fixture captured yet for {domain}."
        )
        params.append(pytest.param(domain, marks=pytest.mark.skip(reason=reason), id=domain))
    return params


def test_every_cohort_domain_is_accounted_for() -> None:
    """Sanity check on the harness itself: every domain is either fixture-
    backed or explicitly listed in `_MISSING_FIXTURE_REASONS` -- a domain
    can never just fall through both and vanish."""
    for domain in _COMPETITOR_DOMAINS:
        has_fixture = (_FIXTURES_DIR / domain / "product.html").is_file()
        assert has_fixture or domain in _MISSING_FIXTURE_REASONS, (
            f"{domain} has neither a fixture nor a documented TODO reason -- "
            "add one or the other, never leave it silently uncovered."
        )


@pytest.mark.parametrize("domain", _fixture_params())
def test_extraction_chain_matches_golden_fixture(domain: str) -> None:
    fixture_dir = _FIXTURES_DIR / domain
    html = (fixture_dir / "product.html").read_text(encoding="utf-8", errors="replace")
    expected = json.loads((fixture_dir / "expected.json").read_text(encoding="utf-8"))

    candidate = extract(html)

    assert candidate is not None, (
        f"{domain}: extraction chain found no price candidate at all -- "
        f"was PASSING when {expected['provenance']['source']} was captured."
    )
    assert candidate.raw_price_text == expected["price"], (
        f"{domain}: price mismatch -- got {candidate.raw_price_text!r}, "
        f"expected {expected['price']!r} (method={candidate.method.value})"
    )
    assert candidate.currency == expected["currency"], (
        f"{domain}: currency mismatch -- got {candidate.currency!r}, "
        f"expected {expected['currency']!r}"
    )
    expected_stock = (
        StockStatus(expected["availability"]) if expected["availability"] is not None else None
    )
    assert candidate.stock == expected_stock, (
        f"{domain}: availability mismatch -- got {candidate.stock!r}, "
        f"expected {expected_stock!r}"
    )
    assert candidate.method == ExtractionMethod(expected["extraction_method"]), (
        f"{domain}: extraction strategy changed -- got {candidate.method.value!r}, "
        f"expected {expected['extraction_method']!r}. A different strategy firing "
        "for a previously-working domain is itself a regression worth reviewing, "
        "not just a fluke to relax."
    )
