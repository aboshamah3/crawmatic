"""Typed competitor identifiers + Shopify variant resolution (EPA B4, READY-003).

The bug this suite pins down (2026-08-24 production canary,
``PRODUCTION_READINESS_REPORT_2026-08-24.md``): the Shopify adapter read
``competitor_product_matches.competitor_variant_identifier`` — an
**untyped** legacy text column — as if it were always a Shopify
``variants[].id``. On S-Tech (``stech.ink``) that column actually holds a
product *handle*, a *barcode*, or a supplier *SKU*, so the adapter found
"no variant with that id" on a perfectly healthy HTTP 200 product JSON
and emitted terminal ``NOT_LISTED`` for 26 of 30 canary targets. A
false "the competitor delisted this product" is the most expensive
possible wrong answer: it silently removes a real competitor price from
every downstream comparison.

The contract these tests fix:

* identifiers are **typed** (:class:`app_shared.enums.CompetitorIdentifierType`)
  and carry provenance (:class:`~app_shared.enums.CompetitorIdentifierSource`);
* ``resolve_shopify_variant`` returns a closed algebraic result —
  ``Resolved`` | ``Ambiguous`` | ``AbsentProven`` | ``IdentityIncompatible``;
* ``NOT_LISTED`` is reachable **only** from ``AbsentProven``, which in
  turn requires *repeated validated absence* (two independent fetches
  >= 24h apart) — never a single 404, never "the identifier didn't look
  like a variant id". This is deliberately the same evidence bar A6's
  ``CONFIRMED_DELISTED`` uses (``scripts/classify_match_set.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app_shared.enums import (
    CompetitorIdentifierSource,
    CompetitorIdentifierType,
    ScrapeErrorCode,
)
from scrape_core.adapters.result import AdapterOutcome
from scrape_core.adapters.variant_resolution import (
    AbsenceEvidence,
    AbsentProven,
    Ambiguous,
    IdentityIncompatible,
    Resolved,
    TypedIdentifier,
    classify_identifier,
    outcome_for_resolution,
    resolve_shopify_variant,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _variant(variant_id: int, *, sku: str | None = None, barcode: str | None = None, price: int = 9000) -> dict:
    return {
        "id": variant_id,
        "title": "Default Title",
        "sku": sku,
        "barcode": barcode,
        "price": price,
        "available": True,
    }


def _product(handle: str, variants: list[dict]) -> dict:
    return {
        "id": 7777777,
        "handle": handle,
        "title": "HP 903XL Cyan",
        "variants": variants,
    }


def _identifier(
    identifier_type: CompetitorIdentifierType,
    value: str,
    *,
    source: CompetitorIdentifierSource = CompetitorIdentifierSource.LEGACY_BACKFILL,
    verified_at: datetime | None = None,
) -> TypedIdentifier:
    return TypedIdentifier(
        identifier_type=identifier_type, value=value, source=source, verified_at=verified_at
    )


# --- 1. A handle identifier can never produce NOT_LISTED ------------------


def test_handle_identifier_never_yields_not_listed() -> None:
    """The exact S-Tech regression: the legacy column held a product
    handle, the adapter compared it to ``variants[].id``, found nothing,
    and called the product delisted. A handle names the *product*, not a
    variant — it can confirm identity but can never, on its own, prove a
    variant's absence."""
    product = _product("hp-ink-original-cyan-903xl-t6m03ae-officejet-1", [_variant(48394646913319)])
    identifiers = [
        _identifier(
            CompetitorIdentifierType.HANDLE,
            "hp-ink-original-cyan-903xl-t6m03ae-officejet-1",
        )
    ]

    result = resolve_shopify_variant(product, identifiers)

    assert isinstance(result, Resolved), result
    assert result.variant["id"] == 48394646913319
    assert not isinstance(result, AbsentProven)
    assert outcome_for_resolution(result) is AdapterOutcome.FOUND


def test_legacy_untyped_handle_value_resolves_instead_of_not_listed() -> None:
    """The production shape: the value arrives ``UNKNOWN``-typed straight
    off the legacy column. Resolution is by *evidence against the fetched
    JSON*, never by regex/length guessing — the value happens to be the
    product handle, so identity is confirmed and the single variant is
    selected."""
    product = _product("canon-ink-original-black-pg-445", [_variant(48394648000001, sku="13401")])
    identifiers = [_identifier(CompetitorIdentifierType.UNKNOWN, "canon-ink-original-black-pg-445")]

    result = resolve_shopify_variant(product, identifiers)

    assert isinstance(result, Resolved), result
    assert result.matched_identifier is not None
    assert result.corrected_type is CompetitorIdentifierType.HANDLE


# --- 2. A unique barcode selects its variant ------------------------------


def test_unique_barcode_matches_its_variant() -> None:
    product = _product(
        "hp-ink-original-cyan-951xl-cn046ae-8610-8621",
        [
            _variant(1, sku="13344", barcode="CN046AE"),
            _variant(2, sku="13345", barcode="CN047AE"),
        ],
    )
    identifiers = [_identifier(CompetitorIdentifierType.BARCODE, "CN047AE")]

    result = resolve_shopify_variant(product, identifiers)

    assert isinstance(result, Resolved), result
    assert result.variant["id"] == 2
    assert result.matched_field == "barcode"


# --- 3. An ambiguous barcode quarantines, never guesses -------------------


def test_ambiguous_barcode_quarantines_and_never_picks_one() -> None:
    """Two variants carry the same barcode. A wrong price is worse than a
    missing one, so this must NOT pick the first/cheapest/available one —
    it quarantines for review."""
    product = _product(
        "duplicate-barcode-product",
        [_variant(1, barcode="SHARED"), _variant(2, barcode="SHARED")],
    )
    identifiers = [_identifier(CompetitorIdentifierType.BARCODE, "SHARED")]

    result = resolve_shopify_variant(product, identifiers)

    assert isinstance(result, Ambiguous), result
    assert {v["id"] for v in result.candidates} == {1, 2}
    assert outcome_for_resolution(result) is AdapterOutcome.IDENTITY_UNRESOLVED
    assert result.needs_review is True


# --- 4. The default variant is only ever legitimate when there is one -----


def test_default_variant_only_for_single_variant_products() -> None:
    single = _product("single", [_variant(11)])
    multi = _product("multi", [_variant(21), _variant(22), _variant(23)])

    resolved = resolve_shopify_variant(single, [])
    assert isinstance(resolved, Resolved), resolved
    assert resolved.variant["id"] == 11
    assert resolved.matched_identifier is None

    ambiguous = resolve_shopify_variant(multi, [])
    assert isinstance(ambiguous, Ambiguous), ambiguous
    assert len(ambiguous.candidates) == 3


def test_unmatched_legacy_identifier_falls_back_to_the_sole_variant() -> None:
    """Nine of the 30 canary S-Tech targets hold a *stale* legacy value
    (an old supplier SKU / a truncated handle) that matches nothing in
    today's JSON. The product itself is plainly there and has exactly one
    variant, so the honest answer is that one variant — never
    ``NOT_LISTED``. An unverified legacy string failing to match is
    evidence about the *string*, not about the store."""
    product = _product("ugreen-dp-male-to-male-cable-2m-black-10211", [_variant(51550526210343, sku="18931")])
    identifiers = [_identifier(CompetitorIdentifierType.UNKNOWN, "10211")]

    result = resolve_shopify_variant(product, identifiers)

    assert isinstance(result, Resolved), result
    assert result.variant["id"] == 51550526210343
    assert result.matched_identifier is None


def test_verified_variant_id_absent_from_a_multi_variant_product_is_not_guessed() -> None:
    """A *verified* variant id that is gone from a multi-variant product
    is a genuine variant-level absence — but still not ``NOT_LISTED``
    without repeated evidence, and never silently substituted with
    another variant's price."""
    product = _product("multi", [_variant(21), _variant(22)])
    identifiers = [
        _identifier(
            CompetitorIdentifierType.SHOPIFY_VARIANT_ID,
            "99999",
            source=CompetitorIdentifierSource.PRODUCT_JSON,
            verified_at=NOW - timedelta(days=3),
        )
    ]

    result = resolve_shopify_variant(product, identifiers)

    assert not isinstance(result, Resolved), result
    assert outcome_for_resolution(result) is not AdapterOutcome.NOT_LISTED


# --- 5/6. Absence needs repeated, validated evidence ----------------------


def test_single_404_is_not_absent_proven() -> None:
    """One validated-absence fetch is never enough — the same bar A6's
    ``CONFIRMED_DELISTED`` sets. A lone 404 conflates a real delisting
    with a block, a wrong locale, and a misconfiguration."""
    evidence = AbsenceEvidence(validated_absence_at=(NOW,))

    result = resolve_shopify_variant(
        None,
        [_identifier(CompetitorIdentifierType.HANDLE, "gone-product")],
        absence_evidence=evidence,
    )

    assert not isinstance(result, AbsentProven), result
    assert outcome_for_resolution(result) is not AdapterOutcome.NOT_LISTED


def test_repeated_validated_absence_is_not_listed() -> None:
    evidence = AbsenceEvidence(
        validated_absence_at=(NOW - timedelta(hours=25), NOW)
    )

    result = resolve_shopify_variant(
        None,
        [_identifier(CompetitorIdentifierType.HANDLE, "gone-product")],
        absence_evidence=evidence,
    )

    assert isinstance(result, AbsentProven), result
    assert outcome_for_resolution(result) is AdapterOutcome.NOT_LISTED


def test_two_absence_fetches_less_than_24h_apart_are_not_proven() -> None:
    evidence = AbsenceEvidence(
        validated_absence_at=(NOW - timedelta(hours=6), NOW)
    )

    result = resolve_shopify_variant(None, [], absence_evidence=evidence)

    assert not isinstance(result, AbsentProven), result


# --- IdentityIncompatible -------------------------------------------------


def test_handle_naming_a_different_product_is_identity_incompatible() -> None:
    product = _product("actual-product", [_variant(1)])
    identifiers = [_identifier(CompetitorIdentifierType.HANDLE, "a-completely-different-product")]

    result = resolve_shopify_variant(product, identifiers)

    assert isinstance(result, IdentityIncompatible), result
    assert outcome_for_resolution(result) is AdapterOutcome.IDENTITY_UNRESOLVED
    assert result.needs_review is True


def test_non_shopify_payload_is_identity_incompatible() -> None:
    result = resolve_shopify_variant({"not": "a shopify product"}, [])
    assert isinstance(result, IdentityIncompatible), result


# --- Outcome mapping ------------------------------------------------------


def test_identity_unresolved_maps_to_its_own_error_code() -> None:
    """``IDENTITY_UNRESOLVED`` is a *failure* code, deliberately distinct
    from ``NOT_LISTED`` (a terminal listing verdict) and from
    ``IDENTITY_MISMATCH`` (we proved we fetched the wrong product)."""
    assert ScrapeErrorCode.IDENTITY_UNRESOLVED != ScrapeErrorCode.NOT_LISTED
    assert ScrapeErrorCode.IDENTITY_UNRESOLVED != ScrapeErrorCode.IDENTITY_MISMATCH
    assert AdapterOutcome.IDENTITY_UNRESOLVED.value == "IDENTITY_UNRESOLVED"


# --- classify_identifier: evidence, never regex/length --------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("48394646913319", CompetitorIdentifierType.SHOPIFY_VARIANT_ID),
        ("CN046AE", CompetitorIdentifierType.BARCODE),
        ("13344", CompetitorIdentifierType.SKU),
        ("hp-ink-cyan-903xl", CompetitorIdentifierType.HANDLE),
        ("9278219845927", CompetitorIdentifierType.UNKNOWN),
    ],
)
def test_classify_identifier_resolves_against_the_json(
    value: str, expected: CompetitorIdentifierType
) -> None:
    product = _product(
        "hp-ink-cyan-903xl", [_variant(48394646913319, sku="13344", barcode="CN046AE")]
    )
    classification = classify_identifier(value, product)
    assert classification.identifier_type is expected


def test_classify_identifier_never_guesses_a_numeric_value_as_a_variant_id() -> None:
    """A 13-digit numeric string *looks* like a Shopify variant id, and
    length/regex heuristics would type it as one. It is an EAN that this
    store does not carry in any field — so the honest type is
    ``UNKNOWN``, quarantined, not a fabricated variant id."""
    product = _product("some-product", [_variant(48394646913319, sku="13344", barcode="CN046AE")])
    classification = classify_identifier("9278219845927", product)
    assert classification.identifier_type is CompetitorIdentifierType.UNKNOWN
    assert classification.matched_field is None


def test_classify_identifier_reports_ambiguity_when_two_fields_agree() -> None:
    product = _product("p", [_variant(1, sku="SAME", barcode="SAME")])
    classification = classify_identifier("SAME", product)
    assert classification.ambiguous is True
    assert classification.identifier_type is CompetitorIdentifierType.UNKNOWN


# --- AbsenceEvidence policy ----------------------------------------------


def test_absence_evidence_requires_two_independent_fetches_24h_apart() -> None:
    assert AbsenceEvidence(()).is_proven() is False
    assert AbsenceEvidence((NOW,)).is_proven() is False
    assert AbsenceEvidence((NOW, NOW + timedelta(hours=23, minutes=59))).is_proven() is False
    assert AbsenceEvidence((NOW, NOW + timedelta(hours=24))).is_proven() is True
    # Duplicate timestamps are one fetch recorded twice, never two.
    assert AbsenceEvidence((NOW, NOW)).is_proven() is False
