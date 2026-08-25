"""Replay of the 30 canary S-Tech targets against the real product JSON (EPA B4).

DB-free, network-free. Every fixture is a real, previously-captured
Shopify ``products/{handle}.js`` document paired with the legacy
``competitor_variant_identifier`` production actually held for that
match — see ``tests/fixtures/stech_30_targets/FIXTURES.md`` for full
provenance, sanitization and the logged fetch budget.

On 2026-08-24 the production canary answered terminal ``NOT_LISTED`` for
26 of these 30 targets. All 30 had returned HTTP 200 with healthy product
JSON; the adapter had simply compared an untyped legacy value against
``variants[].id``, found nothing, and called the product delisted
(``PRODUCTION_READINESS_REPORT_2026-08-24.md``).

The two assertions that matter:

1. **ZERO false ``NOT_LISTED``.** ``NOT_LISTED`` is reachable only from
   ``AbsentProven``, and no product here is absent — so not one of these
   30 may produce it, by any route.
2. **>= 26 now-resolvable identities**, i.e. every target the canary got
   wrong is now resolved to exactly one variant.

The replay runs the whole adapter (``ShopifyProductJsonAdapter.adapt``),
not just the resolver, so an outcome-mapping regression is caught too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scrape_core.adapters.result import AdapterContext, AdapterOutcome, AdapterResponse
from scrape_core.adapters.shopify import ShopifyProductJsonAdapter
from scrape_core.adapters.variant_resolution import (
    AbsentProven,
    Resolved,
    identifiers_from_legacy_context,
    resolve_shopify_variant,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stech_30_targets"


def _load_cases() -> list[tuple[str, dict, dict]]:
    cases = []
    for directory in sorted(p for p in _FIXTURES.iterdir() if p.is_dir()):
        product = json.loads((directory / "product.json").read_text(encoding="utf-8"))
        expected = json.loads((directory / "expected.json").read_text(encoding="utf-8"))
        cases.append((directory.name, product, expected))
    return cases


CASES = _load_cases()
assert len(CASES) == 30, f"expected the full 30-target canary set, found {len(CASES)}"

# Populated by the parametrized test below; read by the aggregate tests.
_results: dict[str, str] = {}


class _Target:
    """The duck-typed shape ``AdapterContext.from_target`` reads."""

    def __init__(self, url: str, identifier: str | None, sku: str | None) -> None:
        self.url = url
        self.competitor_variant_identifier = identifier
        self.competitor_variant_sku = sku
        self.profile = None


@pytest.mark.parametrize("name,product,expected", CASES, ids=[c[0] for c in CASES])
def test_stech_target_never_reports_not_listed(name: str, product: dict, expected: dict) -> None:
    """Full adapter replay: a healthy HTTP 200 product document must never
    come back ``NOT_LISTED``, whatever the legacy identifier holds."""
    target = _Target(
        expected["url"],
        expected["legacy_competitor_variant_identifier"],
        expected["legacy_competitor_variant_sku"],
    )
    response = AdapterResponse(
        body=json.dumps(product), final_url=expected["url"], status=expected["http_status"]
    )
    result = ShopifyProductJsonAdapter().adapt(response, AdapterContext.from_target(target))

    _results[name] = str(result.outcome)
    assert result.outcome is not AdapterOutcome.NOT_LISTED, (
        f"{name}: FALSE NOT_LISTED on a live HTTP 200 product "
        f"(handle={product.get('handle')!r}, legacy identifier="
        f"{expected['legacy_competitor_variant_identifier']!r}) — this is exactly "
        "the 2026-08-24 canary regression"
    )


def test_zero_false_not_listed_across_the_whole_canary_set() -> None:
    assert len(_results) == 30, "the parametrized replay did not run for every target"
    false_not_listed = [n for n, outcome in _results.items() if outcome == "NOT_LISTED"]
    assert not false_not_listed, f"false NOT_LISTED verdicts remain: {false_not_listed}"


def test_at_least_26_identities_are_now_resolvable() -> None:
    """The canary's 26 wrong answers must all now resolve to one variant.

    Asserted through the pure resolver as well as the adapter, so the
    number is about identity resolution and not about, say, a price
    field happening to parse.
    """
    resolved = 0
    for name, product, expected in CASES:
        identifiers = identifiers_from_legacy_context(
            expected["legacy_competitor_variant_identifier"],
            expected["legacy_competitor_variant_sku"],
        )
        resolution = resolve_shopify_variant(product, identifiers)
        assert not isinstance(resolution, AbsentProven), (
            f"{name}: resolver proved absence for a product that is right there"
        )
        if isinstance(resolution, Resolved):
            resolved += 1

    assert resolved >= 26, (
        f"only {resolved}/30 S-Tech identities resolve; the 2026-08-24 canary got 4 right "
        "and this change must recover at least the 26 it got wrong"
    )


def test_every_canary_not_listed_target_now_resolves() -> None:
    """Per-target, not just in aggregate: name the ones still failing."""
    regressions = []
    for name, product, expected in CASES:
        if expected["canary_2026_08_24_error_code"] != "NOT_LISTED":
            continue
        identifiers = identifiers_from_legacy_context(
            expected["legacy_competitor_variant_identifier"],
            expected["legacy_competitor_variant_sku"],
        )
        if not isinstance(resolve_shopify_variant(product, identifiers), Resolved):
            regressions.append(name)
    assert not regressions, f"still unresolved after the B4 fix: {regressions}"


def test_the_fixture_corpus_is_the_documented_shape() -> None:
    """Guards the corpus itself: if a refresh silently changes what these
    30 documents contain, the claims in FIXTURES.md (and the 26/30 number
    derived from them) stop meaning anything."""
    statuses = {expected["http_status"] for _, _, expected in CASES}
    assert statuses == {200}, f"fixture set must be all-HTTP-200 captures, got {statuses}"

    canary_not_listed = sum(
        1 for _, _, e in CASES if e["canary_2026_08_24_error_code"] == "NOT_LISTED"
    )
    assert canary_not_listed == 26, (
        f"the canary recorded 26 NOT_LISTED verdicts, this corpus has {canary_not_listed}"
    )
