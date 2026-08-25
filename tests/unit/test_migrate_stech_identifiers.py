"""Pure-core tests for ``scripts/migrate_stech_identifiers.py`` (EPA B4).

No database, no network: ``decide()`` takes a match row plus the product
JSON that was (or was not) obtained, and returns what the backfill would
write. The properties worth pinning down are the ones that make an
irreversible mass write safe:

* a value is typed only when a real store document proves the type;
* no evidence -> ``UNKNOWN`` + quarantine, never a shape-based guess;
* ``canonical_variant_ref`` is selected only for a deterministic identity;
* the legacy column is never a write target.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app_shared.enums import CompetitorIdentifierSource, CompetitorIdentifierType

_SPEC = importlib.util.spec_from_file_location(
    "migrate_stech_identifiers",
    Path(__file__).resolve().parents[2] / "scripts" / "migrate_stech_identifiers.py",
)
assert _SPEC and _SPEC.loader
backfill = importlib.util.module_from_spec(_SPEC)
# Register before exec: @dataclass resolves annotations through
# sys.modules[cls.__module__], which does not exist yet otherwise.
sys.modules[_SPEC.name] = backfill
_SPEC.loader.exec_module(backfill)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _row(identifier: str | None, *, sku: str | None = None) -> "backfill.MatchRow":
    return backfill.MatchRow(
        match_id=uuid.uuid4(),
        competitor_url="https://www.stech.ink/products/hp-ink-cyan-903xl",
        legacy_identifier=identifier,
        legacy_sku=sku,
        canonical_variant_ref=None,
    )


PRODUCT = {
    "id": 9001,
    "handle": "hp-ink-cyan-903xl",
    "variants": [
        {"id": 48394646913319, "sku": "13389", "barcode": "T6M03AE", "price": 9000}
    ],
}
MULTI_VARIANT = {
    "id": 9002,
    "handle": "multi",
    "variants": [
        {"id": 1, "sku": "A", "barcode": "X", "price": 100},
        {"id": 2, "sku": "B", "barcode": "X", "price": 200},
    ],
}


@pytest.mark.parametrize(
    "value,expected_type",
    [
        ("48394646913319", CompetitorIdentifierType.SHOPIFY_VARIANT_ID),
        ("T6M03AE", CompetitorIdentifierType.BARCODE),
        ("13389", CompetitorIdentifierType.SKU),
        ("hp-ink-cyan-903xl", CompetitorIdentifierType.HANDLE),
    ],
)
def test_types_come_from_the_product_json(value: str, expected_type) -> None:
    decision = backfill.decide(_row(value), PRODUCT, fetch_note="ok")
    assert decision.identifier_type is expected_type
    assert decision.source is CompetitorIdentifierSource.PRODUCT_JSON
    assert decision.verified is True
    assert decision.canonical is True
    assert decision.quarantined is False


def test_a_value_the_store_does_not_carry_stays_unknown_and_quarantines() -> None:
    """``9278219845927`` is a 13-digit EAN — indistinguishable by regex or
    length from a Shopify variant id. The store carries it nowhere, so
    the honest type is ``UNKNOWN``."""
    decision = backfill.decide(_row("9278219845927"), PRODUCT, fetch_note="ok")
    assert decision.identifier_type is CompetitorIdentifierType.UNKNOWN
    assert decision.source is CompetitorIdentifierSource.LEGACY_BACKFILL
    assert decision.verified is False
    # The product is single-variant, so the *variant* still resolves —
    # what stays unknown is the identifier's type, not the price.
    assert decision.resolution == "Resolved"
    # ...and precisely because the type was never proven, this row is not
    # promoted to canonical.
    assert decision.canonical is False


def test_no_product_json_never_types_anything() -> None:
    decision = backfill.decide(_row("anything"), None, fetch_note="rate_limited_429")
    assert decision.identifier_type is CompetitorIdentifierType.UNKNOWN
    assert decision.canonical is False
    assert decision.quarantined is True
    assert "rate_limited_429" in decision.reason


def test_a_429_is_never_read_as_an_absence() -> None:
    """The bug class this whole task exists to remove: a transport-level
    refusal must never become a statement about the merchant's catalog."""
    decision = backfill.decide(_row("hp-ink-cyan-903xl"), None, fetch_note="rate_limited_429")
    assert decision.resolution == "NO_PRODUCT_JSON"
    assert "NOT_LISTED" not in decision.resolution


def test_ambiguous_barcode_is_quarantined_not_canonical() -> None:
    decision = backfill.decide(_row("X"), MULTI_VARIANT, fetch_note="ok")
    assert decision.canonical is False
    assert decision.quarantined is True
    assert decision.resolution == "Ambiguous"


def test_a_match_with_no_legacy_identifier_writes_nothing() -> None:
    decision = backfill.decide(_row(None), PRODUCT, fetch_note="ok")
    assert decision.value is None
    assert decision.quarantined is False
    assert backfill.typed_identifier_for(decision, now=NOW) is None


def test_verified_rows_carry_their_evidence_timestamp() -> None:
    decision = backfill.decide(_row("13389"), PRODUCT, fetch_note="ok")
    identifier = backfill.typed_identifier_for(decision, now=NOW)
    assert identifier is not None
    assert identifier.verified_at == NOW
    assert identifier.confidence == 1.0
    assert identifier.effective_from == NOW
    assert identifier.effective_to is None


def test_unverified_rows_carry_no_verification_timestamp() -> None:
    decision = backfill.decide(_row("9278219845927"), PRODUCT, fetch_note="ok")
    identifier = backfill.typed_identifier_for(decision, now=NOW)
    assert identifier is not None
    assert identifier.verified_at is None
    assert identifier.confidence is None


def test_apply_requires_a_backup_id() -> None:
    assert backfill.main(["--apply"]) == 2
