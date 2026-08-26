"""TDD tests for the OfferObservation contract (W3.1, READY-012).

Covers the plan's Step 1 failing-tests list exactly:

1. float prices are rejected at validation;
2. unknown fields stay `None` (never coerced to 0);
3. landed total is computed only when ALL of its components are known;
4. serialization round-trips stably across `schema_version`;
5. the evidence hash for a fixture observation is resolvable through the
   replay tool.

No DB connection: pure pydantic validation + the filesystem evidence
store (isolated to `tmp_path`, never the real `var/evidence`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app_shared.observations.evidence_store import (
    EvidenceIntegrityError,
    EvidenceNotFoundError,
    compute_hash,
    replay,
    resolve_hash,
    store_evidence,
)
from app_shared.observations.offer_observation import (
    ConfidenceDimensions,
    IdentityFacet,
    OfferObservation,
    PromotionFacts,
)


def _minimal_kwargs(**overrides: object) -> dict:
    base = dict(
        observation_id=uuid.uuid4(),
        source_url="https://competitor.example/p/123",
        domain="competitor.example",
        observed_at=datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return base


# --- Step 1: float prices rejected at validation ---------------------------


@pytest.mark.parametrize(
    "field_name",
    ["item_price", "list_price", "shipping_cost", "fees", "deposit", "unit_price"],
)
def test_float_price_rejected(field_name: str) -> None:
    with pytest.raises(ValidationError, match="float"):
        OfferObservation(**_minimal_kwargs(**{field_name: 19.99}))


def test_float_rejected_in_nested_promotion_money() -> None:
    with pytest.raises(ValidationError, match="float"):
        PromotionFacts(automatic_discount=5.0)


def test_float_rejected_in_confidence_dimension() -> None:
    with pytest.raises(ValidationError, match="float"):
        ConfidenceDimensions(discovery=0.9)


def test_valid_decimal_price_accepted() -> None:
    obs = OfferObservation(**_minimal_kwargs(item_price=Decimal("19.99"), currency="USD"))
    assert obs.item_price == Decimal("19.99")


def test_over_scale_price_rejected() -> None:
    """Money's existing boundary (`parse_money`) rejects >4 fractional
    digits rather than silently rounding — this contract must inherit
    that, not re-round."""
    with pytest.raises(ValidationError):
        OfferObservation(**_minimal_kwargs(item_price=Decimal("19.999999")))


# --- Step 2: unknown stays None, never 0 ------------------------------------


def test_unknown_fields_default_to_none_not_zero() -> None:
    obs = OfferObservation(**_minimal_kwargs())

    assert obs.item_price is None
    assert obs.list_price is None
    assert obs.shipping_cost is None
    assert obs.fees is None
    assert obs.deposit is None
    assert obs.unit_price is None
    assert obs.landed_total is None
    assert obs.tax_included is None
    assert obs.confidence is None
    assert obs.expected_identity is None
    assert obs.observed_identity is None
    assert obs.promotion is None
    assert obs.human_review_required is None
    assert obs.rejection_reason is None

    # None of these should ever silently become 0 / False / "".
    for field_name in ("item_price", "list_price", "shipping_cost", "fees", "deposit"):
        assert getattr(obs, field_name) != 0


def test_unknown_confidence_dimension_stays_none_not_zero() -> None:
    dims = ConfidenceDimensions(discovery=Decimal("0.8"))
    assert dims.identity is None
    assert dims.extraction is None
    assert dims.comparability is None
    assert dims.identity != 0


def test_identity_facet_partial_fields_stay_none() -> None:
    facet = IdentityFacet(title="Widget Pro")
    assert facet.sku is None
    assert facet.gtin is None
    assert facet.mpn is None
    assert facet.variant_attrs is None


# --- Step 3: landed total computed only when ALL components known ----------


def test_landed_total_none_when_any_component_missing() -> None:
    obs = OfferObservation(
        **_minimal_kwargs(
            item_price=Decimal("100.00"),
            shipping_cost=Decimal("10.00"),
            # fees and deposit are unknown.
        )
    )
    assert obs.landed_total is None


def test_landed_total_computed_when_all_components_known() -> None:
    obs = OfferObservation(
        **_minimal_kwargs(
            item_price=Decimal("100.00"),
            shipping_cost=Decimal("10.00"),
            fees=Decimal("2.50"),
            deposit=Decimal("0.00"),
        )
    )
    assert obs.landed_total == Decimal("112.50")


def test_landed_total_input_is_ignored_and_recomputed() -> None:
    """landed_total is a computed field — an explicitly-passed value that
    disagrees with the components is silently overwritten by the
    computation, not trusted as caller input."""
    obs = OfferObservation(
        **_minimal_kwargs(
            item_price=Decimal("100.00"),
            shipping_cost=Decimal("10.00"),
            fees=Decimal("0.00"),
            deposit=Decimal("0.00"),
            landed_total=Decimal("999999.00"),
        )
    )
    assert obs.landed_total == Decimal("110.00")


def test_landed_total_none_when_zero_components_known() -> None:
    obs = OfferObservation(**_minimal_kwargs())
    assert obs.landed_total is None


# --- Step 4: serialization round-trip stable across schema_version ---------


def test_schema_version_defaults_and_is_fixed() -> None:
    obs = OfferObservation(**_minimal_kwargs())
    assert obs.schema_version == "1"

    with pytest.raises(ValidationError):
        OfferObservation(**_minimal_kwargs(schema_version="2"))


def test_serialization_round_trip_stable() -> None:
    obs = OfferObservation(
        **_minimal_kwargs(
            canonical_url="https://competitor.example/p/123?ref=strip",
            market="AE",
            currency="AED",
            item_price=Decimal("249.9900"),
            list_price=Decimal("299.0000"),
            shipping_cost=Decimal("15.0000"),
            fees=Decimal("0.0000"),
            deposit=Decimal("0.0000"),
            tax_included=True,
            expected_identity=IdentityFacet(sku="ABC-123", brand="Acme"),
            observed_identity=IdentityFacet(
                sku="ABC-123", brand="Acme", variant_attrs={"color": "black"}
            ),
            promotion=PromotionFacts(
                coupon_code="SAVE10",
                automatic_discount=Decimal("10.0000"),
            ),
            confidence=ConfidenceDimensions(
                discovery=Decimal("0.9000"),
                identity=Decimal("0.8500"),
                extraction=Decimal("0.9900"),
                comparability=Decimal("0.7000"),
            ),
            validation_reasons=["title_fuzzy_match"],
            raw_evidence_hash="a" * 64,
        )
    )

    dumped_json = obs.model_dump_json()
    restored = OfferObservation.model_validate_json(dumped_json)

    assert restored == obs
    assert restored.schema_version == obs.schema_version == "1"
    assert restored.item_price == Decimal("249.9900")
    # Decimal round-trips as an exact string, never a lossy float.
    assert '"item_price":"249.9900"' in dumped_json.replace(" ", "")


def test_serialization_round_trip_stable_with_all_unknown() -> None:
    """The minimal, mostly-None observation round-trips too — absence
    round-trips as absence, not as a materialized zero."""
    obs = OfferObservation(**_minimal_kwargs())
    restored = OfferObservation.model_validate_json(obs.model_dump_json())
    assert restored == obs
    assert restored.item_price is None


# --- Step 5: evidence hash resolvable through the replay tool ---------------


def test_evidence_hash_resolvable_through_replay(tmp_path: Path) -> None:
    fixture_bytes = b"<html><body>Price: AED 249.99</body></html>"
    evidence_hash = store_evidence(fixture_bytes, store_dir=tmp_path)

    assert evidence_hash == compute_hash(fixture_bytes)

    obs = OfferObservation(**_minimal_kwargs(raw_evidence_hash=evidence_hash))

    result = replay(obs.raw_evidence_hash, store_dir=tmp_path)
    assert result.data == fixture_bytes
    assert result.verified is True
    assert result.evidence_hash == evidence_hash


def test_evidence_replay_missing_hash_raises(tmp_path: Path) -> None:
    with pytest.raises(EvidenceNotFoundError):
        resolve_hash("b" * 64, store_dir=tmp_path)


def test_evidence_replay_detects_corruption(tmp_path: Path) -> None:
    fixture_bytes = b"original evidence bytes"
    evidence_hash = store_evidence(fixture_bytes, store_dir=tmp_path)

    # Corrupt the stored object in place.
    stored_path = tmp_path / evidence_hash[:2] / evidence_hash[2:4] / evidence_hash
    stored_path.write_bytes(b"tampered bytes")

    with pytest.raises(EvidenceIntegrityError):
        resolve_hash(evidence_hash, store_dir=tmp_path)


def test_evidence_store_is_idempotent(tmp_path: Path) -> None:
    fixture_bytes = b"same bytes twice"
    hash_a = store_evidence(fixture_bytes, store_dir=tmp_path)
    hash_b = store_evidence(fixture_bytes, store_dir=tmp_path)
    assert hash_a == hash_b
    assert resolve_hash(hash_a, store_dir=tmp_path) == fixture_bytes


# --- extra_forbid / naive datetime guards -----------------------------------


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        OfferObservation(**_minimal_kwargs(not_a_real_field="x"))


def test_naive_observed_at_rejected() -> None:
    with pytest.raises(ValidationError, match="naive"):
        OfferObservation(**_minimal_kwargs(observed_at=datetime(2026, 8, 26, 12, 0, 0)))


def test_invalid_currency_rejected() -> None:
    with pytest.raises(ValidationError):
        OfferObservation(**_minimal_kwargs(currency="usd"))


def test_confidence_dimension_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        ConfidenceDimensions(discovery=Decimal("1.5"))
