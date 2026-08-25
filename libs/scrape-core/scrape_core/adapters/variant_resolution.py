"""Typed competitor identifiers -> a Shopify variant (EPA B4, READY-003).

Why this module exists
----------------------
``competitor_product_matches.competitor_variant_identifier`` is a single,
**untyped** text column. The Shopify adapter used to compare it directly
against ``variants[].id`` (falling back to ``variants[].sku``) and, when
nothing matched, emit terminal ``NOT_LISTED``. On S-Tech (``stech.ink``)
that column actually holds a product *handle*, a *barcode*, or a supplier
*SKU*, so on 2026-08-24 the production canary declared 26 of 30 healthy
HTTP 200 products "not listed"
(``PRODUCTION_READINESS_REPORT_2026-08-24.md``). A false delisting is the
most expensive wrong answer this system can give: it removes a real
competitor price from every comparison, silently and terminally.

The contract
------------
:func:`resolve_shopify_variant` maps ``(product_json, typed identifiers)``
to a closed algebraic result:

``Resolved``
    exactly one variant is identified.
``Ambiguous``
    several variants fit, or none fits and the product carries more than
    one — quarantine, never guess. A wrong price is worse than none.
``AbsentProven``
    the store gave a valid response for the right market/locale in which
    the product JSON is genuinely absent, **and** that absence has been
    observed at least twice, at least 24h apart. This is the only route
    to ``NOT_LISTED`` — deliberately the same evidence bar A6's
    ``CONFIRMED_DELISTED`` uses (``scripts/classify_match_set.py``). A
    lone 404 never qualifies.
``IdentityIncompatible``
    the identifiers name a different product than the one fetched, or the
    payload is not a Shopify product at all.

Two rules carry most of the weight:

1. **Typing is evidence, never shape.** A value is a ``SHOPIFY_VARIANT_ID``
   only because it was found in ``variants[].id`` of a real product JSON.
   A 13-digit EAN is indistinguishable from a Shopify variant id by regex
   or length — guessing by shape is exactly how the original bug happened.
2. **An unverified legacy string failing to match is evidence about the
   string, not about the store.** Nine of the 30 canary S-Tech matches
   hold a stale supplier SKU or a truncated handle that matches nothing
   in today's JSON. The product is plainly there; falling back to its
   sole variant is right, and calling it delisted is not. Only a
   *verified* variant-selecting identifier that has vanished from a
   *multi-variant* product counts as a variant-level absence — and even
   then it is quarantined, not declared ``NOT_LISTED``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app_shared.enums import CompetitorIdentifierSource, CompetitorIdentifierType

from scrape_core.adapters.result import AdapterOutcome

__all__ = [
    "ABSENCE_MIN_FETCHES",
    "ABSENCE_MIN_GAP",
    "AbsenceEvidence",
    "AbsentProven",
    "Ambiguous",
    "IdentifierClassification",
    "IdentityIncompatible",
    "Resolved",
    "TypedIdentifier",
    "VariantResolution",
    "VARIANT_FIELD_BY_TYPE",
    "classify_identifier",
    "identifiers_from_legacy_context",
    "outcome_for_resolution",
    "resolve_shopify_variant",
]

#: Repeated-evidence policy for a validated absence. Identical in spirit
#: and in numbers to A6's ``CONFIRMED_DELISTED`` rule, so the live
#: adapter and the offline classifier can never disagree about what
#: "delisted" means.
ABSENCE_MIN_FETCHES = 2
ABSENCE_MIN_GAP = timedelta(hours=24)

#: Which Shopify variant field each identifier type is *declared* to
#: address. ``HANDLE`` addresses the product, not a variant, and so has
#: no entry here.
VARIANT_FIELD_BY_TYPE: Mapping[CompetitorIdentifierType, str] = {
    CompetitorIdentifierType.SHOPIFY_VARIANT_ID: "id",
    CompetitorIdentifierType.SKU: "sku",
    CompetitorIdentifierType.BARCODE: "barcode",
}

#: Search order when resolving: the most specific identity first. An
#: ``UNKNOWN`` legacy value is tried against every field before the
#: product-level handle.
_RESOLUTION_ORDER: tuple[CompetitorIdentifierType, ...] = (
    CompetitorIdentifierType.SHOPIFY_VARIANT_ID,
    CompetitorIdentifierType.BARCODE,
    CompetitorIdentifierType.SKU,
    CompetitorIdentifierType.UNKNOWN,
    CompetitorIdentifierType.HANDLE,
)

_ALL_VARIANT_FIELDS: tuple[str, ...] = ("id", "sku", "barcode")


def _same(left: Any, right: Any) -> bool:
    """Case- and whitespace-insensitive scalar equality.

    Shopify renders ``variants[].id`` as a JSON number and everything
    else as a string; the stored identifier is always text. Comparing
    ``str()`` forms is the only way both sides can meet.
    """
    if left is None or right is None:
        return False
    return str(left).strip().casefold() == str(right).strip().casefold()


@dataclass(frozen=True)
class TypedIdentifier:
    """One typed, provenance-carrying competitor identifier.

    The in-memory twin of a ``match_competitor_identifiers`` row (see
    ``app_shared.models.competitor_identifiers``). ``verified_at`` set,
    or a ``source`` of ``PRODUCT_JSON``/``MERCHANT_FEED``/``MANUAL``,
    means somebody or something actually checked this value against the
    store — which is what licenses treating its disappearance as a fact
    about the store rather than about the string.
    """

    identifier_type: CompetitorIdentifierType
    value: str
    source: CompetitorIdentifierSource = CompetitorIdentifierSource.LEGACY_BACKFILL
    confidence: float | None = None
    verified_at: datetime | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None

    @property
    def is_verified(self) -> bool:
        return (
            self.verified_at is not None
            or self.source is not CompetitorIdentifierSource.LEGACY_BACKFILL
        )

    @property
    def is_current(self) -> bool:
        return self.effective_to is None


@dataclass(frozen=True)
class AbsenceEvidence:
    """Timestamps of *validated* absence observations for one match.

    "Validated absence" means what it means everywhere else in this
    system (``scrape_core`` adapters, A6's classifier): a valid store
    response for the correct market/locale in which the product JSON is
    specifically absent — never a raw transport ``HTTP_404``/``HTTP_410``,
    which conflates a delisting with a block, a wrong locale and a
    misconfiguration.
    """

    validated_absence_at: tuple[datetime, ...] = ()

    def is_proven(
        self,
        *,
        min_fetches: int = ABSENCE_MIN_FETCHES,
        min_gap: timedelta = ABSENCE_MIN_GAP,
    ) -> bool:
        """``True`` iff >= ``min_fetches`` *independent* observations span >= ``min_gap``.

        Duplicate timestamps are one fetch recorded twice, never two — so
        a retry storm inside one run can never manufacture a delisting.
        """
        distinct = sorted(set(self.validated_absence_at))
        if len(distinct) < min_fetches:
            return False
        return (distinct[-1] - distinct[0]) >= min_gap


@dataclass(frozen=True)
class Resolved:
    """Exactly one variant identified."""

    variant: Mapping[str, Any]
    matched_identifier: TypedIdentifier | None = None
    matched_field: str | None = None
    corrected_type: CompetitorIdentifierType | None = None
    reason: str = ""

    needs_review: bool = False


@dataclass(frozen=True)
class Ambiguous:
    """Several variants fit, or none fits a multi-variant product.

    ``candidates`` may legitimately be empty: an unproven absence is also
    "we do not know", and must land here rather than in ``AbsentProven``.
    """

    candidates: tuple[Mapping[str, Any], ...] = ()
    identifier: TypedIdentifier | None = None
    reason: str = ""

    needs_review: bool = True


@dataclass(frozen=True)
class AbsentProven:
    """Validated absence, observed repeatedly. The ONLY route to NOT_LISTED."""

    evidence: tuple[datetime, ...] = ()
    reason: str = ""

    needs_review: bool = False


@dataclass(frozen=True)
class IdentityIncompatible:
    """The identifiers name a different product, or this is not a Shopify product."""

    identifier: TypedIdentifier | None = None
    observed_identity: str | None = None
    reason: str = ""

    needs_review: bool = True


VariantResolution = Resolved | Ambiguous | AbsentProven | IdentityIncompatible


@dataclass(frozen=True)
class IdentifierClassification:
    """What a raw legacy value turned out to *be*, proven against real JSON."""

    identifier_type: CompetitorIdentifierType
    matched_field: str | None = None
    matched_variant_id: str | None = None
    ambiguous: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)


def outcome_for_resolution(resolution: VariantResolution) -> AdapterOutcome:
    """Map a resolution to its adapter outcome.

    ``NOT_LISTED`` is reachable from ``AbsentProven`` and from nowhere
    else — that single line is the whole point of this module.
    """
    if isinstance(resolution, Resolved):
        return AdapterOutcome.FOUND
    if isinstance(resolution, AbsentProven):
        return AdapterOutcome.NOT_LISTED
    return AdapterOutcome.IDENTITY_UNRESOLVED


# --------------------------------------------------------------------
# Classification: what IS this value? (evidence only, never shape)
# --------------------------------------------------------------------


def _variants_of(product_json: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(product_json, Mapping):
        return []
    raw = product_json.get("variants")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def classify_identifier(
    value: str | None, product_json: Mapping[str, Any] | None
) -> IdentifierClassification:
    """Type ``value`` by resolving it against ``product_json``'s real fields.

    Checks ``variants[].id``, ``variants[].barcode``, ``variants[].sku``
    and the product ``handle`` — never a regex, never a length. A value
    found in two different fields is reported ``ambiguous`` and left
    ``UNKNOWN``: a coincidence is not an identity.
    """
    if value is None or not str(value).strip():
        return IdentifierClassification(CompetitorIdentifierType.UNKNOWN)

    variants = _variants_of(product_json)
    handle = product_json.get("handle") if isinstance(product_json, Mapping) else None

    hits: list[tuple[CompetitorIdentifierType, str, str | None]] = []
    for variant in variants:
        for field_name in _ALL_VARIANT_FIELDS:
            if _same(variant.get(field_name), value):
                identifier_type = next(
                    key
                    for key, mapped in VARIANT_FIELD_BY_TYPE.items()
                    if mapped == field_name
                )
                hits.append((identifier_type, field_name, str(variant.get("id"))))
    if _same(handle, value):
        hits.append((CompetitorIdentifierType.HANDLE, "handle", None))

    distinct_types = {hit[0] for hit in hits}
    evidence = {
        "checked_fields": list(_ALL_VARIANT_FIELDS) + ["handle"],
        "variant_count": len(variants),
        "handle": handle,
        "matches": [{"type": str(t), "field": f, "variant_id": v} for t, f, v in hits],
    }
    if not hits:
        return IdentifierClassification(
            CompetitorIdentifierType.UNKNOWN, evidence=evidence
        )
    if len(distinct_types) > 1:
        return IdentifierClassification(
            CompetitorIdentifierType.UNKNOWN, ambiguous=True, evidence=evidence
        )
    identifier_type, field_name, variant_id = hits[0]
    # Same type on several variants (e.g. a barcode shared by two) is
    # still ambiguous at the *variant* level, but the TYPE is known.
    return IdentifierClassification(
        identifier_type,
        matched_field=field_name,
        matched_variant_id=variant_id,
        ambiguous=len({hit[2] for hit in hits}) > 1,
        evidence=evidence,
    )


# --------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------


def _matches_for(
    identifier: TypedIdentifier, variants: Sequence[Mapping[str, Any]]
) -> tuple[list[Mapping[str, Any]], str | None, CompetitorIdentifierType | None]:
    """Variants this identifier selects, the field it matched, and any type correction.

    Tries the identifier's **declared** field first. Only if that finds
    nothing does it fall back to the other variant fields — and reports
    the corrected type when it does, so the caller (and the backfill) can
    record that the stored type was wrong rather than silently papering
    over it.
    """
    declared_field = VARIANT_FIELD_BY_TYPE.get(identifier.identifier_type)
    if declared_field is not None:
        hits = [v for v in variants if _same(v.get(declared_field), identifier.value)]
        if hits:
            return hits, declared_field, None

    for field_name in _ALL_VARIANT_FIELDS:
        if field_name == declared_field:
            continue
        hits = [v for v in variants if _same(v.get(field_name), identifier.value)]
        if hits:
            corrected = next(
                key for key, mapped in VARIANT_FIELD_BY_TYPE.items() if mapped == field_name
            )
            return hits, field_name, corrected
    return [], None, None


def resolve_shopify_variant(
    product_json: Mapping[str, Any] | None,
    identifiers: Iterable[TypedIdentifier],
    *,
    absence_evidence: AbsenceEvidence | None = None,
) -> VariantResolution:
    """Resolve typed identifiers against a fetched Shopify product JSON.

    ``product_json`` is ``None``/empty when the store answered validly but
    the product is not there. Everything else is a present product.
    """
    ordered = [
        identifier
        for identifier in identifiers
        if identifier.value is not None and str(identifier.value).strip() and identifier.is_current
    ]
    ordered.sort(key=lambda i: _RESOLUTION_ORDER.index(i.identifier_type))

    # --- Absence branch: the product itself is gone from the response ---
    if not product_json:
        evidence = absence_evidence or AbsenceEvidence()
        if evidence.is_proven():
            return AbsentProven(
                evidence=tuple(sorted(set(evidence.validated_absence_at))),
                reason=(
                    f"validated absence observed {len(set(evidence.validated_absence_at))} times "
                    f">= {ABSENCE_MIN_GAP} apart"
                ),
            )
        return Ambiguous(
            identifier=ordered[0] if ordered else None,
            reason=(
                "product absent from this response, but absence is not yet proven "
                f"({len(set(evidence.validated_absence_at))} validated-absence fetch(es); "
                f"policy requires {ABSENCE_MIN_FETCHES} >= {ABSENCE_MIN_GAP} apart)"
            ),
        )

    if not isinstance(product_json, Mapping) or "variants" not in product_json:
        return IdentityIncompatible(
            identifier=ordered[0] if ordered else None,
            reason="payload is not a Shopify product document (no 'variants' key)",
        )

    variants = _variants_of(product_json)
    handle = product_json.get("handle")

    # --- Product-level identity: a handle names the product ------------
    handle_identifiers = [
        i for i in ordered if i.identifier_type is CompetitorIdentifierType.HANDLE
    ]
    for identifier in handle_identifiers:
        if handle is not None and not _same(handle, identifier.value):
            return IdentityIncompatible(
                identifier=identifier,
                observed_identity=str(handle),
                reason=(
                    f"identifier names product handle {identifier.value!r} but the response "
                    f"is handle {handle!r}"
                ),
            )

    if not variants:
        return Ambiguous(
            identifier=ordered[0] if ordered else None,
            reason="product document carries no variants at all",
        )

    # An identifier of ANY declared type whose value equals the product
    # handle confirms product identity — this is the S-Tech shape, where
    # the legacy column arrives ``UNKNOWN`` and turns out to be a handle.
    handle_confirmation = next(
        (i for i in ordered if handle is not None and _same(handle, i.value)), None
    )

    # --- Variant-level identity ---------------------------------------
    ambiguity: Ambiguous | None = None
    unmatched_verified: TypedIdentifier | None = None

    for identifier in ordered:
        if identifier.identifier_type is CompetitorIdentifierType.HANDLE or (
            identifier is handle_confirmation
        ):
            # A handle confirms the *product*; it never selects among
            # that product's variants. Already validated above.
            continue
        hits, matched_field, corrected = _matches_for(identifier, variants)
        if len(hits) == 1:
            return Resolved(
                variant=hits[0],
                matched_identifier=identifier,
                matched_field=matched_field,
                corrected_type=corrected,
                reason=(
                    f"identifier {identifier.identifier_type} matched variant "
                    f"{matched_field}"
                    + (f" (declared type corrected to {corrected})" if corrected else "")
                ),
            )
        if len(hits) > 1 and ambiguity is None:
            ambiguity = Ambiguous(
                candidates=tuple(hits),
                identifier=identifier,
                reason=(
                    f"{len(hits)} variants share {matched_field}={identifier.value!r} — "
                    "quarantined rather than guessed"
                ),
            )
        if not hits and identifier.is_verified and unmatched_verified is None:
            unmatched_verified = identifier

    if ambiguity is not None:
        return ambiguity

    # A *verified* variant-selecting identifier that has vanished from a
    # multi-variant product is a real variant-level absence — but still
    # never NOT_LISTED (that verdict is about the product's listing, and
    # is gated on repeated validated absence of the product itself).
    if unmatched_verified is not None and len(variants) > 1:
        return Ambiguous(
            candidates=tuple(variants),
            identifier=unmatched_verified,
            reason=(
                f"verified {unmatched_verified.identifier_type} "
                f"{unmatched_verified.value!r} is absent from a {len(variants)}-variant "
                "product — the right variant is unknown, so no price is taken"
            ),
        )

    # --- Default-variant branch ---------------------------------------
    # Legitimate for exactly one variant and never for more: with a
    # single variant the product IS the variant, so an unmatched legacy
    # string tells us nothing about the store. With several, picking one
    # would be a guess.
    if len(variants) == 1:
        corrected = (
            CompetitorIdentifierType.HANDLE
            if handle_confirmation is not None
            and handle_confirmation.identifier_type is not CompetitorIdentifierType.HANDLE
            else None
        )
        return Resolved(
            variant=variants[0],
            matched_identifier=handle_confirmation,
            matched_field="handle" if handle_confirmation is not None else None,
            corrected_type=corrected,
            reason=(
                "single-variant product: identity confirmed at the product level, "
                "the sole variant is the only possible answer"
            ),
        )
    return Ambiguous(
        candidates=tuple(variants),
        identifier=ordered[0] if ordered else None,
        reason=(
            f"no identifier selects a variant and the product carries {len(variants)}; "
            "a default variant is only ever legitimate for a single-variant product"
        ),
    )


# --------------------------------------------------------------------
# Legacy bridge
# --------------------------------------------------------------------


def identifiers_from_legacy_context(
    variant_identifier: str | None,
    sku: str | None = None,
    *,
    typed: Iterable[TypedIdentifier] | None = None,
) -> list[TypedIdentifier]:
    """Build the identifier list an adapter should resolve with.

    Typed rows from ``match_competitor_identifiers`` win when present.
    Otherwise the legacy columns are wrapped — and, critically,
    ``competitor_variant_identifier`` becomes ``UNKNOWN``, **not**
    ``SHOPIFY_VARIANT_ID``. That single line is the S-Tech fix: the
    column never carried a type, so claiming one was the bug.
    """
    typed_rows = [row for row in (typed or ()) if row.is_current]
    if typed_rows:
        return typed_rows

    rows: list[TypedIdentifier] = []
    if variant_identifier is not None and str(variant_identifier).strip():
        rows.append(
            TypedIdentifier(
                identifier_type=CompetitorIdentifierType.UNKNOWN,
                value=str(variant_identifier),
                source=CompetitorIdentifierSource.LEGACY_BACKFILL,
            )
        )
    if sku is not None and str(sku).strip():
        rows.append(
            TypedIdentifier(
                identifier_type=CompetitorIdentifierType.SKU,
                value=str(sku),
                source=CompetitorIdentifierSource.LEGACY_BACKFILL,
            )
        )
    return rows
