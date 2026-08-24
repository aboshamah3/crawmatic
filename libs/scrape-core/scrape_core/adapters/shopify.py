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
from scrape_core.extraction.result import ExtractionCandidate

__all__ = ["ShopifyProductJsonAdapter", "shopify_json_url"]


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
        expected_variant = context.variant_identifier or context.sku
        variant: Mapping[str, Any] | None = None
        variant_paths = config.get("variant_identity_paths", ("id", "sku"))
        variant_paths = (variant_paths,) if isinstance(variant_paths, str) else variant_paths
        if expected_variant:
            for candidate in variants:
                if isinstance(candidate, Mapping):
                    for path in variant_paths:
                        observed = first_value(candidate, path)
                        if observed is not None and _same(observed, expected_variant):
                            variant = candidate
                            break
                    if variant is not None:
                        break
            if variant is None:
                missing = IdentityEvidence(
                    IdentityStatus.NOT_LISTED,
                    str(expected_variant),
                    source="shopify_variant",
                    reason="Shopify response contains no exact variant identifier",
                )
                return AdapterResult(AdapterOutcome.NOT_LISTED, response.final_url, missing, message=missing.reason)
        else:
            variant = next(
                (item for item in variants if isinstance(item, Mapping) and item.get("available") is True),
                next((item for item in variants if isinstance(item, Mapping)), None),
            )
        if variant is None:
            return AdapterResult(AdapterOutcome.NOT_FOUND, response.final_url, evidence)

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
