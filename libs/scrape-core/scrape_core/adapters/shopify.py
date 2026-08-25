"""Public Shopify ``products/{handle}.js`` adapter."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app_shared.enums import ExtractionMethod, StockStatus

from scrape_core.adapters.config import apply_decimal_transform, first_value, resolve_adapter_config
from scrape_core.adapters.result import (
    AdapterContext,
    AdapterOutcome,
    AdapterRequest,
    AdapterResponse,
    AdapterResult,
    IdentityEvidence,
    IdentityStatus,
)
from scrape_core.adapters.variant_resolution import (
    AbsenceEvidence,
    AbsentProven,
    Ambiguous,
    IdentityIncompatible,
    Resolved,
    TypedIdentifier,
    identifiers_from_legacy_context,
    outcome_for_resolution,
    resolve_shopify_variant,
)
from scrape_core.extraction.result import ExtractionCandidate

__all__ = ["ShopifyProductJsonAdapter", "shopify_json_url", "typed_identifiers_for"]


_HANDLE_RE = re.compile(r"/products/(?P<handle>[^/?#]+)", flags=re.IGNORECASE)


def _handle(url: str) -> str | None:
    match = _HANDLE_RE.search(urlsplit(url).path)
    return match.group("handle") if match else None


def shopify_json_url(product_url: str) -> str:
    parsed = urlsplit(product_url)
    path = parsed.path.rstrip("/")
    if path.endswith(".js"):
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}.js", "", ""))


def _same(left: Any, right: Any) -> bool:
    return str(left).strip().casefold() == str(right).strip().casefold()


def _positive_money(value: str | None) -> bool:
    try:
        return value is not None and Decimal(value) > 0
    except (InvalidOperation, ValueError):
        return False


def typed_identifiers_for(context: AdapterContext) -> list[TypedIdentifier]:
    """Typed identifiers for this target, preferring the B4 child table.

    ``context.values["typed_identifiers"]`` carries
    ``match_competitor_identifiers`` rows once the caller loads them
    (``TypedIdentifier`` instances, or plain mappings of the same shape).
    With nothing there we fall back to the legacy columns — where
    ``competitor_variant_identifier`` is deliberately typed ``UNKNOWN``
    rather than ``SHOPIFY_VARIANT_ID``: the column never carried a type,
    and asserting one is exactly what produced 26 false ``NOT_LISTED``
    verdicts on S-Tech in the 2026-08-24 canary.
    """
    raw = context.values.get("typed_identifiers") if context.values else None
    typed: list[TypedIdentifier] = []
    for item in raw or ():
        if isinstance(item, TypedIdentifier):
            typed.append(item)
        elif isinstance(item, Mapping):
            typed.append(TypedIdentifier(**dict(item)))
    return identifiers_from_legacy_context(
        context.variant_identifier, context.sku, typed=typed
    )


def _absence_evidence_for(context: AdapterContext) -> AbsenceEvidence:
    """Prior validated-absence timestamps the caller has gathered, if any."""
    raw = context.values.get("validated_absence_at") if context.values else None
    if not raw:
        return AbsenceEvidence()
    return AbsenceEvidence(validated_absence_at=tuple(raw))


class ShopifyProductJsonAdapter:
    def build_request(self, context: AdapterContext) -> AdapterRequest:
        config = resolve_adapter_config(context)
        return AdapterRequest(
            shopify_json_url(context.target_url),
            headers=config.get("headers", {}),
            follow_redirects=config.get("follow_redirects", True) is not False,
        )

    def adapt(self, response: AdapterResponse, context: AdapterContext, **_: Any) -> AdapterResult:
        config = resolve_adapter_config(context)
        expected_handle = _handle(context.target_url)
        try:
            body = response.body.decode("utf-8") if isinstance(response.body, bytes) else response.body
            product = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return AdapterResult(
                AdapterOutcome.INVALID_RESPONSE,
                response.final_url,
                IdentityEvidence(IdentityStatus.UNVERIFIED, expected_handle, source="shopify_json"),
                message=f"Shopify product endpoint returned malformed JSON: {exc}",
            )
        if not isinstance(product, Mapping):
            return AdapterResult(
                AdapterOutcome.INVALID_RESPONSE,
                response.final_url,
                IdentityEvidence(IdentityStatus.UNVERIFIED, expected_handle, source="shopify_json"),
                message="Shopify product endpoint did not return an object",
            )

        observed_handle = product.get("handle")
        if expected_handle and (not observed_handle or not _same(expected_handle, observed_handle)):
            evidence = IdentityEvidence(
                IdentityStatus.MISMATCH,
                expected_handle,
                str(observed_handle) if observed_handle else None,
                "shopify_handle",
                "Shopify response handle does not match the requested product",
            )
            return AdapterResult(AdapterOutcome.IDENTITY_MISMATCH, response.final_url, evidence, message=evidence.reason)
        evidence = IdentityEvidence(
            IdentityStatus.VALID, expected_handle, str(observed_handle), "shopify_handle"
        )

        variants = product.get("variants")
        if not isinstance(variants, list) or not variants:
            return AdapterResult(
                AdapterOutcome.NOT_FOUND,
                response.final_url,
                evidence,
                message="Shopify product has no variants",
            )

        # EPA B4: typed, evidence-based variant resolution. The previous
        # code compared the untyped legacy identifier straight against
        # `variants[].id`/`sku` and answered NOT_LISTED when nothing
        # matched — which is how 26 of 30 healthy S-Tech products were
        # declared delisted in the 2026-08-24 canary. NOT_LISTED is now
        # reachable only from a repeatedly-validated absence.
        identifiers = typed_identifiers_for(context)
        resolution = resolve_shopify_variant(
            product, identifiers, absence_evidence=_absence_evidence_for(context)
        )
        if isinstance(resolution, AbsentProven):
            missing = IdentityEvidence(
                IdentityStatus.NOT_LISTED,
                identifiers[0].value if identifiers else None,
                source="shopify_variant",
                reason=resolution.reason,
            )
            return AdapterResult(
                AdapterOutcome.NOT_LISTED, response.final_url, missing, message=missing.reason
            )
        if isinstance(resolution, (Ambiguous, IdentityIncompatible)):
            unresolved = IdentityEvidence(
                IdentityStatus.UNRESOLVED,
                resolution.identifier.value if resolution.identifier else None,
                getattr(resolution, "observed_identity", None),
                "shopify_variant",
                resolution.reason,
            )
            return AdapterResult(
                outcome_for_resolution(resolution),
                response.final_url,
                unresolved,
                message=resolution.reason,
                metadata={"needs_review": True, "resolution": type(resolution).__name__},
            )
        # LOW-6 (Phase B gate-fix): `assert` is stripped under `python -O`,
        # which would turn an exhaustiveness bug into a silent fall-through
        # instead of a loud failure. Every other member of the closed ADT
        # (AbsentProven; Ambiguous/IdentityIncompatible) is handled above, so
        # reaching here with anything but `Resolved` means the ADT gained a
        # member this function was not updated for.
        if not isinstance(resolution, Resolved):
            raise TypeError(f"unhandled VariantResolution: {resolution!r}")
        variant = resolution.variant

        fields = config.get("fields", {})
        fields = fields if isinstance(fields, Mapping) else {}
        profile_transform = (
            getattr(context.profile, "price_transform_rules", None) if context.profile else None
        )
        transform = config.get("price_transform", profile_transform or {"divide_by": 100})
        price = apply_decimal_transform(first_value(variant, fields.get("price", "price")), transform)
        if price is None:
            return AdapterResult(
                AdapterOutcome.NOT_FOUND,
                response.final_url,
                evidence,
                message="selected Shopify variant has no usable price",
            )
        compare = apply_decimal_transform(
            first_value(variant, fields.get("old_price", "compare_at_price")), transform
        )
        old_price = compare if _positive_money(compare) and Decimal(compare) != Decimal(price) else None
        available = first_value(variant, fields.get("stock", "available"))
        currency_value = first_value(product, fields.get("currency", "currency"))
        currency = currency_value or config.get("currency") or context.expected_currency
        candidate = ExtractionCandidate(
            raw_price_text=price,
            currency=str(currency) if currency else None,
            method=ExtractionMethod.PLATFORM_JSON,
            confidence=float(config.get("confidence", 1.0)),
            selector_used="Shopify exact product/variant JSON",
            raw_title=str(product.get("title")) if product.get("title") else None,
            stock=(
                StockStatus.IN_STOCK
                if available is True
                else StockStatus.OUT_OF_STOCK
                if available is False
                else StockStatus.UNKNOWN
            ),
            matched_text=json.dumps(variant, default=str),
        )
        return AdapterResult(
            AdapterOutcome.FOUND,
            response.final_url,
            evidence,
            candidate=candidate,
            old_price_text=old_price,
            canonical_url=context.target_url,
        )
