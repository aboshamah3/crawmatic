"""Amazon/Noon re-certification on labeled fixtures (Task B5, READY-004 part 1).

DB-free, network-free: every fixture below is real, previously-captured
page content (see ``tests/fixtures/amazon_labeled/FIXTURES.md`` and
``tests/fixtures/noon_labeled/FIXTURES.md`` for full provenance/governance
-- captured timestamp, sha256, source URL, refresh policy, and the
50-request logged budget this session used to obtain them). No network
call happens anywhere in this module; extraction runs the real
``scrape_core`` strategy functions against on-disk HTML/JSON, exactly as
``contracts/extraction.md`` describes them running in production.

## Certification model

A method is **certified** only when its fixture pass rate meets its
profile threshold (Amazon CSS >= 90%, Noon PLATFORM_JSON >= 90%) on
eligible fixtures, with **zero** ``xfail`` markers anywhere in its
certified test function. ``test_gate_b_certified_amazon_css_has_zero_xfail_markers``
below is the mechanical enforcement of that: it inspects
``test_amazon_css_certified``'s own pytest markers directly, so nobody can
silence a real regression by slapping ``@pytest.mark.xfail`` on the
certified test instead of fixing it or moving the case to the diagnostic
bucket.

## Results this session (see FIXTURES.md in each fixture dir for evidence)

* **Amazon CSS: CERTIFIED**, 5/5 fixtures pass (100%, threshold 90%).
  4 of the 5 are genuine "page says unavailable, correctly extract no
  price" cases; only 1 exercises a full positive price+currency+identity
  match -- see ``amazon_labeled/FIXTURES.md``'s "n=1 caveat" for the
  honest limits of that sample. A separate, real, evidence-backed known
  limitation (amazon.sa's default locale renders currency as an Arabic
  word, not the ISO code) is captured as its own ``xfail(strict=True)``
  diagnostic test, deliberately excluded from the certified pass rate.
* **Noon PLATFORM_JSON: CERTIFIED** (Task B5b, proxy capture), 21/21
  ``ACTIVE``-classified fixtures pass (100%, threshold 90%). B5's direct
  fetches were 100% blocked (Akamai IP-level block); B5b re-attempted the
  same 34 canary targets through the DataImpulse proxy (production's own
  configured access method for noon.com) and got 34/34 HTTP 200s.
  Certification is scored only on the 21 targets classified ``ACTIVE`` in
  ``match_audit_classifications`` (A6) -- the set whose 2026-08-24 canary
  observation was a real successful, comparable price -- per the task's
  eligibility scope; all 21 were found again today. See
  ``noon_labeled/FIXTURES.md`` for the full capture trace, proxy request
  log, and a **material robots.txt finding** (Noon's ``/_svc/`` path is
  ``Disallow``'d for generic user agents, which is what both production's
  pre-existing playbook and this session's Chrome-impersonated captures
  used -- flagged there for owner review, not a code fix this test module
  is positioned to make).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from scrape_core.adapters.catalog import PublicCatalogJsonAdapter
from scrape_core.adapters.result import AdapterContext, AdapterOutcome, AdapterResponse, IdentityStatus
from scrape_core.extraction.css import extract_css
from scrape_core.validation import Accepted, validate_candidate

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_AMAZON_DIR = _FIXTURES / "amazon_labeled"
_NOON_DIR = _FIXTURES / "noon_labeled"

AMAZON_CSS_THRESHOLD = 0.90
NOON_THRESHOLD = 0.90


@dataclass
class _Profile:
    """Minimal stand-in for a resolved ``ScrapeProfile`` row -- ``extract_css``
    only reads these attributes (see ``scrape_core.extraction.css``)."""

    price_selector: str | None = None
    currency_selector: str | None = None
    title_selector: str | None = None
    stock_selector: str | None = None
    old_price_selector: str | None = None
    confidence_rules: dict | None = None


# Selectors verified directly against the real captures in
# amazon_labeled/ (see FIXTURES.md "Locale finding"): `.a-price
# .a-offscreen` inside `#corePrice_feature_div` for price,
# `.a-price-symbol` for currency, `#productTitle` for title,
# `#availability` for the stock text. These match the ExtractionMethod.CSS
# results canary DB observations already recorded for amazon.sa.
AMAZON_CSS_PROFILE = _Profile(
    price_selector=".a-price .a-offscreen",
    currency_selector=".a-price-symbol",
    title_selector="#productTitle",
    stock_selector="#availability",
)


def _load_cases(root: Path) -> list[tuple[str, Path, dict]]:
    cases = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        expected = json.loads((d / "expected.json").read_text(encoding="utf-8"))
        cases.append((d.name, d, expected))
    return cases


CERTIFIED_AMAZON_CASES = _load_cases(_AMAZON_DIR)
CERTIFIED_AMAZON_IDS = frozenset(name for name, _, _ in CERTIFIED_AMAZON_CASES)
assert CERTIFIED_AMAZON_CASES, "no amazon_labeled fixtures found -- fixture set is empty"

DIAGNOSTIC_AMAZON_CASES = _load_cases(_AMAZON_DIR / "_locale_diagnostic")
DIAGNOSTIC_AMAZON_IDS = frozenset(name for name, _, _ in DIAGNOSTIC_AMAZON_CASES)
assert DIAGNOSTIC_AMAZON_CASES, "no diagnostic amazon fixtures found"

# Gate B, part 1 (structural): a certified id can never also be a
# diagnostic (known-limitation/xfail) id. Runs at collection time, so a
# future fixture move that violates this fails the whole module
# immediately rather than silently blending a broken case into the
# certified set.
_overlap = CERTIFIED_AMAZON_IDS & DIAGNOSTIC_AMAZON_IDS
assert not _overlap, (
    f"Gate B violation: {_overlap} appear(s) in BOTH the certified and "
    "diagnostic Amazon fixture sets -- a certified method must have ZERO "
    "xfails; move the case to exactly one bucket."
)

# Populated by test_amazon_css_certified as it runs; read by
# test_amazon_css_pass_rate_meets_threshold. (pytest runs a module's
# tests in file order by default, so this dependency is safe -- but each
# certified-case assertion is also self-contained, so a reordered run
# only loses the aggregate pass-rate check, never hides a per-case
# failure.)
_amazon_results: dict[str, bool] = {}


@pytest.mark.parametrize(
    "name,fixture_dir,expected", CERTIFIED_AMAZON_CASES, ids=[c[0] for c in CERTIFIED_AMAZON_CASES]
)
def test_amazon_css_certified(name: str, fixture_dir: Path, expected: dict) -> None:
    """Price exact, currency exact, identity (title) agrees -- or, for a
    genuinely unavailable product, correctly no price at all. No
    ``xfail`` is permitted on this function (see
    ``test_gate_b_certified_amazon_css_has_zero_xfail_markers``)."""
    html = (fixture_dir / "product.html").read_text(encoding="utf-8")
    candidate = extract_css(html, profile=AMAZON_CSS_PROFILE)

    if expected["expect_no_price"]:
        ok = candidate is None
        _amazon_results[name] = ok
        assert ok, f"{name}: expected no price (page says unavailable) but got {candidate!r}"
        return

    assert candidate is not None, f"{name}: expected a price, extract_css found none"
    outcome = validate_candidate(candidate, {"required_currency": expected["currency"]}, None)

    ok = (
        isinstance(outcome, Accepted)
        and outcome.comparable is True
        and outcome.price == Decimal(str(expected["price"]))
        and candidate.currency == expected["currency"]
        and expected["title_contains"] in (candidate.raw_title or "")
    )
    _amazon_results[name] = ok
    assert ok, (
        f"{name}: price/currency/identity mismatch -- outcome={outcome!r} "
        f"candidate={candidate!r} expected={expected!r}"
    )


def test_amazon_css_pass_rate_meets_threshold() -> None:
    """Certify CSS for amazon.sa only if the measured pass rate clears
    the profile threshold. Depends on test_amazon_css_certified's
    parametrized cases having run first (same module, earlier in file
    order) -- if none ran, that is itself a fixture-collection problem
    worth failing loudly on rather than silently reporting 0/0."""
    assert _amazon_results, "test_amazon_css_certified produced no results to aggregate"
    passed = sum(1 for ok in _amazon_results.values() if ok)
    total = len(_amazon_results)
    rate = passed / total
    assert rate >= AMAZON_CSS_THRESHOLD, (
        f"Amazon CSS pass rate {rate:.0%} ({passed}/{total}) is below the "
        f"{AMAZON_CSS_THRESHOLD:.0%} certification threshold -- UNCERTIFIED"
    )


def _own_marker_names(test_func: object) -> set[str]:
    return {m.name for m in getattr(test_func, "pytestmark", [])}


def test_gate_b_certified_amazon_css_has_zero_xfail_markers() -> None:
    """Mechanical Gate B enforcement (task instruction: 'the test module
    must expose a helper/assertion making it impossible for a certified
    method to retain xfails silently'). Inspects
    ``test_amazon_css_certified``'s own pytest markers directly: if
    anyone ever adds ``@pytest.mark.xfail`` to it (e.g. to quietly paper
    over a real regression instead of fixing it or moving the case to
    the diagnostic bucket), this assertion fails the suite immediately
    -- a certified method can never carry a hidden xfail."""
    markers = _own_marker_names(test_amazon_css_certified)
    assert "xfail" not in markers, (
        "Gate B violation: test_amazon_css_certified carries an xfail "
        f"marker ({markers}) -- a certified method must have ZERO "
        "xfails. Fix the regression, or move the failing fixture into "
        "the diagnostic set (see test_amazon_locale_currency_known_limitation) "
        "and drop the claim of certification instead."
    )


# --- Diagnostic-only: known limitation, explicitly NOT certified ---------


# RESOLVED 2026-08-25 by EPA Task B4 (which folded in this B5 finding).
# B5 recorded this as a strict xfail: amazon.sa's default (non-'/-/en/')
# locale renders the currency node as the Arabic word 'ريال', not the ISO
# code 'SAR', and NO currency normalization existed anywhere in the
# codebase. B5's own note said a real fix "would flip this test green, at
# which point it must be promoted out of the diagnostic set into the
# certified one, not left xfail" -- so the xfail marker is gone and this
# is now a plain regression test.
#
# The fix: `scrape_core.money_text.normalize_currency`, applied once in
# `ExtractionCandidate.__post_init__` so the *persisted* currency is an
# ISO code. That is what mattered: `app_shared.alerts.engine.
# filter_comparable` compares the stored string to the client currency
# with `!=`, so every Arabic-locale observation had been flagged
# CURRENCY_MISMATCH, flipped comparable=false and dropped from every
# price comparison. See tests/unit/test_currency_normalization.py for the
# mapping's deliberate limits (Qatari/Omani/Yemeni riyal wordings and the
# shared ﷼ symbol are NEVER read as SAR).
#
# These fixtures stay in the `_locale_diagnostic` directory (moving them
# would rewrite B5's captured provenance) but they are no longer a known
# limitation, and they remain outside the certified Amazon CSS pass rate
# so that rate keeps meaning exactly what B5 measured.
@pytest.mark.parametrize(
    "name,fixture_dir,expected", DIAGNOSTIC_AMAZON_CASES, ids=[c[0] for c in DIAGNOSTIC_AMAZON_CASES]
)
def test_amazon_locale_currency_known_limitation(name: str, fixture_dir: Path, expected: dict) -> None:
    """The Arabic-locale currency regression, now green (EPA B4)."""
    html = (fixture_dir / "product.html").read_text(encoding="utf-8")
    candidate = extract_css(html, profile=AMAZON_CSS_PROFILE)
    assert candidate is not None, f"{name}: expected a price (this is a real in-stock capture)"
    outcome = validate_candidate(
        candidate, {"required_currency": expected["currency_expected_iso"]}, None
    )
    # The price always parsed; the currency is what used to fail. Both
    # halves are now asserted directly: the Arabic word normalizes to the
    # ISO code, so the observation is comparable instead of silently
    # excluded.
    assert isinstance(outcome, Accepted) and outcome.comparable is True and candidate.currency == "SAR"


# --- Noon: real proxy-captured certification (Task B5b) -------------------


@dataclass
class _AdapterProfile:
    """Minimal stand-in for a resolved profile row carrying just
    ``adapter_config`` -- ``resolve_adapter_config``/``resolve_identifier``
    (``scrape_core.adapters.config``) only read this one attribute."""

    adapter_config: dict


# Mirrors ``resilience.noon.catalog-json.v1``'s ``adapter_config`` exactly
# (see ``scripts/seed_domain_playbooks.sql`` -- the JSONB payload seeded
# there for the noon.com PLATFORM_JSON strategy), reproduced here as a
# plain dict so this module never needs a live DB/profile-repository
# round trip to exercise the real ``PublicCatalogJsonAdapter`` code path.
NOON_CATALOG_ADAPTER_CONFIG = {
    "endpoint_template": "https://www.noon.com/_svc/catalog/api/v3/u/search/?q={identifier_urlencoded}&page=1",
    "identifier_sources": ["competitor_variant_identifier", "competitor_variant_sku"],
    "exact_id_paths": ["sku", "sku_code", "uniqueId", "id"],
    "fields": {
        "sale_price": ["sale_price", "salePrice"],
        "price": ["price", "sellingPrice"],
        "stock": ["is_buyable", "isBuyable"],
        "currency": ["currency", "currencyCode"],
        "title": ["name", "title"],
        "url": ["url", "productUrl"],
    },
    "currency": "SAR",
    "follow_redirects": True,
}
NOON_PROFILE = _AdapterProfile(adapter_config=NOON_CATALOG_ADAPTER_CONFIG)
NOON_ADAPTER = PublicCatalogJsonAdapter()


def _load_noon_cases(root: Path) -> list[tuple[str, Path, dict]]:
    cases = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and (p / "product.json").exists()):
        expected = json.loads((d / "expected.json").read_text(encoding="utf-8"))
        cases.append((d.name, d, expected))
    return cases


CERTIFIED_NOON_CASES = _load_noon_cases(_NOON_DIR)
CERTIFIED_NOON_IDS = frozenset(name for name, _, _ in CERTIFIED_NOON_CASES)
assert CERTIFIED_NOON_CASES, "no noon_labeled real-capture fixtures found -- fixture set is empty"
assert len(CERTIFIED_NOON_CASES) == 21, (
    f"expected exactly 21 ACTIVE-classified Noon fixtures (Task B5b), found {len(CERTIFIED_NOON_CASES)} "
    "-- either a fixture was added/removed without updating this expectation, or the certified set drifted"
)

_noon_results: dict[str, bool] = {}


@pytest.mark.parametrize(
    "name,fixture_dir,expected", CERTIFIED_NOON_CASES, ids=[c[0] for c in CERTIFIED_NOON_CASES]
)
def test_noon_platform_json_certified(name: str, fixture_dir: Path, expected: dict) -> None:
    """Price exact, currency exact, identity (sku + title) agrees, run
    through the real ``PublicCatalogJsonAdapter`` against a real,
    proxy-captured (DataImpulse, session tag ``epa-b5b``) catalog-API
    response -- see ``noon_labeled/FIXTURES.md`` for the capture trace.
    No ``xfail`` is permitted on this function (see
    ``test_gate_b_certified_noon_platform_json_has_zero_xfail_markers``)."""
    body = (fixture_dir / "product.json").read_bytes()
    context = AdapterContext(
        target_url=expected["provenance"]["source_url"],
        variant_identifier=expected["sku_query"],
        profile=NOON_PROFILE,
    )
    response = AdapterResponse(body=body, final_url=expected["provenance"]["source_url"], status=200)
    result = NOON_ADAPTER.adapt(response, context)

    identity_ok = result.outcome == AdapterOutcome.FOUND and result.identity.status == IdentityStatus.VALID
    if not identity_ok:
        _noon_results[name] = False
        assert identity_ok, f"{name}: expected FOUND/VALID identity, got outcome={result.outcome!r} identity={result.identity!r}"
        return

    assert result.candidate is not None, f"{name}: FOUND outcome but no extraction candidate"
    outcome = validate_candidate(result.candidate, {"required_currency": expected["currency"]}, None)

    ok = (
        isinstance(outcome, Accepted)
        and outcome.comparable is True
        and outcome.price == Decimal(str(expected["price"]))
        and result.candidate.currency == expected["currency"]
        and expected["title_contains"] in (result.candidate.raw_title or "")
    )
    _noon_results[name] = ok
    assert ok, (
        f"{name}: price/currency/identity mismatch -- outcome={outcome!r} "
        f"candidate={result.candidate!r} expected={expected!r}"
    )


def test_noon_platform_json_pass_rate_meets_threshold() -> None:
    """Certify PLATFORM_JSON for noon.com only if the measured pass rate
    (on the 21 ``ACTIVE``-classified eligible targets) clears the profile
    threshold. Depends on ``test_noon_platform_json_certified``'s
    parametrized cases having run first (same module, earlier in file
    order)."""
    assert _noon_results, "test_noon_platform_json_certified produced no results to aggregate"
    passed = sum(1 for ok in _noon_results.values() if ok)
    total = len(_noon_results)
    rate = passed / total
    assert rate >= NOON_THRESHOLD, (
        f"Noon PLATFORM_JSON pass rate {rate:.0%} ({passed}/{total}) is below the "
        f"{NOON_THRESHOLD:.0%} certification threshold -- UNCERTIFIED"
    )


def test_gate_b_certified_noon_platform_json_has_zero_xfail_markers() -> None:
    """Mechanical Gate B enforcement for Noon, mirroring the Amazon CSS
    check above: a certified method can never carry a hidden xfail."""
    markers = _own_marker_names(test_noon_platform_json_certified)
    assert "xfail" not in markers, (
        "Gate B violation: test_noon_platform_json_certified carries an xfail "
        f"marker ({markers}) -- a certified method must have ZERO xfails. Fix "
        "the regression, or drop the certification claim instead of silencing it."
    )


def test_noon_not_listed_reconfirmed_evidence_recorded() -> None:
    """The 13 non-``ACTIVE``-classified targets are outside this session's
    certification scope (task instruction: eligibility is scoped to
    ``ACTIVE``-classified targets only) but were still fetched via the
    same proxy session and their real outcome honestly recorded --
    asserted here so a future accidental deletion/fabrication of that
    record is caught, not because they carry any certification weight."""
    reconfirmed = json.loads((_NOON_DIR / "not_listed_reconfirmed.json").read_text(encoding="utf-8"))
    assert reconfirmed["count"] == len(reconfirmed["targets"]) == 13
    overlap = CERTIFIED_NOON_IDS & {t["sku_query"] for t in reconfirmed["targets"]}
    assert not overlap, (
        f"Gate B violation: {overlap} appear(s) in BOTH the certified and "
        "not-listed-reconfirmed Noon sets -- a target must live in exactly one bucket."
    )
    for record in reconfirmed["targets"]:
        assert record["match_id"], "every reconfirmed record must carry a real match_id"
        assert record["evidence_sha256"], "every reconfirmed record must hash its real captured bytes"


def test_noon_db_evidence_labels_still_present() -> None:
    """B5's original DB-evidence-only labels (2026-08-24 canary) remain
    the historical record for cross-reference, even though the 21
    ``ACTIVE`` targets are now superseded by real B5b captures above."""
    db_evidence = json.loads((_NOON_DIR / "db_evidence_labels.json").read_text(encoding="utf-8"))
    assert db_evidence["n_targets"] == len(db_evidence["targets"]) == 34
    for record in db_evidence["targets"]:
        assert record["match_id"], "every DB-evidence label must carry a real match_id"
        assert record["url"].startswith(
            "https://www.noon.com/_svc/catalog/api/v3/u/search/?q="
        ), "every DB-evidence label must be a real canary catalog-API URL, never invented"
